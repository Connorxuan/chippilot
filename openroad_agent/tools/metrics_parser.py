"""Tool: parse and compare OpenROAD metrics / timing reports."""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from langchain_core.tools import tool


@tool
def parse_metrics_file(metrics_path: str) -> str:
    """Parse an OpenROAD metrics JSON file and return a summary.

    Args:
        metrics_path: Absolute path to the .metrics JSON file.

    Returns:
        Pretty-formatted JSON of all metrics.
    """
    try:
        with open(metrics_path) as f:
            data = json.load(f)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error parsing {metrics_path}: {e}"


@tool
def compare_metrics(
    metrics_a_json: str,
    metrics_b_json: str,
    label_a: str = "A",
    label_b: str = "B",
) -> str:
    """Compare two sets of OpenROAD metrics and highlight differences.

    Args:
        metrics_a_json: JSON string of first metrics set.
        metrics_b_json: JSON string of second metrics set.
        label_a: Label for first set.
        label_b: Label for second set.

    Returns:
        Markdown table comparing the two metric sets.
    """
    try:
        a = json.loads(metrics_a_json)
        b = json.loads(metrics_b_json)
    except json.JSONDecodeError as e:
        return f"JSON parse error: {e}"

    all_keys = sorted(set(list(a.keys()) + list(b.keys())))
    lines = [f"| Metric | {label_a} | {label_b} | Delta |", "| --- | --- | --- | --- |"]
    for k in all_keys:
        va = a.get(k, "N/A")
        vb = b.get(k, "N/A")
        delta = ""
        try:
            fa, fb = float(va), float(vb)
            delta = f"{fb - fa:+.4f}"
        except (ValueError, TypeError):
            delta = "—"
        lines.append(f"| {k} | {va} | {vb} | {delta} |")
    return "\n".join(lines)


@tool
def extract_timing_summary(openroad_stdout: str) -> str:
    """Extract key timing metrics from OpenROAD stdout.

    Args:
        openroad_stdout: Raw stdout output from an OpenROAD run.

    Returns:
        JSON with worst_slack_min, worst_slack_max, tns, clock_skew,
        utilization, design_area, drv_count, antenna_errors.
    """
    result: dict = {}

    patterns = {
        "worst_slack_min": r"worst slack -min\s+([-\d.]+)",
        "worst_slack_max": r"worst slack -max\s+([-\d.]+)",
        "tns": r"tns\s+([-\d.]+)",
        "clock_skew": r"clock skew\s+([-\d.]+)",
        "design_area": r"Design area\s+([\d.]+)",
        "utilization": r"utilization\s+([\d.]+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, openroad_stdout, re.IGNORECASE)
        if m:
            try:
                result[key] = float(m.group(1))
            except ValueError:
                result[key] = m.group(1)

    # Count DRVs
    drv_match = re.search(r"Number of violations\s*[:=]\s*(\d+)", openroad_stdout)
    if drv_match:
        result["drv_count"] = int(drv_match.group(1))

    return json.dumps(result, indent=2)


@tool
def check_metrics_limits(
    metrics_json: str,
    limits_path: str,
) -> str:
    """Check if current metrics satisfy the limits file.

    Args:
        metrics_json: JSON string of current run metrics.
        limits_path: Path to the .metrics_limits JSON file.

    Returns:
        JSON with pass/fail status and list of violations.
    """
    try:
        metrics = json.loads(metrics_json)
        with open(limits_path) as f:
            limits = json.load(f)
    except Exception as e:
        return json.dumps({"error": str(e)})

    violations = []
    for key, limit_val in limits.items():
        if key not in metrics:
            continue
        try:
            actual = float(metrics[key])
            limit = float(limit_val)
            # For slack metrics (higher=better), actual should be >= limit
            if "slack" in key.lower():
                if actual < limit:
                    violations.append({
                        "metric": key,
                        "actual": actual,
                        "limit": limit,
                        "direction": "should be >=",
                    })
            # For error/violation counts, actual should be <= limit
            elif "error" in key.lower() or "drv" in key.lower() or "violation" in key.lower():
                if actual > limit:
                    violations.append({
                        "metric": key,
                        "actual": actual,
                        "limit": limit,
                        "direction": "should be <=",
                    })
            # For tns (closer to 0 is better), absolute value should be <= limit
            elif "tns" in key.lower():
                if abs(actual) > abs(limit):
                    violations.append({
                        "metric": key,
                        "actual": actual,
                        "limit": limit,
                        "direction": "|actual| should be <= |limit|",
                    })
        except (ValueError, TypeError):
            pass

    return json.dumps({
        "pass": len(violations) == 0,
        "violation_count": len(violations),
        "violations": violations,
    }, indent=2)
