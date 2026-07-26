"""Import smoke test: the package tree is importable without EnergyPlus, Ollama or a DB.

Deliberately shallow. Its job is to fail loudly on a broken package layout, a circular import,
or a module that reaches for the EnergyPlus runtime API at import time - not to test
behaviour, of which there is none yet.
"""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "agent",
    "common",
    "dashboard",
    "experiments",
    "guardian",
    "mcp_server",
    "simulation",
]

# Modules that must import cleanly on a machine with no EnergyPlus and no model runtime.
MODULES = [
    "agent.bus",
    "agent.cache",
    "agent.digest",
    "agent.events",
    "agent.ollama_client",
    "agent.plan_cache",
    "agent.planner",
    "agent.prompts",
    "agent.scheduler",
    "common.config",
    "common.eplus_path",
    "common.generated_enums",
    "common.log",
    "common.models",
    "common.planslot",
    "common.store",
    "experiments.ab_harness",
    "experiments.endurance",
    "experiments.kpi_extract",
    "experiments.kpis",
    "experiments.mcp_exercise",
    "experiments.smoke_llm_loop",
    "experiments.smoke_roundtrip",
    "guardian.core",
    "guardian.executor",
    "guardian.fallback",
    "guardian.limits",
    "guardian.supervisor",
    "guardian.watchdog",
    "mcp_server.providers",
    "mcp_server.server",
    "mcp_server.tools",
    "simulation.fetch_assets",
    "simulation.idf_io",
    "simulation.patching",
    "simulation.prepare_idf",
    "simulation.receding",
    "simulation.run_baseline",
    "simulation.snapshots",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_eplus_path_is_soft_when_energyplus_absent() -> None:
    """Importing the path helper must never fail just because EnergyPlus is missing."""
    from common import eplus_path

    assert eplus_path.ENV_VAR == "ENERGYPLUS_DIR"
    assert eplus_path.ensure_on_path() is None or eplus_path.RESOLVED_DIR is not None


def test_plan_schema_is_the_single_source_of_truth() -> None:
    """The plan contract must be constructible and JSON-schema-able (constrained decoding)."""
    from common.models import Actuator, PlanStep, SetpointPlan

    step = PlanStep(
        offset_minutes=0,
        zone="Core_ZN",
        actuator=Actuator.COOLING_SETPOINT_C,
        value=25.0,
    )
    plan = SetpointPlan(planner_model="test", steps=[step], rationale="smoke")

    assert plan.horizon_minutes > 0
    assert plan.steps[0].actuator is Actuator.COOLING_SETPOINT_C
    assert "properties" in SetpointPlan.model_json_schema()
