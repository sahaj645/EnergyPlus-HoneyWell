"""Process-wide configuration, resolved from environment variables with sane defaults.

Deliberately a plain Pydantic model rather than ``pydantic-settings``: one dependency fewer,
and the whole surface fits on a screen. Construct once at startup and pass it down; do not
read ``os.environ`` from anywhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


class Settings(BaseModel):
    """Runtime configuration for a HIVE process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- paths ---
    repo_root: Path = Field(default=REPO_ROOT)
    simulation_dir: Path = Field(default=REPO_ROOT / "simulation")
    data_dir: Path = Field(default=REPO_ROOT / "data")
    db_path: Path = Field(default=REPO_ROOT / "hive.sqlite")

    # --- EnergyPlus ---
    idf_path: Path = Field(default=REPO_ROOT / "simulation" / "baseline.idf")
    epw_path: Path = Field(default=REPO_ROOT / "simulation" / "weather.epw")
    output_dir: Path = Field(default=REPO_ROOT / "simulation" / "out")

    # --- planner ---
    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen2.5:7b-instruct-q4_K_M")
    planner_timeout_s: float = Field(default=30.0, gt=0)
    plan_interval_minutes: int = Field(default=15, gt=0)

    # --- telemetry ---
    # Batching is counted in timesteps, not rows: one timestep emits one row per zone, and the
    # flush trigger has to be tied to the callback's cadence to bound how much is at risk.
    telemetry_flush_every_timesteps: int = Field(default=12, gt=0)

    # --- logging ---
    log_level: str = Field(default="INFO")

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment, falling back to the defaults above."""
        return cls(
            db_path=Path(_env("HIVE_DB_PATH", str(REPO_ROOT / "hive.sqlite"))),
            idf_path=Path(_env("HIVE_IDF_PATH", str(REPO_ROOT / "simulation" / "baseline.idf"))),
            epw_path=Path(_env("HIVE_EPW_PATH", str(REPO_ROOT / "simulation" / "weather.epw"))),
            output_dir=Path(_env("HIVE_OUTPUT_DIR", str(REPO_ROOT / "simulation" / "out"))),
            ollama_host=_env("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=_env("HIVE_OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M"),
            planner_timeout_s=float(_env("HIVE_PLANNER_TIMEOUT_S", "30")),
            plan_interval_minutes=int(_env("HIVE_PLAN_INTERVAL_MIN", "15")),
            telemetry_flush_every_timesteps=int(_env("HIVE_TELEMETRY_FLUSH_STEPS", "12")),
            log_level=_env("HIVE_LOG_LEVEL", "INFO"),
        )
