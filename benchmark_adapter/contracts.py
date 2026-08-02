"""Machine-checkable public portions of Level 1-5 task contracts."""

from __future__ import annotations

from typing import Any


def validate_execution_contract(
    level: int,
    contract: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    calls = [event.get("tool") for event in events if event.get("type") == "tool_started"]
    required = contract.get("required_capabilities", [])
    missing = [name for name in required if name not in calls]
    if missing:
        violations.append({"type": "required_capabilities_missing", "capabilities": missing})

    positions: dict[str, int] = {}
    for index, name in enumerate(calls):
        positions.setdefault(str(name), index)
    for rule in contract.get("precedence", contract.get("precedence_constraints", [])):
        before = after = None
        if isinstance(rule, str) and " before " in rule:
            before, after = rule.split(" before ", 1)
        elif isinstance(rule, (list, tuple)) and len(rule) == 2:
            before, after = rule
        elif isinstance(rule, dict):
            before, after = rule.get("before"), rule.get("after")
        if before and after and (
            before not in positions or after not in positions or positions[before] >= positions[after]
        ):
            violations.append({"type": "precedence_violation", "before": before, "after": after})

    if level == 1 and contract.get("atomic", True):
        primary_calls = [event for event in events if event.get("type") == "tool_started" and event.get("primary")]
        if len(primary_calls) > 1:
            violations.append({"type": "atomicity_violation", "primary_tool_calls": len(primary_calls)})
    return violations
