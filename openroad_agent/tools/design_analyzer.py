"""Tool: analyse design inputs, results, and suggest improvements."""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from langchain_core.tools import tool

from openroad_agent.config import OpenROADConfig


def _get_cfg() -> OpenROADConfig:
    """Lazy config accessor — ensures env is loaded before first use."""
    global _cfg_instance
    if _cfg_instance is None:
        _cfg_instance = OpenROADConfig()
    return _cfg_instance


_cfg_instance: OpenROADConfig | None = None


@tool
def list_available_designs() -> str:
    """List test designs available in OpenROAD/test directory.

    Returns:
        JSON list of design objects with name, platform, files.
    """
    test_dir = _get_cfg().test_dir
    designs = []
    if not os.path.isdir(test_dir):
        return json.dumps({"error": f"Test dir not found: {test_dir}"})

    for fname in sorted(os.listdir(test_dir)):
        if fname.endswith(".tcl") and not fname.startswith(("helpers", "flow")):
            # e.g., aes_nangate45.tcl → design=aes, platform=nangate45
            base = fname[:-4]
            parts = base.rsplit("_", 1)
            if len(parts) == 2:
                design_name, platform = parts
            else:
                design_name, platform = base, "unknown"

            entry: dict = {"file": fname, "design": design_name, "platform": platform}

            # Check for companion files
            for ext in (".v", ".sdc", ".metrics", ".metrics_limits"):
                companion = os.path.join(test_dir, base + ext)
                if os.path.exists(companion):
                    entry[ext.lstrip(".")] = base + ext

            designs.append(entry)

    return json.dumps(designs, indent=2)


@tool
def read_design_tcl(design_tcl_path: str) -> str:
    """Read and return the content of a design TCL file.

    Args:
        design_tcl_path: Path to the design .tcl file (relative to test/ or absolute).

    Returns:
        The TCL file content.
    """
    if not os.path.isabs(design_tcl_path):
        design_tcl_path = os.path.join(_get_cfg().test_dir, design_tcl_path)
    try:
        with open(design_tcl_path) as f:
            return f.read()
    except FileNotFoundError:
        return f"File not found: {design_tcl_path}"


@tool
def read_platform_vars(platform: str) -> str:
    """Read platform variable definitions for a PDK.

    Args:
        platform: Platform name (e.g., "nangate45", "sky130hd").

    Returns:
        Contents of the .vars file.
    """
    try:
        path = _get_cfg().platform_vars_path(platform)
        with open(path) as f:
            return f.read()
    except (ValueError, FileNotFoundError) as e:
        return str(e)


@tool
def list_supported_platforms() -> str:
    """List all supported PDK platforms.

    Returns:
        JSON list of platform names.
    """
    return json.dumps(list(_get_cfg().supported_platforms.keys()))


@tool
def analyse_timing_report(report_text: str) -> str:
    """Analyse a timing report and provide structured summary.

    Args:
        report_text: Raw timing report text from OpenROAD.

    Returns:
        JSON summary with WNS, TNS, violated paths, suggestions.
    """
    summary: dict = {"paths": [], "violations": [], "suggestions": []}

    # Extract worst slack
    wns_match = re.search(r"worst slack\s+([-\d.]+)", report_text, re.IGNORECASE)
    if wns_match:
        wns = float(wns_match.group(1))
        summary["worst_negative_slack"] = wns
        if wns < 0:
            summary["violations"].append(f"Timing violated: WNS = {wns}")
            if wns < -0.5:
                summary["suggestions"].append(
                    "Consider increasing clock period, reducing logic depth, "
                    "or using faster cells."
                )
            else:
                summary["suggestions"].append(
                    "Minor timing violation. Try repair_timing with "
                    "tighter constraints or adjust placement density."
                )

    # Extract TNS
    tns_match = re.search(r"tns\s+([-\d.]+)", report_text, re.IGNORECASE)
    if tns_match:
        summary["total_negative_slack"] = float(tns_match.group(1))

    # Slew violations
    slew_match = re.search(r"max_slew.*?(\d+)\s+violation", report_text, re.IGNORECASE)
    if slew_match:
        count = int(slew_match.group(1))
        summary["slew_violations"] = count
        if count > 0:
            summary["suggestions"].append(
                f"{count} slew violations. Consider adjusting slew_margin or buffer insertion."
            )

    return json.dumps(summary, indent=2)


@tool
def suggest_parameter_ranges(
    platform: str,
    design_metrics_json: str,
) -> str:
    """Suggest parameter ranges for design space exploration based on current metrics.

    Args:
        platform: PDK platform name.
        design_metrics_json: JSON string of current design metrics.

    Returns:
        JSON with suggested parameter ranges for exploration.
    """
    try:
        metrics = json.loads(design_metrics_json)
    except json.JSONDecodeError:
        metrics = {}

    suggestions: dict = {
        "density": {
            "description": "Global placement density",
            "range": [0.2, 0.8],
            "step": 0.05,
            "current_recommendation": 0.3,
        },
        "pad": {
            "description": "Placement padding (site widths)",
            "range": [0, 4],
            "step": 1,
            "current_recommendation": 2,
        },
        "cts_cluster_diameter": {
            "description": "CTS sink clustering diameter",
            "range": [50, 200],
            "step": 25,
            "current_recommendation": 100,
        },
        "slew_margin": {
            "description": "Slew margin for repair_design (%)",
            "range": [0, 40],
            "step": 5,
            "current_recommendation": 0,
        },
        "cap_margin": {
            "description": "Capacitance margin for repair_design (%)",
            "range": [0, 40],
            "step": 5,
            "current_recommendation": 0,
        },
        "congestion_iters": {
            "description": "Global routing congestion iterations",
            "range": [50, 200],
            "step": 25,
            "current_recommendation": 100,
        },
    }

    # Adjust recommendations based on metrics
    wns = metrics.get("DRT::worst_slack_max") or metrics.get("RSZ::worst_slack_max")
    if wns is not None:
        try:
            wns_val = float(wns)
            if wns_val < -0.5:
                suggestions["density"]["current_recommendation"] = 0.25
                suggestions["slew_margin"]["current_recommendation"] = 20
                suggestions["cap_margin"]["current_recommendation"] = 20
        except ValueError:
            pass

    util = metrics.get("DPL::utilization")
    if util is not None:
        try:
            util_val = float(util)
            if util_val > 80:
                suggestions["density"]["range"] = [0.6, 0.95]
                suggestions["density"]["current_recommendation"] = 0.7
        except ValueError:
            pass

    return json.dumps(suggestions, indent=2)
