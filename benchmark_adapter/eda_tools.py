"""Lite open-source EDA and benchmark-management tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool, tool

from openroad_agent.tools.openroad_runner import run_openroad_tcl
from openroad_agent.tools.session_manager import SessionManager
from openroad_agent.tools.yosys_runner import run_yosys_synthesis


def _root() -> Path:
    session = SessionManager.get_current()
    if not session:
        raise RuntimeError("No active benchmark session")
    return Path(session.base_dir).resolve()


def _inside(value: str, *, exists: bool = True) -> Path:
    root = _root()
    supplied = Path(value)
    target = supplied.resolve() if supplied.is_absolute() else (root / supplied).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path is outside the active benchmark session: {value}")
    if exists and not target.exists():
        raise FileNotFoundError(value)
    return target


def _binary(name: str) -> str:
    override = os.environ.get(f"{name.upper()}_BIN")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(f"Required executable is not available: {name}")


def _run(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    cpu_started = os.times()
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        cpu_finished = os.times()
        cpu_seconds = (cpu_finished.children_user + cpu_finished.children_system) - (
            cpu_started.children_user + cpu_started.children_system
        )
        return {
            "status": "success" if completed.returncode == 0 else "failed",
            "success": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-8000:],
            "effective_parameters": {"argv": argv},
            "artifacts": [],
            "metrics": {},
            "warnings": [],
            "runtime_seconds": round(time.monotonic() - started, 3),
            "resource_usage": {"cpu_core_hours": max(0.0, cpu_seconds) / 3600},
            "provenance": {},
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "failed", "success": False, "exit_code": None,
            "stdout": str(exc.stdout or "")[-12000:], "stderr": "Tool timeout",
            "effective_parameters": {"argv": argv}, "artifacts": [], "metrics": {},
            "warnings": [], "runtime_seconds": round(time.monotonic() - started, 3),
            "resource_usage": {}, "provenance": {},
        }


@tool
def synthesize(synth_script: str, run_label: str = "synthesize", timeout_seconds: int = 1800) -> str:
    """Run a structured Yosys synthesis operation."""
    return run_yosys_synthesis.invoke(
        {"synth_script": synth_script, "run_label": run_label, "timeout_seconds": timeout_seconds}
    )


def _openroad_stage(tcl_script: str, run_label: str, timeout_seconds: int) -> str:
    return run_openroad_tcl.invoke(
        {"tcl_script": tcl_script, "run_label": run_label, "timeout_seconds": timeout_seconds}
    )


def _stage_tool(name: str, description: str) -> StructuredTool:
    def execute(tcl_script: str, run_label: str = name, timeout_seconds: int = 3600) -> str:
        return _openroad_stage(tcl_script, run_label, timeout_seconds)

    return StructuredTool.from_function(func=execute, name=name, description=description)


report_timing = _stage_tool("report_timing", "Run OpenROAD/OpenSTA timing analysis with explicit corners and path filters.")
initialize_floorplan = _stage_tool("initialize_floorplan", "Initialize an OpenROAD floorplan using explicit task parameters.")
global_place = _stage_tool("global_place", "Run OpenROAD global placement with explicit density and mode parameters.")
detailed_place = _stage_tool("detailed_place", "Run OpenROAD detailed placement and legality checks.")
clock_tree_synthesis = _stage_tool("clock_tree_synthesis", "Run OpenROAD clock-tree synthesis.")
global_route = _stage_tool("global_route", "Run OpenROAD global routing and congestion reporting.")
detailed_route = _stage_tool("detailed_route", "Run OpenROAD detailed routing and DRC reporting.")
extract_parasitics = _stage_tool("extract_parasitics", "Run OpenROAD parasitic extraction and produce SPEF.")
report_power = _stage_tool("report_power", "Run OpenROAD/OpenSTA power analysis with explicit activity inputs.")


@tool
def simulate(
    verilog_files: list[str],
    top_module: str,
    simulator: str = "iverilog",
    plusargs: list[str] | None = None,
    timeout_seconds: int = 300,
) -> str:
    """Compile and run a Verilog/SystemVerilog simulation in the active session."""
    files = [str(_inside(item)) for item in verilog_files]
    run_dir = _root() / "simulation"
    run_dir.mkdir(exist_ok=True)
    if simulator == "iverilog":
        binary = run_dir / "simulation.vvp"
        compile_result = _run([_binary("iverilog"), "-g2012", "-s", top_module, "-o", str(binary), *files], run_dir, timeout_seconds)
        if not compile_result["success"]:
            return json.dumps(compile_result)
        result = _run([_binary("vvp"), str(binary), *(plusargs or [])], run_dir, timeout_seconds)
        result["artifacts"] = [str(binary)]
    elif simulator == "verilator":
        output_dir = run_dir / "verilator"
        result = _run(
            [_binary("verilator"), "--binary", "--timing", "--top-module", top_module, "--Mdir", str(output_dir), *files],
            run_dir,
            timeout_seconds,
        )
        binary = output_dir / f"V{top_module}"
        if result["success"]:
            result = _run([str(binary), *(plusargs or [])], run_dir, timeout_seconds)
            result["artifacts"] = [str(binary)]
    else:
        raise ValueError("simulator must be iverilog or verilator")
    result["metrics"] = {"test_passed": result["success"]}
    return json.dumps(result)


@tool
def run_regression(tests: list[dict[str, Any]], max_parallelism: int = 1) -> str:
    """Run a fixed list of simulation specifications and return a pass/fail summary."""
    results = []
    for spec in tests:
        result = json.loads(simulate.invoke(spec))
        results.append({"name": spec.get("name", spec.get("top_module", "test")), **result})
    passed = sum(bool(item.get("success")) for item in results)
    return json.dumps({
        "status": "success" if passed == len(results) else "failed",
        "effective_parameters": {"max_parallelism": max_parallelism, "tests": len(tests)},
        "results": results,
        "metrics": {"passed": passed, "failed": len(results) - passed, "pass_rate": passed / len(results) if results else 0},
        "artifacts": [], "warnings": [], "runtime_seconds": sum(item.get("runtime_seconds", 0) for item in results),
        "resource_usage": {}, "provenance": {},
    })


@tool
def check_equivalence(
    reference_verilog: str,
    implementation_verilog: str,
    top_module: str,
    timeout_seconds: int = 600,
) -> str:
    """Check RTL/netlist equivalence with the Yosys equivalence flow."""
    reference = _inside(reference_verilog)
    implementation = _inside(implementation_verilog)
    run_dir = _root() / "equivalence"
    run_dir.mkdir(exist_ok=True)
    script = run_dir / "equivalence.ys"
    script.write_text(
        f"read_verilog -sv {reference}\nprep -top {top_module}\nrename {top_module} gold\n"
        f"read_verilog -sv {implementation}\nprep -top {top_module}\nrename {top_module} gate\n"
        "equiv_make gold gate equiv\nhierarchy -top equiv\nequiv_simple\nequiv_status -assert\n",
        encoding="utf-8",
    )
    result = _run([_binary("yosys"), "-s", str(script)], run_dir, timeout_seconds)
    result["artifacts"] = [str(script)]
    result["metrics"] = {"equivalent": result["success"]}
    return json.dumps(result)


@tool
def run_drc(layout_file: str, rule_deck: str, report_file: str = "drc_report.xml", timeout_seconds: int = 1800) -> str:
    """Run a task-supplied KLayout DRC rule deck."""
    layout = _inside(layout_file)
    deck = _inside(rule_deck)
    report = _inside(report_file, exists=False)
    report.parent.mkdir(parents=True, exist_ok=True)
    result = _run([_binary("klayout"), "-b", "-r", str(deck), "-rd", f"input={layout}", "-rd", f"report={report}"], report.parent, timeout_seconds)
    result["artifacts"] = [str(report)] if report.exists() else []
    result["metrics"] = {"report_exists": report.exists()}
    return json.dumps(result)


@tool
def run_lvs(
    layout_netlist: str,
    schematic_netlist: str,
    setup_file: str,
    top_module: str,
    backend: str = "auto",
    timeout_seconds: int = 1800,
) -> str:
    """Run LVS with Netgen, or a task-supplied KLayout LVS script."""
    layout = _inside(layout_netlist)
    schematic = _inside(schematic_netlist)
    setup = _inside(setup_file)
    report = _root() / "lvs_report.log"
    if backend == "auto":
        try:
            _binary("netgen")
        except FileNotFoundError:
            backend = "klayout"
        else:
            backend = "netgen"
    if backend == "netgen":
        argv = [_binary("netgen"), "-batch", "lvs", f"{layout} {top_module}", f"{schematic} {top_module}", str(setup), str(report)]
    elif backend == "klayout":
        argv = [
            _binary("klayout"), "-b", "-r", str(setup),
            "-rd", f"layout={layout}", "-rd", f"schematic={schematic}",
            "-rd", f"top={top_module}", "-rd", f"report={report}",
        ]
    else:
        raise ValueError("backend must be auto, netgen, or klayout")
    result = _run(argv, report.parent, timeout_seconds)
    result["artifacts"] = [str(report)] if report.exists() else []
    result["metrics"] = {"matched": result["success"]}
    return json.dumps(result)


@tool
def query_waveform(waveform_file: str, signal_names: list[str], max_matches: int = 200) -> str:
    """Query declarations and value changes for selected signals in a text VCD."""
    path = _inside(waveform_file)
    matches = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if any(name in line for name in signal_names):
                matches.append({"line": number, "text": line.rstrip()})
                if len(matches) >= max_matches:
                    break
    return json.dumps({"status": "success", "matches": matches, "metrics": {"match_count": len(matches)}})


@tool
def apply_text_replacement(file_path: str, old_text: str, new_text: str, expected_count: int = 1) -> str:
    """Apply a bounded text replacement to a mutable file in the active session."""
    path = _inside(file_path)
    content = path.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count != expected_count:
        raise ValueError(f"Expected {expected_count} matches, found {count}")
    path.write_text(content.replace(old_text, new_text), encoding="utf-8")
    return json.dumps({"status": "success", "effective_parameters": {"path": str(path), "replacements": count}, "artifacts": [str(path)], "metrics": {}})


@tool
def create_checkpoint(source_path: str, checkpoint_name: str) -> str:
    """Copy a design database or artifact into the run checkpoint registry."""
    source = _inside(source_path)
    destination = _root() / "checkpoints" / Path(checkpoint_name).name
    destination.parent.mkdir(exist_ok=True)
    shutil.copy2(source, destination)
    return json.dumps({"status": "success", "checkpoint": str(destination), "artifacts": [str(destination)]})


@tool
def create_trial(trial_id: str, parameters: dict[str, Any], parent_checkpoint: str = "") -> str:
    """Register an isolated Level-5 design-space trial."""
    safe_id = "".join(ch for ch in trial_id if ch.isalnum() or ch in "_-")[:80]
    if not safe_id:
        raise ValueError("Invalid trial_id")
    directory = _root() / "trials" / safe_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {"trial_id": safe_id, "parameters": parameters, "parent_checkpoint": parent_checkpoint, "status": "created"}
    (directory / "trial.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return json.dumps({"status": "success", "trial": record, "artifacts": [str(directory / 'trial.json')]})


@tool
def register_solution(solution_id: str, artifacts: list[str], metrics: dict[str, Any], feasible: bool) -> str:
    """Register an anytime or final Level-5 candidate solution."""
    resolved = [str(_inside(item)) for item in artifacts]
    path = _root() / "solutions.jsonl"
    record = {"solution_id": solution_id, "artifacts": resolved, "metrics": metrics, "feasible": feasible, "registered_at": time.time()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return json.dumps({"status": "success", "solution": record, "artifacts": [str(path)]})


@tool
def cross_probe(search_term: str, files: list[str], max_matches: int = 200) -> str:
    """Cross-probe a net, instance, rule, or signal across logs and reports."""
    matches = []
    for file_name in files:
        path = _inside(file_name)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if search_term.lower() in line.lower():
                    matches.append({"file": str(path), "line": line_number, "text": line.rstrip()})
                    if len(matches) >= max_matches:
                        break
        if len(matches) >= max_matches:
            break
    return json.dumps({"status": "success", "matches": matches, "metrics": {"match_count": len(matches)}})


@tool
def rollback_checkpoint(checkpoint_path: str, destination_path: str) -> str:
    """Restore a registered checkpoint to a mutable workspace destination."""
    checkpoint = _inside(checkpoint_path)
    destination = _inside(destination_path, exists=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(checkpoint, destination)
    return json.dumps({"status": "success", "artifacts": [str(destination)], "metrics": {"rollback": True}})


@tool
def register_trial_result(trial_id: str, metrics: dict[str, Any], status: str, artifacts: list[str] | None = None) -> str:
    """Finish a Level-5 trial and persist its metrics and output artifacts."""
    directory = _inside(f"trials/{trial_id}")
    resolved = [str(_inside(item)) for item in (artifacts or [])]
    record = {"trial_id": trial_id, "metrics": metrics, "status": status, "artifacts": resolved}
    path = directory / "result.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return json.dumps({"status": "success", "trial_result": record, "artifacts": [str(path), *resolved], "metrics": metrics})


@tool
def compute_pareto_front(
    candidates: list[dict[str, Any]], minimize: list[str], maximize: list[str] | None = None
) -> str:
    """Return the non-dominated candidate set for named metric objectives."""
    maximize = maximize or []

    def dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
        no_worse = all(left["metrics"][key] <= right["metrics"][key] for key in minimize)
        no_worse &= all(left["metrics"][key] >= right["metrics"][key] for key in maximize)
        better = any(left["metrics"][key] < right["metrics"][key] for key in minimize)
        better |= any(left["metrics"][key] > right["metrics"][key] for key in maximize)
        return no_worse and better

    front = [candidate for candidate in candidates if not any(dominates(other, candidate) for other in candidates if other is not candidate)]
    return json.dumps({"status": "success", "solutions": front, "metrics": {"non_dominated_count": len(front)}})


BENCHMARK_TOOLS = [
    simulate, run_regression, synthesize, check_equivalence, report_timing,
    initialize_floorplan, global_place, detailed_place, clock_tree_synthesis,
    global_route, detailed_route, extract_parasitics, run_drc, run_lvs, report_power, query_waveform,
    apply_text_replacement, create_checkpoint, create_trial, register_solution, cross_probe,
    rollback_checkpoint, register_trial_result, compute_pareto_front,
]
