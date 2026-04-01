"""Tool: TCL template library for RTL-to-GDS flow stages.

These templates mirror the reference flow in OpenROAD/test/flow.tcl and
can be assembled, customised, and parameterised by the LLM agent.

Naming convention:
  - _FMT templates use Python .format() and need {{ }} for literal TCL braces.
  - _RAW templates are plain TCL text (single { } for TCL braces).
"""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool


# =====================================================================
# FMT templates – used with .format(), {{ }} → { } after formatting
# =====================================================================

HEADER_FMT = '''\
# ============================================================
# Auto-generated OpenROAD TCL script
# Design : {design}
# Platform: {platform}
# ============================================================
source "helpers.tcl"
source "flow_helpers.tcl"
source "{platform_vars}"
'''

READ_DESIGN_FMT = '''
# ── Read Design ────────────────────────────────────────────
set design "{design}"
set top_module "{top_module}"
set synth_verilog "{synth_verilog}"
set sdc_file "{sdc_file}"
set die_area {{{die_area}}}
set core_area {{{core_area}}}

{extra_vars}

read_libraries
read_verilog $synth_verilog
link_design $top_module
read_sdc $sdc_file

set_thread_count [cpu_count]
sta::set_thread_count 1
'''

GLOBAL_PLACEMENT_FMT = '''
# ── Global Placement ──────────────────────────────────────
foreach layer_adjustment $global_routing_layer_adjustments {{
  lassign $layer_adjustment layer adjustment
  set_global_routing_layer_adjustment $layer $adjustment
}}
set_routing_layers -signal $global_routing_layers \\
  -clock $global_routing_clock_layers
set_macro_extension 2

global_placement -density {density} \\
  -pad_left {pad} -pad_right {pad} -skip_io

place_pins -hor_layers $io_placer_hor_layer -ver_layers $io_placer_ver_layer

global_placement -routability_driven -density {density} \\
  -pad_left {pad} -pad_right {pad}

set global_place_db [make_result_file ${{design}}_${{platform}}_global_place.db]
write_db $global_place_db
'''

RESIZE_REPAIR_FMT = '''
# ── Resize / Repair ───────────────────────────────────────
source $layer_rc_file
set_wire_rc -signal -layer $wire_rc_layer
set_wire_rc -clock -layer $wire_rc_layer_clk
set_dont_use $dont_use

estimate_parasitics -placement

repair_design -slew_margin {slew_margin} -cap_margin {cap_margin}

repair_tie_fanout -separation $tie_separation $tielo_port
repair_tie_fanout -separation $tie_separation $tiehi_port

set_placement_padding -global -left $detail_place_pad -right $detail_place_pad
detailed_placement

report_worst_slack -min -digits 3
report_worst_slack -max -digits 3
report_tns -digits 3
report_check_types -max_slew -max_capacitance -max_fanout -violators
'''

CTS_FMT = '''
# ── Clock Tree Synthesis ──────────────────────────────────
repair_clock_inverters

clock_tree_synthesis -root_buf $cts_buffer -buf_list $cts_buffer \\
  -sink_clustering_enable \\
  -sink_clustering_max_diameter {cts_cluster_diameter}

repair_clock_nets
detailed_placement

set cts_db [make_result_file ${{design}}_${{platform}}_cts.db]
write_db $cts_db
'''

GLOBAL_ROUTING_FMT = '''
# ── Global Routing ────────────────────────────────────────
pin_access

set route_guide [make_result_file ${{design}}_${{platform}}.route_guide]
global_route -guide_file $route_guide \\
  -congestion_iterations {congestion_iters}

set verilog_file [make_result_file ${{design}}_${{platform}}.v]
write_verilog -remove_cells $filler_cells $verilog_file
'''

FINAL_REPORT_FMT = '''
# ── Final Report ───────────────────────────────────────────
report_checks -path_delay min_max -format full_clock_expanded \\
  -fields {{input_pin slew capacitance}} -digits 3
report_worst_slack -min -digits 3
report_worst_slack -max -digits 3
report_tns -digits 3
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3
report_clock_skew -digits 3
report_power -corner {power_corner}

report_floating_nets -verbose
report_design_area
'''


# =====================================================================
# RAW templates – plain TCL, no .format() needed, single { } braces
# =====================================================================

FLOORPLAN_RAW = '''
# ── Floorplan ──────────────────────────────────────────────
initialize_floorplan -site $site \\
  -die_area $die_area \\
  -core_area $core_area

source $tracks_file
remove_buffers
'''

MACRO_PLACEMENT_RAW = '''
# ── Macro Placement ───────────────────────────────────────
if { [have_macros] } {
  lassign $macro_place_halo halo_x halo_y
  set report_dir [make_result_file ${design}_${platform}_rtlmp]
  rtl_macro_placer -halo_width $halo_x -halo_height $halo_y \\
    -report_directory $report_dir
}
'''

TAPCELL_RAW = '''
# ── Tapcell ────────────────────────────────────────────────
eval tapcell $tapcell_args
'''

PDN_RAW = '''
# ── PDN ────────────────────────────────────────────────────
source $pdn_cfg
pdngen
'''

TIMING_REPAIR_RAW = '''
# ── Timing Repair ─────────────────────────────────────────
set_propagated_clock [all_clocks]
estimate_parasitics -placement

repair_timing -skip_gate_cloning

report_worst_slack -min -digits 3
report_worst_slack -max -digits 3
report_tns -digits 3
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3
'''

DETAILED_PLACEMENT_RAW = '''
# ── Detailed Placement ────────────────────────────────────
detailed_placement
'''

ANTENNA_REPAIR_GRT_RAW = '''
# ── Antenna Repair (post-GRT) ─────────────────────────────
repair_antennas -iterations 5
check_antennas
'''

DETAILED_ROUTING_RAW = '''
# ── Detailed Routing ──────────────────────────────────────
detailed_route \\
  -output_drc [make_result_file "${design}_${platform}_route_drc.rpt"] \\
  -output_maze [make_result_file "${design}_${platform}_maze.log"] \\
  -no_pin_access \\
  -verbose 0

write_guides [make_result_file "${design}_${platform}_output_guide.mod"]

set routed_db [make_result_file ${design}_${platform}_route.db]
write_db $routed_db

set routed_def [make_result_file ${design}_${platform}_route.def]
write_def $routed_def
'''

ANTENNA_REPAIR_DRT_RAW = '''
# ── Antenna Repair (post-DRT) ─────────────────────────────
set repair_antennas_iters 0
while { [check_antennas] && $repair_antennas_iters < 5 } {
  repair_antennas
  detailed_route \\
    -output_drc [make_result_file "${design}_${platform}_ant_fix_drc.rpt"] \\
    -output_maze [make_result_file "${design}_${platform}_ant_fix_maze.log"] \\
    -no_pin_access -verbose 0
  incr repair_antennas_iters
}
check_antennas

if { ![design_is_routed] } {
  error "Design has unrouted nets."
}
'''

FILLER_RAW = '''
# ── Filler Placement ──────────────────────────────────────
filler_placement $filler_cells
check_placement -verbose

set fill_db [make_result_file ${design}_${platform}_fill.db]
write_db $fill_db
'''

EXTRACTION_RAW = '''
# ── Extraction ─────────────────────────────────────────────
if { $rcx_rules_file != "" } {
  define_process_corner -ext_model_index 0 X
  extract_parasitics -ext_model_file $rcx_rules_file

  set spef_file [make_result_file ${design}_${platform}.spef]
  write_spef $spef_file
  read_spef $spef_file
} else {
  estimate_parasitics -global_routing
}
'''


# ── Map stage name → (template, needs_format) ─────────────────────────
_STAGE_TEMPLATES: dict[str, tuple[str, bool]] = {
    "floorplan":          (FLOORPLAN_RAW, False),
    "macro_placement":    (MACRO_PLACEMENT_RAW, False),
    "tapcell":            (TAPCELL_RAW, False),
    "pdn":                (PDN_RAW, False),
    "global_placement":   (GLOBAL_PLACEMENT_FMT, True),
    "io_placement":       ("", False),   # included in global_placement
    "resize_repair":      (RESIZE_REPAIR_FMT, True),
    "cts":                (CTS_FMT, True),
    "timing_repair":      (TIMING_REPAIR_RAW, False),
    "detailed_placement": (DETAILED_PLACEMENT_RAW, False),
    "global_routing":     (GLOBAL_ROUTING_FMT, True),
    "antenna_repair_grt": (ANTENNA_REPAIR_GRT_RAW, False),
    "detailed_routing":   (DETAILED_ROUTING_RAW, False),
    "antenna_repair_drt": (ANTENNA_REPAIR_DRT_RAW, False),
    "filler_placement":   (FILLER_RAW, False),
    "extraction":         (EXTRACTION_RAW, False),
    "final_report":       (FINAL_REPORT_FMT, True),
}


def _format_stage(stage: str, params: dict | None = None) -> str:
    """Render a stage template, applying .format() only when needed."""
    entry = _STAGE_TEMPLATES.get(stage)
    if entry is None:
        return f"# Unknown stage: {stage}\n"
    tpl, needs_fmt = entry
    if not tpl:
        return ""
    if needs_fmt and params:
        try:
            return tpl.format(**params)
        except KeyError:
            return tpl   # fall back to raw if keys missing
    if needs_fmt and not params:
        return tpl       # caller wants the raw FMT template
    return tpl


# ── LangChain Tools ───────────────────────────────────────────────────

@tool
def get_tcl_template(stage: str) -> str:
    """Return the TCL template for a given RTL-to-GDS flow stage.

    Args:
        stage: One of: floorplan, macro_placement, tapcell, pdn,
               global_placement, resize_repair, cts, timing_repair,
               detailed_placement, global_routing, antenna_repair_grt,
               detailed_routing, antenna_repair_drt, filler_placement,
               extraction, final_report.

    Returns:
        TCL template string. FMT templates contain Python {placeholders}.
    """
    entry = _STAGE_TEMPLATES.get(stage)
    if entry is None:
        return f"Unknown stage '{stage}'. Available: {list(_STAGE_TEMPLATES.keys())}"
    return entry[0]


@tool
def list_flow_stages() -> str:
    """List all RTL-to-GDS flow stages in order.

    Returns:
        JSON list of stage names with descriptions.
    """
    from openroad_agent.config import FLOW_STAGES, STAGE_DESCRIPTIONS
    stages = [{"name": s, "description": STAGE_DESCRIPTIONS.get(s, "")} for s in FLOW_STAGES]
    return json.dumps(stages, indent=2)


@tool
def assemble_full_flow_tcl(
    design: str,
    top_module: str,
    platform: str,
    synth_verilog: str,
    sdc_file: str,
    die_area: str,
    core_area: str,
    density: float = 0.3,
    pad: int = 2,
    slew_margin: int = 0,
    cap_margin: int = 0,
    cts_cluster_diameter: int = 100,
    congestion_iters: int = 100,
    power_corner: str = "default",
    extra_vars: str = "",
) -> str:
    """Assemble a complete RTL-to-GDS TCL flow script from templates.

    Args:
        design: Design name (e.g., "aes").
        top_module: Top module name (e.g., "aes_cipher_top").
        platform: PDK platform (e.g., "nangate45").
        synth_verilog: Path to synthesised Verilog file.
        sdc_file: Path to SDC timing constraints file.
        die_area: Die area coordinates "x0 y0 x1 y1".
        core_area: Core area coordinates "x0 y0 x1 y1".
        density: Global placement density target.
        pad: Placement padding in site widths.
        slew_margin: Slew margin for repair_design (%).
        cap_margin: Capacitance margin for repair_design (%).
        cts_cluster_diameter: CTS sink cluster diameter.
        congestion_iters: Global routing congestion iterations.
        power_corner: Power analysis corner name.
        extra_vars: Extra TCL variable definitions.

    Returns:
        Complete TCL script ready for OpenROAD execution.
    """
    from openroad_agent.config import OpenROADConfig, FLOW_STAGES
    cfg = OpenROADConfig()
    platform_vars = cfg.supported_platforms.get(platform, f"{platform}/{platform}.vars")

    fmt_params = dict(
        density=density,
        pad=pad,
        slew_margin=slew_margin,
        cap_margin=cap_margin,
        cts_cluster_diameter=cts_cluster_diameter,
        congestion_iters=congestion_iters,
        power_corner=power_corner,
    )

    parts = [
        HEADER_FMT.format(
            design=design,
            platform=platform,
            platform_vars=platform_vars,
        ),
        READ_DESIGN_FMT.format(
            design=design,
            top_module=top_module,
            synth_verilog=synth_verilog,
            sdc_file=sdc_file,
            die_area=die_area,
            core_area=core_area,
            extra_vars=extra_vars,
        ),
    ]

    for stage in FLOW_STAGES:
        parts.append(_format_stage(stage, fmt_params))

    return "\n".join(parts)


@tool
def assemble_partial_flow_tcl(
    design: str,
    top_module: str,
    platform: str,
    synth_verilog: str,
    sdc_file: str,
    die_area: str,
    core_area: str,
    stages: list[str],
    params: Optional[dict] = None,
) -> str:
    """Assemble a partial flow TCL script with selected stages only.

    Args:
        design: Design name.
        top_module: Top module name.
        platform: PDK platform name.
        synth_verilog: Path to synthesised Verilog.
        sdc_file: Path to SDC file.
        die_area: Die area "x0 y0 x1 y1".
        core_area: Core area "x0 y0 x1 y1".
        stages: List of stage names to include.
        params: Optional dict of parameters (density, pad, slew_margin, etc.).

    Returns:
        TCL script with only the requested stages.
    """
    from openroad_agent.config import OpenROADConfig
    cfg = OpenROADConfig()
    platform_vars = cfg.supported_platforms.get(platform, f"{platform}/{platform}.vars")

    p = params or {}
    fmt_params = dict(
        density=p.get("density", "$global_place_density"),
        pad=p.get("pad", "$global_place_pad"),
        slew_margin=p.get("slew_margin", "$slew_margin"),
        cap_margin=p.get("cap_margin", "$cap_margin"),
        cts_cluster_diameter=p.get("cts_cluster_diameter", "$cts_cluster_diameter"),
        congestion_iters=p.get("congestion_iters", 100),
        power_corner=p.get("power_corner", "default"),
    )

    parts = [
        HEADER_FMT.format(design=design, platform=platform, platform_vars=platform_vars),
        READ_DESIGN_FMT.format(
            design=design,
            top_module=top_module,
            synth_verilog=synth_verilog,
            sdc_file=sdc_file,
            die_area=die_area,
            core_area=core_area,
            extra_vars=p.get("extra_vars", ""),
        ),
    ]

    for stage in stages:
        parts.append(_format_stage(stage, fmt_params))

    return "\n".join(parts)
