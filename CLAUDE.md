# CLAUDE.md — HIVE project memory

Read this before touching anything. It encodes decisions that are **already made**.
Re-litigating them costs more than it saves; if one genuinely must change, change it here
first, in the same commit as the code.

---

## 1. Architecture in five lines

1. **EnergyPlus** runs a building digital twin; the `pyenergyplus` runtime API fires a
   synchronous C callback at each zone timestep, which reads sensors and writes actuators.
2. **The agent** (`agent/`) builds a compact *digest* of recent state + forecasts + KPIs and
   asks a **local Ollama** model for a `Plan` — off the callback thread, on its own cadence.
3. **The guardian** (`guardian/`) is deterministic Python that clamps, rate-limits, and — on
   any doubt — discards the plan and falls back to the baseline schedule. It is the only
   thing that ever touches an actuator.
4. **The MCP server** (`mcp_server/`) exposes the tool surface (`get_state`, `get_forecasts`,
   `get_kpis`, `submit_plan`, `read_error_log`, `patch_model`) so the planner reasons through
   tools instead of ad-hoc function calls.
5. **Telemetry** lands in SQLite (WAL) in batches; **Streamlit** (`dashboard/`) reads that
   database read-only and renders the loop for a human.

```
EnergyPlus ──callback──> guardian ──actuate──> EnergyPlus
     │                      ▲
     └── telemetry ──> SQLite (WAL) ──> digest ──> Ollama planner ──Plan──┘
```

---

## 2. Locked stack

| Decision | Choice | Why it is locked |
|---|---|---|
| Runtime | **Python 3.11, single process** | The EnergyPlus callback lives in this process. Multi-process means IPC on the hot path. Not worth it. |
| Plan contract | **Pydantic v2**, defined once in `common/models.py` | One schema, validated at every boundary. Also feeds Ollama's constrained decoding via JSON Schema. |
| Telemetry | **SQLite in WAL mode** | Single writer + concurrent readers is exactly our shape: the sim writes, the dashboard reads. No server to run. |
| UI | **Streamlit** | The dashboard is a read-only view. A SPA would be a second codebase for zero extra insight. |
| LLM | **Ollama, local, constrained decoding** | Offline, reproducible, no API key, no per-token cost, no network on the control path. |
| Explicitly rejected | **No FastAPI. No React. No Celery. No asyncio in the control path.** | Each would add a runtime, a build step, or a scheduler between us and a hard real-time C callback. |

Model baseline: `qwen2.5:7b-instruct-q4_K_M`. Swapping it is a config change, not a code
change — anything model-specific belongs in `agent/prompts.py`.

---

## 3. Hard rules

These are invariants, not style preferences. A change that violates one is a bug even if the
tests pass.

### R1 — The EnergyPlus callback is synchronous C. Treat it as an interrupt handler.

- **Never block on the LLM inside it.** No inference, no HTTP, no waiting on a queue with a
  timeout. The planner runs elsewhere and *deposits* a plan; the callback only ever reads the
  most recently approved one.
- **Never use asyncio inside it.** No `await`, no `asyncio.run`, no event loop. EnergyPlus
  called us from C; there is no loop to yield to and starting one re-entrantly deadlocks.
- **Wrap the entire callback body in `try/except Exception`.** An exception crossing the C
  boundary takes the simulation down with a useless traceback. On *any* exception: log it,
  actuate the **baseline fallback**, and return normally. The simulation must always finish.
- Cheap work only: array reads, comparisons, a bounded write buffer append.

### R2 — Every plan passes through the guardian before actuation. No bypass path may exist.

There is exactly one function that writes actuator values, and it takes a *guardian-approved*
plan — not a `Plan`. If you find yourself adding a `force=True`, a "debug direct write", or a
test hook that skips clamping, stop: that path will end up in the demo. Test the guardian by
feeding it hostile plans, not by going around it.

### R3 — All telemetry writes are batched. Never one INSERT per timestep.

A year-long run at 10-minute timesteps is ~52k timesteps × N channels. One INSERT (and one
implicit transaction, and one fsync) per timestep will dominate wall-clock and can stall the
callback. Buffer in memory, flush on a size or time threshold inside a single transaction, and
flush once more at simulation end. `common/store.py` owns this; nothing else opens a write
connection.

### R4 — `common/models.py` is the single source of truth for the plan schema.

`agent/`, `guardian/`, `mcp_server/`, the journal, and the dashboard all **import** those
models. Never redefine a plan-shaped dict, TypedDict, or parallel dataclass anywhere else —
including in tests. If the planner and the guardian can disagree about what a plan *is*, the
guardian is not actually a safety layer. Schema changes happen in `common/models.py` and
everything else follows.

---

## 4. Layout

Folder names are fixed by the competition brief — **do not rename them**.

```
simulation/   IDF/EPW assets, versioned model variants (v1_..vN.idf), run scripts
agent/        Ollama client, prompts, plan cache, digest builder
mcp_server/   tool surface exposed to the planner
guardian/     clamps, rate limits, fallback, watchdog
dashboard/    Streamlit app (read-only)
common/       models, SQLite store, config, logging  ← shared contracts
experiments/  A/B harness, endurance run, KPI extraction
tests/        pytest + hypothesis
reports/      architecture.md, exported results
data/         tariff.csv, carbon_intensity.csv (representative, not billing-grade)
media/        demo video
```

---

## 5. Working notes

- **`pyenergyplus` is not pip-installable.** It ships inside the EnergyPlus install
  directory. `common/eplus_path.py` appends `$ENERGYPLUS_DIR` to `sys.path` at import time.
  Import it before anything that touches the runtime API. CI has no EnergyPlus, so nothing
  in the test suite may require it at import time.
- **Current state: scaffold only.** Modules are stubs that raise `NotImplementedError` by
  design. No control logic has been written yet. Do not mistake a stub for a regression.
- **The data in `data/` is representative, not authoritative.** Indian ToU tariff and grid
  carbon-intensity curves shaped for realistic behaviour (midday solar dip, dirty evening
  peak). Fine for optimisation and demos; do not present it as billing data.
- Acceptance for every commit: `ruff check .` clean, `pytest -q` green.
