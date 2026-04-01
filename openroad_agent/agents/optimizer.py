"""Optimiser sub-agent: iteratively improves design results."""

from __future__ import annotations

from deepagents import create_deep_agent

from openroad_agent.prompts.system_prompts import OPTIMISER_PROMPT
from openroad_agent.tools.openroad_runner import run_openroad_tcl, read_openroad_log
from openroad_agent.tools.tcl_templates import (
    assemble_full_flow_tcl,
    assemble_partial_flow_tcl,
    get_tcl_template,
    list_flow_stages,
)
from openroad_agent.tools.metrics_parser import (
    compare_metrics,
    extract_timing_summary,
    parse_metrics_file,
    check_metrics_limits,
)
from openroad_agent.tools.design_analyzer import (
    analyse_timing_report,
    suggest_parameter_ranges,
    read_platform_vars,
)


def create_optimiser_agent(model: str | None = None):
    """Create the design optimiser sub-agent.

    This agent can modify parameters, regenerate TCL scripts, re-run
    OpenROAD, and compare results to find better configurations.
    """
    tools = [
        run_openroad_tcl,
        read_openroad_log,
        assemble_full_flow_tcl,
        assemble_partial_flow_tcl,
        get_tcl_template,
        list_flow_stages,
        compare_metrics,
        extract_timing_summary,
        parse_metrics_file,
        check_metrics_limits,
        analyse_timing_report,
        suggest_parameter_ranges,
        read_platform_vars,
    ]

    return create_deep_agent(
        name="optimiser",
        model=model,
        tools=tools,
        system_prompt=OPTIMISER_PROMPT,
    )
