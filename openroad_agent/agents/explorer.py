"""Explorer sub-agent: systematic parameter space exploration."""

from __future__ import annotations

from deepagents import create_deep_agent

from openroad_agent.prompts.system_prompts import EXPLORER_PROMPT
from openroad_agent.tools.openroad_runner import run_openroad_tcl, read_openroad_log
from openroad_agent.tools.tcl_templates import (
    assemble_full_flow_tcl,
    assemble_partial_flow_tcl,
    list_flow_stages,
)
from openroad_agent.tools.metrics_parser import (
    compare_metrics,
    extract_timing_summary,
    parse_metrics_file,
)
from openroad_agent.tools.design_analyzer import (
    suggest_parameter_ranges,
    read_platform_vars,
    list_supported_platforms,
)


def create_explorer_agent(model: str | None = None):
    """Create the parameter space explorer sub-agent.

    This agent systematically explores the design parameter space by
    running multiple configurations and comparing results.
    """
    tools = [
        run_openroad_tcl,
        read_openroad_log,
        assemble_full_flow_tcl,
        assemble_partial_flow_tcl,
        list_flow_stages,
        compare_metrics,
        extract_timing_summary,
        parse_metrics_file,
        suggest_parameter_ranges,
        read_platform_vars,
        list_supported_platforms,
    ]

    return create_deep_agent(
        name="explorer",
        model=model,
        tools=tools,
        system_prompt=EXPLORER_PROMPT,
    )
