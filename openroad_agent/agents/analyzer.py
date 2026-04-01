"""Analyser sub-agent: interprets OpenROAD results and metrics."""

from __future__ import annotations

from deepagents import create_deep_agent

from openroad_agent.prompts.system_prompts import ANALYSER_PROMPT
from openroad_agent.tools.metrics_parser import (
    check_metrics_limits,
    compare_metrics,
    extract_timing_summary,
    parse_metrics_file,
)
from openroad_agent.tools.design_analyzer import (
    analyse_timing_report,
    suggest_parameter_ranges,
)
from openroad_agent.tools.openroad_runner import read_openroad_log


def create_analyser_agent(model: str | None = None):
    """Create the design analyser sub-agent.

    This agent specialises in interpreting OpenROAD output, timing
    reports, metrics, and providing actionable analysis.
    """
    tools = [
        parse_metrics_file,
        compare_metrics,
        extract_timing_summary,
        check_metrics_limits,
        analyse_timing_report,
        suggest_parameter_ranges,
        read_openroad_log,
    ]

    return create_deep_agent(
        name="analyser",
        model=model,
        tools=tools,
        system_prompt=ANALYSER_PROMPT,
    )
