"""TCL Generator sub-agent: generates OpenROAD TCL scripts."""

from __future__ import annotations

from deepagents import create_deep_agent

from openroad_agent.prompts.system_prompts import TCL_GENERATOR_PROMPT
from openroad_agent.tools.tcl_templates import (
    assemble_full_flow_tcl,
    assemble_partial_flow_tcl,
    get_tcl_template,
    list_flow_stages,
)
from openroad_agent.tools.design_analyzer import (
    list_available_designs,
    list_supported_platforms,
    read_design_tcl,
    read_platform_vars,
)


def create_tcl_generator_agent(model: str | None = None):
    """Create the TCL generator sub-agent.

    This agent specialises in producing OpenROAD TCL scripts from
    templates, user requirements, and platform configurations.
    """
    tools = [
        assemble_full_flow_tcl,
        assemble_partial_flow_tcl,
        get_tcl_template,
        list_flow_stages,
        list_available_designs,
        list_supported_platforms,
        read_design_tcl,
        read_platform_vars,
    ]

    return create_deep_agent(
        name="tcl_generator",
        model=model,
        tools=tools,
        system_prompt=TCL_GENERATOR_PROMPT,
    )
