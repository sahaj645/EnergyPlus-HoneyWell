# HIVE

**HIVE is a closed-loop building-energy control agent.** A local LLM planner watches an
EnergyPlus digital twin through an MCP tool surface, reasons about weather, time-of-use
tariffs and grid carbon intensity, and proposes setpoint plans for the next hour. Every plan
then passes through a deterministic safety guardian that clamps it to a declared envelope,
rate-limits it, and — on any doubt — throws it away and falls back to the baseline schedule.
Only the guardian touches an actuator. The result is a system that gets the LLM's judgement
where judgement helps, and none of its stochasticity where it would break a building.

> **Status: scaffold.** Package layout, contracts, tooling and CI are in place. The control
> loop is not implemented yet — modules raise `NotImplementedError` by design.

---

## Architecture

```
                 ┌──────────────────────────────────────────────┐
                 │            EnergyPlus (digital twin)         │
                 │   pyenergyplus runtime API, synchronous C     │
                 └───────┬──────────────────────────▲───────────┘
        sensors, per     │                          │  actuator writes
        timestep         ▼                          │  (guardian-approved only)
                 ┌───────────────┐          ┌───────┴────────┐
                 │  callback     │─────────▶│    guardian    │
                 │  (hot path)   │  read    │  clamp / limit │
                 └───────┬───────┘  cache   │  fallback      │
                         │                  └───────▲────────┘
        batched writes   ▼                          │ ApprovedPlan
                 ┌───────────────┐                  │
                 │ SQLite (WAL)  │                  │
                 └───┬───────┬───┘          ┌───────┴────────┐
             digest  │       │ read-only    │  MCP server    │
                     ▼       ▼              │  6 tools       │
             ┌──────────────┐ ┌──────────┐  └───────▲────────┘
             │   planner    │ │Streamlit │          │ submit_plan
             │ Ollama, local│ │dashboard │          │
             └──────┬───────┘ └──────────┘          │
                    └─────────────Plan──────────────┘
```

One Python 3.11 process. The planner never runs inside the EnergyPlus callback — it deposits
plans in a cache that the callback reads without blocking. See [CLAUDE.md](CLAUDE.md) for the
invariants and [reports/architecture.md](reports/architecture.md) for the reasoning.

---

## Setup

### 1. EnergyPlus 24.x

Download and install from [energyplus.net](https://energyplus.net/downloads). Then point
`ENERGYPLUS_DIR` at the install root:

```bash
export ENERGYPLUS_DIR=/usr/local/EnergyPlus-24-1-0
```

On Windows (PowerShell):

```powershell
$env:ENERGYPLUS_DIR = "C:\EnergyPlusV24-1-0"
```

> **`pyenergyplus` is not pip-installable.** It is not on PyPI — it ships *inside* the
> EnergyPlus installation directory, next to the shared library it binds to. That directory
> must be on `PYTHONPATH` for `import pyenergyplus` to work.
>
> `common/eplus_path.py` does this for you: importing it appends `$ENERGYPLUS_DIR` to
> `sys.path`. Import it before anything that touches the runtime API:
>
> ```python
> from common import eplus_path  # noqa: F401  (side effect: sys.path)
> from pyenergyplus.api import EnergyPlusAPI
> ```
>
> Importing it on a machine without EnergyPlus is a no-op rather than an error, which is what
> lets CI, the tests and the dashboard run without a simulation engine. Call
> `eplus_path.require_energyplus()` at the point where you genuinely need it.

### 2. Ollama and the planner model

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
```

### 3. The project

```bash
pip install -e ".[dev]"
pre-commit install
```

### 4. Simulation assets

`baseline.idf` and `weather.epw` are not committed — see [simulation/README.md](simulation/README.md)
for what to drop in and where to get it.

### 5. Check it

```bash
ruff check . && pytest -q
```

Then:

```bash
streamlit run dashboard/app.py
```

---

## Repo map

| Path | What lives there |
|---|---|
| `simulation/` | `baseline.idf`, `.epw`, agent-authored `v1_..vN.idf` variants, run scripts |
| `agent/` | Ollama client, prompts, plan cache, digest builder — the planning half |
| `mcp_server/` | Tool surface: `get_state`, `get_forecasts`, `get_kpis`, `submit_plan`, `read_error_log`, `patch_model` |
| `guardian/` | Deterministic safety layer: limits, clamps, rate limits, fallback, watchdog |
| `dashboard/` | Streamlit app — read-only view of the run database |
| `common/` | `models.py` (the plan contract), `store.py` (SQLite WAL), `config.py`, `log.py`, `eplus_path.py` |
| `experiments/` | A/B harness, endurance run, KPI extraction |
| `tests/` | pytest + hypothesis |
| `reports/` | `architecture.md`, exported results |
| `data/` | Representative Indian ToU tariff and grid carbon-intensity curves |
| `media/` | Demo video |

### Key files

| File | Why it matters |
|---|---|
| [`common/models.py`](common/models.py) | The single source of truth for the plan schema. Everything imports it; nothing redefines it. |
| [`guardian/limits.py`](guardian/limits.py) | The safety envelope, as reviewable data rather than code. |
| [`CLAUDE.md`](CLAUDE.md) | Project memory: locked stack and the four hard rules. Read before contributing. |

---

## A note on the data

`data/tariff.csv` and `data/carbon_intensity.csv` are **representative, not authoritative**.
They are shaped like the real thing — off-peak nights, a midday trough where rooftop solar
displaces thermal generation, and a hard, dirty evening peak — which is enough to make load
shifting behave realistically in simulation. They are not billing data and not a measured grid
feed. Swap in the actual utility schedule before quoting a rupee or a kilogram.

---

## Development

```bash
ruff check .        # lint
ruff check --fix .  # lint + autofix
pytest -q           # tests
```

CI runs both on every push. There is no Docker build in CI, and no EnergyPlus install —
nothing in the test suite may require the runtime API at import time.
