"""Master orchestrator agent: coordinates the full OpenROAD flow."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Any

from langchain.chat_models.base import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain.agents import create_agent

from openroad_agent.checkpoint import get_sqlite_checkpointer
from openroad_agent.config import OpenROADConfig
from openroad_agent.prompts.system_prompts import (
    ORCHESTRATOR_PROMPT,
    TCL_GENERATOR_PROMPT,
    ANALYSER_PROMPT,
    OPTIMISER_PROMPT,
    EXPLORER_PROMPT,
)

# ── All tools ──────────────────────────────────────────────────────────
from openroad_agent.tools.openroad_runner import (
    run_openroad_tcl,
    run_openroad_command,
    read_openroad_log,
)
from openroad_agent.tools.tcl_templates import (
    assemble_full_flow_tcl,
    assemble_partial_flow_tcl,
    get_tcl_template,
    list_flow_stages,
)
from openroad_agent.tools.metrics_parser import (
    parse_metrics_file,
    compare_metrics,
    extract_timing_summary,
    check_metrics_limits,
)
from openroad_agent.tools.design_analyzer import (
    list_available_designs,
    read_design_tcl,
    read_platform_vars,
    list_supported_platforms,
    analyse_timing_report,
    suggest_parameter_ranges,
)
from openroad_agent.tools.yosys_runner import (
    run_yosys_synthesis,
    generate_yosys_synth_script,
    list_synth_platforms,
)
from openroad_agent.tools.file_manager import (
    save_design_files,
    list_design_files,
    read_design_file,
)
from openroad_agent.tools.klayout_runner import (
    run_klayout_gds,
    list_gds_platforms,
)
from openroad_agent.tools.session_manager import (
    SessionManager,
    start_session,
    get_session_info,
    list_downloadable_artifacts,
    list_sessions,
)


def _all_tools():
    """Return the full list of available tools."""
    return [
        run_openroad_tcl,
        run_openroad_command,
        read_openroad_log,
        assemble_full_flow_tcl,
        assemble_partial_flow_tcl,
        get_tcl_template,
        list_flow_stages,
        list_available_designs,
        read_design_tcl,
        read_platform_vars,
        list_supported_platforms,
        parse_metrics_file,
        compare_metrics,
        extract_timing_summary,
        check_metrics_limits,
        analyse_timing_report,
        suggest_parameter_ranges,
        # ── Yosys synthesis tools ──
        run_yosys_synthesis,
        generate_yosys_synth_script,
        list_synth_platforms,
        # ── Design file management ──
        save_design_files,
        list_design_files,
        read_design_file,
        # ── KLayout GDS generation ──
        run_klayout_gds,
        list_gds_platforms,
        # ── Session management ──
        start_session,
        get_session_info,
        list_downloadable_artifacts,
        list_sessions,
    ]


# ── Sub-agent result extraction ────────────────────────────────────────

def _normalise_content(content) -> str:
    """Normalise Gemini's list-of-dicts content format to plain text."""
    if isinstance(content, list):
        parts = [
            p["text"] if isinstance(p, dict) and "text" in p else str(p)
            for p in content
        ]
        return "\n".join(parts)
    return content or ""


def _extract_subagent_result(result: dict) -> str:
    """Extract a meaningful result from a sub-agent's message history.

    Strategy (in priority order):
    1. Last AI message with real text content.
    2. All AI messages with content concatenated (the agent may have
       produced partial answers across multiple turns).
    3. Fallback: compile a summary from tool-call results
       (run_openroad_tcl outputs, metrics, etc.).
    """
    messages = result.get("messages", [])

    # ── Strategy 1: last AI message with content ──────────────────────
    for msg in reversed(messages):
        if getattr(msg, "type", "") == "ai":
            text = _normalise_content(msg.content)
            if text.strip():
                return text

    # ── Strategy 2: concatenate all non-empty AI messages ─────────────
    ai_texts = []
    for msg in messages:
        if getattr(msg, "type", "") == "ai":
            text = _normalise_content(msg.content)
            if text.strip():
                ai_texts.append(text)
    if ai_texts:
        return "\n\n".join(ai_texts)

    # ── Strategy 3: compile summary from tool results ─────────────────
    import json as _json

    tool_summaries = []
    for msg in messages:
        if getattr(msg, "type", "") != "tool":
            continue
        tool_name = getattr(msg, "name", "")
        raw = _normalise_content(msg.content)

        # Parse run_openroad_tcl results for key metrics
        if tool_name == "run_openroad_tcl":
            try:
                data = _json.loads(raw)
                entry = {
                    "run": data.get("run_dir", "").split("/")[-1],
                    "success": data.get("success"),
                    "elapsed_s": data.get("elapsed_s"),
                    "log_file": data.get("log_file", ""),
                }
                metrics = data.get("metrics", {})
                if metrics:
                    entry["metrics"] = metrics
                tool_summaries.append(entry)
            except (_json.JSONDecodeError, Exception):
                tool_summaries.append({"tool": tool_name, "output": raw[:500]})
        elif tool_name in (
            "assemble_full_flow_tcl",
            "assemble_partial_flow_tcl",
        ):
            # Skip bulky TCL scripts in fallback summary
            continue
        else:
            if len(raw) > 300:
                raw = raw[:300] + "…"
            tool_summaries.append({"tool": tool_name, "output": raw})

    if tool_summaries:
        return (
            "Sub-agent completed but did not produce a final summary. "
            "Here are the tool execution results:\n\n"
            + _json.dumps(tool_summaries, indent=2, ensure_ascii=False)
        )

    return "Sub-agent produced no output."


def _has_subagent_output(result: dict) -> bool:
    """Return True if a sub-agent result contains AI text or tool output."""
    for msg in result.get("messages", []):
        msg_type = getattr(msg, "type", "")
        if msg_type == "tool":
            raw = _normalise_content(msg.content)
            if raw.strip():
                return True
        if msg_type == "ai":
            text = _normalise_content(msg.content)
            if text.strip():
                return True
            if getattr(msg, "tool_calls", None):
                return True
    return False


# ── Sub-agent streaming callback ──────────────────────────────────────
#
# When set, sub-agents stream their internal messages (tool calls,
# reasoning, results) back to the user in real-time through this callback.
# Signature:  callback(event_type: str, agent_name: str, msg: Any) -> None
#   event_type: "start" | "msg" | "end"

_stream_callback: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "openroad_agent_stream_callback",
    default=None,
)


def _run_coro_blocking(coro):
    """Run a coroutine from sync code, even if an event loop is running.

    If no loop is running in this thread, use ``asyncio.run`` directly.
    If a loop is already running, execute the coroutine in a helper thread
    with its own event loop and return the result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            result["value"] = ctx.run(asyncio.run, coro)
        except BaseException as exc:  # pragma: no cover
            error["value"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()

    if "value" in error:
        raise error["value"]
    return result.get("value")


def set_stream_callback(callback) -> None:
    """Set a callback for streaming sub-agent messages to the user.

    Args:
        callback: A function(event_type, agent_name, msg) called for
                  each sub-agent lifecycle event.  Set to ``None`` to
                  disable streaming (sub-agents run silently).
    """
    _stream_callback.set(callback)


# ── Sub-agent runner ───────────────────────────────────────────────────

def _make_subagent_tool(
    name: str,
    description: str,
    system_prompt: str,
    agent_tools: list,
    model_name: str,
):
    """Create a tool that delegates work to a specialised sub-agent.

    Each invocation spins up a lightweight react-agent with the given tools
    and system prompt, runs the user request, and returns the result.
    """

    @tool(f"delegate_to_{name}", description=description)
    def _delegate(task: str) -> str:
        """Delegate a task to this specialised sub-agent.

        IMPORTANT: When calling this tool, include ALL relevant context
        in the task string — the design name, platform, previous run
        parameters, metrics, and what needs to be done. The sub-agent
        has no memory of earlier conversation.

        Args:
            task: Detailed description including full context and what
                  the sub-agent should do.

        Returns:
            The sub-agent's final text response.
        """
        cb = _stream_callback.get()
        if cb:
            cb("start", name, None)

        try:
            llm = init_chat_model(model_name)
            sub = create_agent(
                model=llm,
                tools=agent_tools,
                system_prompt=system_prompt,
            )

            if cb:
                # ── Streaming mode: forward sub-agent events ──────
                async def _run_streaming():
                    all_msgs = [HumanMessage(content=task)]
                    async for event in sub.astream(
                        {"messages": [HumanMessage(content=task)]},
                        config={"recursion_limit": 120},
                        stream_mode="updates",
                    ):
                        for _nd, nd_out in event.items():
                            if "messages" in nd_out:
                                for m in nd_out["messages"]:
                                    all_msgs.append(m)
                                    cb("msg", name, m)
                    return {"messages": all_msgs}

                result = _run_coro_blocking(_run_streaming())
                if not _has_subagent_output(result):
                    retry_notice = AIMessage(
                        content=(
                            "Sub-agent streaming produced no actions or final response; "
                            "retrying once without streaming."
                        )
                    )
                    cb("msg", name, retry_notice)
                    result = _run_coro_blocking(
                        sub.ainvoke(
                            {"messages": [HumanMessage(content=task)]},
                            config={"recursion_limit": 120},
                        )
                    )
            else:
                # ── Silent mode (non-interactive / tests) ─────────
                result = _run_coro_blocking(
                    sub.ainvoke(
                        {"messages": [HumanMessage(content=task)]},
                        config={"recursion_limit": 120},
                    )
                )
            return _extract_subagent_result(result)
        except Exception as exc:
            if cb:
                cb(
                    "msg",
                    name,
                    AIMessage(content=f"Sub-agent encountered an error: {type(exc).__name__}: {exc}"),
                )
            return (
                f"Sub-agent encountered an error: {type(exc).__name__}: {exc}"
            )
        finally:
            if cb:
                cb("end", name, None)

    return _delegate


def create_orchestrator_agent(
    model: str | None = None,
    config: OpenROADConfig | None = None,
) -> Any:
    """Create the master orchestrator agent with all tools and sub-agents.

    Args:
        model: LLM model string, use provider:model format
               (e.g. "google_genai:gemini-2.5-flash").
        config: OpenROADConfig instance (uses defaults if None).

    Returns:
        A compiled react-agent graph ready for invocation.
    """
    cfg = config or OpenROADConfig()
    model_name = model or cfg.model_name

    # ── Auto-start a session for this conversation ─────────────────────
    if not SessionManager.get_current():
        SessionManager.start_new(cfg.work_dir)

    tools = _all_tools()

    # Build sub-agent delegation tools
    subagent_tools = [
        _make_subagent_tool(
            name="tcl_generator",
            description=(
                "Generate OpenROAD TCL scripts or Yosys synthesis scripts "
                "for any flow stage or the full RTL-to-GDS flow. Delegate "
                "here when you need a new or modified TCL / synthesis script."
            ),
            system_prompt=TCL_GENERATOR_PROMPT,
            agent_tools=[
                assemble_full_flow_tcl,
                assemble_partial_flow_tcl,
                get_tcl_template,
                list_flow_stages,
                list_available_designs,
                list_supported_platforms,
                read_design_tcl,
                read_platform_vars,
                generate_yosys_synth_script,
                list_synth_platforms,
            ],
            model_name=model_name,
        ),
        _make_subagent_tool(
            name="analyser",
            description=(
                "Analyse OpenROAD results, timing reports, and metrics. "
                "Delegate here to interpret run outputs and identify issues."
            ),
            system_prompt=ANALYSER_PROMPT,
            agent_tools=[
                parse_metrics_file,
                compare_metrics,
                extract_timing_summary,
                check_metrics_limits,
                analyse_timing_report,
                suggest_parameter_ranges,
                read_openroad_log,
            ],
            model_name=model_name,
        ),
        _make_subagent_tool(
            name="optimiser",
            description=(
                "Optimise design by adjusting parameters and re-running flow "
                "stages. Delegate here for iterative design improvement."
            ),
            system_prompt=OPTIMISER_PROMPT,
            agent_tools=tools,
            model_name=model_name,
        ),
        _make_subagent_tool(
            name="explorer",
            description=(
                "Systematically explore parameter space with multiple runs. "
                "Delegate here for parameter sweeps and Pareto analysis."
            ),
            system_prompt=EXPLORER_PROMPT,
            agent_tools=tools,
            model_name=model_name,
        ),
    ]

    llm = init_chat_model(model_name)
    all_agent_tools = tools + subagent_tools

    return create_agent(
        model=llm,
        tools=all_agent_tools,
        system_prompt=ORCHESTRATOR_PROMPT,
        checkpointer=get_sqlite_checkpointer(cfg.work_dir),
    )
