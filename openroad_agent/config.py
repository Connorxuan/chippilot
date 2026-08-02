"""Configuration management for OpenROAD DeepAgents system."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Known model providers for auto-prefix detection ────────────────────
# Maps model-name prefix → LangChain provider string.
# For models not listed here, use the full "provider:model" form directly.
_PROVIDER_PREFIX_MAP: dict[str, str] = {
    "gemini": "google_genai",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "claude": "anthropic",
    "deepseek": "deepseek",
    "llama": "fireworks",
    "mixtral": "fireworks",
    "mistral": "mistralai",
    "command": "cohere",
    "qwen": "alibaba",
}

# ── Models exposed in the web UI selector ──────────────────────────────
SUPPORTED_MODELS = [
    "deepseek:deepseek-v4-pro",
    "google_genai:gemini-2.5-flash",
    "google_genai:gemini-2.5-pro",
]


def create_llm(model_name: str | None = None, **kwargs) -> Any:
    """Create an LLM instance with provider-specific configuration.

    For DeepSeek models, disables automatic thinking mode to avoid
    multi-turn reasoning_content preservation issues.
    """
    from langchain.chat_models.base import init_chat_model

    name = model_name or os.environ.get("OPENROAD_LLM_MODEL", "deepseek:deepseek-v4-pro")
    name = _normalize_model_name(name)

    if name.startswith("deepseek:"):
        if "model_kwargs" not in kwargs:
            kwargs["model_kwargs"] = {}
        kwargs["model_kwargs"].setdefault("extra_body", {})
        kwargs["model_kwargs"]["extra_body"].setdefault("thinking", {"type": "disabled"})

    return init_chat_model(name, **kwargs)


def _normalize_model_name(name: str) -> str:
    """Ensure a model name has the correct LangChain provider prefix.

    If the string already contains ``:`` or ``/``, it is returned as-is.
    Otherwise we try to guess the provider from the model name prefix and
    prepend ``provider:``.
    """
    if ":" in name or "/" in name:
        return name
    for prefix, provider in _PROVIDER_PREFIX_MAP.items():
        if name.startswith(prefix):
            return f"{provider}:{name}"
    return name


@dataclass
class OpenROADConfig:
    """Master configuration for the OpenROAD agent system."""

    # ── Paths ──────────────────────────────────────────────────────────
    openroad_bin: str = os.environ.get("OPENROAD_BIN", "openroad")
    yosys_bin: str = os.environ.get("YOSYS_BIN", "yosys")
    klayout_bin: str = os.environ.get("KLAYOUT_BIN", "klayout")
    openroad_root: str = os.environ.get(
        "OPENROAD_ROOT",
        str(Path(__file__).resolve().parent.parent / "OpenROAD"),
    )
    work_dir: str = os.environ.get(
        "OPENROAD_WORK_DIR",
        str(Path(__file__).resolve().parent.parent / "openroad_work"),
    )
    test_dir: str = ""  # populated in __post_init__

    # ── LLM ────────────────────────────────────────────────────────────
    model_name: str = os.environ.get("OPENROAD_LLM_MODEL", "deepseek:deepseek-v4-pro")
    temperature: float = float(os.environ.get("OPENROAD_LLM_TEMPERATURE", "0.1"))
    api_key: str = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("GOOGLE_API_KEY", ""))

    # ── Supported PDKs & their variable files (relative to test_dir) ──
    supported_platforms: dict[str, str] = field(default_factory=lambda: {
        "nangate45": "Nangate45/Nangate45.vars",
        "asap7": "asap7/asap7.vars",
        "sky130hd": "sky130hd/sky130hd.vars",
        "sky130hs": "sky130hs/sky130hs.vars",
    })

    # ── Execution mode ─────────────────────────────────────────────────
    # "local" = subprocess on this machine
    # "ray"   = submit to remote Ray cluster
    execution_mode: str = os.environ.get("OPENROAD_EXEC_MODE", "ray")
    ray_address: str = os.environ.get(
        "RAY_ADDRESS", "ray://10.0.13.181:10001"
    )

    # ── Exploration defaults ───────────────────────────────────────────
    max_exploration_iterations: int = int(
        os.environ.get("OPENROAD_MAX_EXPLORE_ITERS", "20")
    )
    parallel_runs: int = int(os.environ.get("OPENROAD_PARALLEL_RUNS", "4"))

    def __post_init__(self) -> None:
        # Resolve all paths to absolute so they work regardless of cwd
        self.openroad_root = str(Path(self.openroad_root).resolve())
        self.work_dir = str(Path(self.work_dir).resolve())
        self.test_dir = os.path.join(self.openroad_root, "test")
        os.makedirs(self.work_dir, exist_ok=True)
        # Ensure model_name has provider prefix for LangChain init_chat_model
        self.model_name = _normalize_model_name(self.model_name)

    def platform_vars_path(self, platform: str) -> str:
        """Return absolute path to the platform .vars file."""
        rel = self.supported_platforms.get(platform)
        if not rel:
            raise ValueError(
                f"Unknown platform '{platform}'. "
                f"Choose from: {list(self.supported_platforms)}"
            )
        return os.path.join(self.test_dir, rel)


# ── RTL-to-GDS flow stage definitions ─────────────────────────────────
FLOW_STAGES = [
    "floorplan",
    "macro_placement",
    "tapcell",
    "pdn",
    "global_placement",
    "io_placement",
    "resize_repair",
    "cts",
    "timing_repair",
    "detailed_placement",
    "global_routing",
    "antenna_repair_grt",
    "detailed_routing",
    "antenna_repair_drt",
    "filler_placement",
    "extraction",
    "final_report",
    "gds_generation",
]

STAGE_DESCRIPTIONS: dict[str, str] = {
    "floorplan": "Initialize floorplan: die/core area, site, tracks",
    "macro_placement": "Place macro blocks (if any) using RTL macro placer",
    "tapcell": "Insert tap cells and endcaps",
    "pdn": "Build power distribution network",
    "global_placement": "Global placement with density targeting",
    "io_placement": "Place I/O pins on specified metal layers",
    "resize_repair": "Repair max-slew/cap/fanout, tie-fanout, buffer insertion",
    "cts": "Clock tree synthesis with buffer insertion and clustering",
    "timing_repair": "Repair setup/hold timing violations",
    "detailed_placement": "Legalize cell placement",
    "global_routing": "Global routing with congestion control",
    "antenna_repair_grt": "Repair antenna violations after global routing",
    "detailed_routing": "Detailed routing with DRC fixing",
    "antenna_repair_drt": "Repair antenna violations after detailed routing",
    "filler_placement": "Insert filler cells",
    "extraction": "Parasitic extraction (RC extraction or estimated)",
    "final_report": "Generate timing/power/area reports and metrics",
    "gds_generation": "Generate GDSII layout from routed DEF using KLayout",
}
