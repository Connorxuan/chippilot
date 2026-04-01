"""Main entry point for OpenROAD DeepAgents system.

Provides both CLI and programmatic interfaces for running the
LLM-powered OpenROAD EDA automation system.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
import warnings
from pathlib import Path
from typing import Optional

# Suppress noisy grpc polling warnings (harmless with nest_asyncio)
logging.getLogger("grpc").setLevel(logging.ERROR)
logging.getLogger("grpc._cython").setLevel(logging.CRITICAL)
logging.getLogger("grpc._cython.cygrpc").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore", message=".*BlockingIOError.*")


def _load_dotenv() -> None:
    """Load .env file from project root if it exists."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        # Also try .env.example as fallback
        env_path = Path(__file__).resolve().parent.parent / ".env.example"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Don't override existing env vars
                if key and key not in os.environ:
                    os.environ[key] = value


# Load .env before anything else reads os.environ
_load_dotenv()

from openroad_agent.config import OpenROADConfig


def create_agent(model: str | None = None, config: OpenROADConfig | None = None):
    """Create and return the orchestrator agent.

    Args:
        model: LLM model identifier (default from config/env).
        config: OpenROADConfig (creates default if None).

    Returns:
        A compiled DeepAgent graph.
    """
    from openroad_agent.agents.orchestrator import create_orchestrator_agent
    return create_orchestrator_agent(model=model, config=config)


# ── Streaming display helpers ──────────────────────────────────────────

def _normalize_msg_content(content) -> str:
    """Normalize LangChain message content (Gemini list-of-dicts) to str."""
    if isinstance(content, list):
        parts = [
            p["text"] if isinstance(p, dict) and "text" in p else str(p)
            for p in content
        ]
        return "\n".join(parts)
    return content or ""


def _brief_args(args: dict) -> str:
    """Summarise tool call arguments for compact display."""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "…"
        parts.append(f"{k}={s}")
    out = ", ".join(parts)
    return out[:200] + "…" if len(out) > 200 else out


def _print_orchestrator_msg(msg) -> None:
    """Display an orchestrator-level streaming message."""
    content = _normalize_msg_content(msg.content)

    if msg.type == "ai":
        tool_calls = getattr(msg, "tool_calls", [])
        if content.strip():
            print(f"🤖 Agent > {content}")
        for tc in tool_calls:
            print(f"  🔧 {tc['name']}({_brief_args(tc.get('args', {}))})")
    elif msg.type == "tool":
        # For delegation results, user already saw sub-agent output — truncate
        max_len = 150 if msg.name.startswith("delegate_to_") else 500
        if len(content) > max_len:
            content = content[:max_len] + "… [truncated]"
        print(f"  📎 [{msg.name}]: {content}")


def _print_subagent_msg(agent_name: str, msg) -> None:
    """Display a sub-agent message with box-drawing indentation."""
    content = _normalize_msg_content(msg.content)

    if msg.type == "ai":
        tool_calls = getattr(msg, "tool_calls", [])
        if content.strip():
            lines = content.strip().split("\n")
            for line in lines[:5]:
                print(f"  │ 🤖 {line}")
            if len(lines) > 5:
                print(f"  │    … ({len(lines)} lines)")
        for tc in tool_calls:
            print(f"  │ 🔧 {tc['name']}({_brief_args(tc.get('args', {}))})")
    elif msg.type == "tool":
        if len(content) > 300:
            content = content[:300] + "… [truncated]"
        first_line = content.split("\n")[0]
        if len(first_line) > 200:
            first_line = first_line[:200] + "…"
        print(f"  │ 📎 [{msg.name}]: {first_line}")


def _on_subagent_event(event_type: str, agent_name: str, msg) -> None:
    """Callback for sub-agent streaming events."""
    if event_type == "start":
        bar = "─" * max(1, 42 - len(agent_name))
        print(f"\n  ┌─ Sub-agent: {agent_name} {bar}")
    elif event_type == "end":
        print(f"  └{'─' * 55}")
    elif event_type == "msg" and msg is not None:
        _print_subagent_msg(agent_name, msg)
        _log_subagent_msg_to_session(agent_name, msg)


# ── Chat‑log persistence helpers ──────────────────────────────────────

def _log_msg_to_session(msg) -> None:
    """Persist a LangChain orchestrator message to the session chat log."""
    from openroad_agent.tools.session_manager import SessionManager

    session = SessionManager.get_current()
    if not session:
        return

    content = _normalize_msg_content(msg.content)

    if msg.type == "ai":
        tool_calls = getattr(msg, "tool_calls", [])
        if content.strip():
            session.log_message("assistant", content)
        for tc in tool_calls:
            session.log_tool_call(tc["name"], tc.get("args", {}))
    elif msg.type == "tool":
        # Limit stored tool result size
        trimmed = content if len(content) <= 2000 else content[:1997] + "…"
        session.log_message("tool_result", trimmed, tool_name=getattr(msg, "name", ""))
    elif msg.type == "human":
        session.log_message("user", content)


def _log_subagent_msg_to_session(agent_name: str, msg) -> None:
    """Persist a sub-agent message to the session chat log."""
    from openroad_agent.tools.session_manager import SessionManager

    session = SessionManager.get_current()
    if not session:
        return

    content = _normalize_msg_content(msg.content)

    if msg.type == "ai":
        tool_calls = getattr(msg, "tool_calls", [])
        if content.strip():
            trimmed = content if len(content) <= 2000 else content[:1997] + "…"
            session.log_message("sub_agent", trimmed, agent_name=agent_name)
        for tc in tool_calls:
            session.log_tool_call(
                tc["name"], tc.get("args", {}),
            )
    elif msg.type == "tool":
        trimmed = content if len(content) <= 2000 else content[:1997] + "…"
        session.log_message(
            "sub_agent_tool_result", trimmed,
            agent_name=agent_name, tool_name=getattr(msg, "name", ""),
        )


async def chat_loop(agent, config: OpenROADConfig) -> None:
    """Interactive chat loop with the OpenROAD agent.

    Args:
        agent: The compiled DeepAgent graph.
        config: OpenROADConfig instance.
    """
    from langchain_core.messages import HumanMessage

    # Enable sub-agent streaming
    from openroad_agent.agents.orchestrator import set_stream_callback
    set_stream_callback(_on_subagent_event)

    # Suppress noisy grpc BlockingIOError from nest_asyncio interactions
    loop = asyncio.get_event_loop()
    _orig_handler = loop.get_exception_handler()

    def _quiet_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, BlockingIOError):
            return  # suppress
        if _orig_handler:
            _orig_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_quiet_handler)

    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    # Show session info
    from openroad_agent.tools.session_manager import SessionManager
    session = SessionManager.get_current()

    print("=" * 70)
    print("  OpenROAD Pilot — LLM-Powered EDA Automation")
    print("  Powered by DeepAgents + OpenROAD")
    print("=" * 70)
    print(f"  Model    : {config.model_name}")
    print(f"  OpenROAD : {config.openroad_bin}")
    print(f"  Work Dir : {config.work_dir}")
    if session:
        print(f"  Session  : {session.session_name}")
        print(f"  Sess Dir : {session.base_dir}")
    print(f"  Platforms: {', '.join(config.supported_platforms.keys())}")
    print("=" * 70)
    print()
    print("Type your EDA task or question. Type 'quit' or 'exit' to stop.")
    print("Examples:")
    print("  • Run the full RTL-to-GDS flow for aes on nangate45")
    print("  • Show available test designs")
    print("  • Optimize timing for gcd on sky130hd")
    print("  • Explore density parameter from 0.2 to 0.6 for aes_nangate45")
    print()

    while True:
        try:
            user_input = input("\n🔧 You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        # Log user message to session
        if session:
            session.log_message("user", user_input)

        print("\n⚙️  Processing...\n")

        try:
            # Stream agent responses
            async for event in agent.astream(
                {"messages": [HumanMessage(content=user_input)]},
                config=run_config,
                stream_mode="updates",
            ):
                for node_name, node_output in event.items():
                    if "messages" in node_output:
                        for msg in node_output["messages"]:
                            _print_orchestrator_msg(msg)
                            _log_msg_to_session(msg)
        except Exception as e:
            print(f"\n❌ Error: {e}")
            print("Please try again or rephrase your request.\n")
            if session:
                session.log_message("error", str(e))


async def run_single_task(agent, task: str, config: OpenROADConfig) -> str:
    """Run a single task non-interactively and return the result.

    Args:
        agent: The compiled DeepAgent graph.
        task: The task description string.
        config: OpenROADConfig instance.

    Returns:
        The agent's final response text.
    """
    from langchain_core.messages import HumanMessage

    # Suppress noisy grpc BlockingIOError
    loop = asyncio.get_event_loop()

    def _quiet_handler(loop, context):
        if isinstance(context.get("exception"), BlockingIOError):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_quiet_handler)

    # Log the task to session
    from openroad_agent.tools.session_manager import SessionManager
    session = SessionManager.get_current()
    if session:
        session.log_message("user", task)

    thread_id = str(uuid.uuid4())
    run_config = {"configurable": {"thread_id": thread_id}}

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=task)]},
        config=run_config,
    )

    # Reuse the same robust extraction as sub-agent delegation
    from openroad_agent.agents.orchestrator import _extract_subagent_result
    text = _extract_subagent_result(result)

    # Log the result to session
    if session:
        session.log_message("assistant", text)

    return text


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="openroad-agent",
        description="OpenROAD Pilot — LLM-powered EDA automation using DeepAgents",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model identifier (default: from OPENROAD_LLM_MODEL env or gpt-4o)",
    )
    parser.add_argument(
        "--openroad-bin",
        default=None,
        help="Path to the openroad binary (default: from OPENROAD_BIN env or 'openroad')",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Working directory for run outputs (default: ./openroad_work)",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Single task to run non-interactively (omit for interactive mode)",
    )
    parser.add_argument(
        "--design",
        default=None,
        help="Quick-start: design name (e.g., aes)",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Quick-start: platform name (e.g., nangate45)",
    )
    parser.add_argument(
        "--flow",
        choices=["full", "floorplan", "place", "cts", "route", "signoff"],
        default=None,
        help="Quick-start: run a predefined flow subset",
    )

    args = parser.parse_args()

    # Build config
    config = OpenROADConfig()
    if args.openroad_bin:
        config.openroad_bin = args.openroad_bin
    if args.work_dir:
        config.work_dir = args.work_dir
        os.makedirs(config.work_dir, exist_ok=True)

    # Build quick-start task if design + platform specified
    task = args.task
    if not task and args.design and args.platform:
        flow_desc = args.flow or "full"
        task = (
            f"Run the {flow_desc} RTL-to-GDS flow for design '{args.design}' "
            f"on platform '{args.platform}'. "
            f"Use the test files from OpenROAD/test/. "
            f"Show me the timing and area results."
        )

    # Create agent
    agent = create_agent(model=args.model, config=config)

    if task:
        # Non-interactive single task mode
        try:
            result = asyncio.run(run_single_task(agent, task, config))
            print(result)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        except Exception as e:
            print(f"\n❌ Fatal error: {type(e).__name__}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        # Interactive chat mode
        asyncio.run(chat_loop(agent, config))


if __name__ == "__main__":
    main()
