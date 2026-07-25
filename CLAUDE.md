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
- **Current state: baseline + closed loop + model-versioning landed; planner still stubbed.**
  Implemented: `simulation/{fetch_assets,run_baseline,prepare_idf,idf_io,snapshots,patching,
  receding}.py`, `agent/bus.py`, `common/store.py`, `guardian/supervisor.py` (first cut),
  `experiments/{kpis,smoke_roundtrip}.py`. Still `NotImplementedError` stubs by design:
  `agent/ollama_client.py`, `agent/digest.py`, `agent/plan_cache.py`, `guardian/watchdog.py`,
  `guardian/fallback.py`, `mcp_server/*`, `dashboard/app.py`,
  `experiments/{ab_harness,endurance,kpi_extract}.py`. Do not mistake a stub for a regression.

### Two actuation modes, one interface

`common.models.ControlInterface` is the contract: `read_state()` and
`write_setpoints(approved, now=...)`. Both implementations satisfy it, so agent code cannot
tell them apart and switching is a construction-site change:

| Mode | Class | How it actuates |
|---|---|---|
| `live` (default) | `agent.bus.SimulationBus` | Runtime-API writes into a running simulation |
| `receding` | `simulation.receding.RecedingHorizonDriver` | Bakes the horizon chunk's schedules into a copy of the IDF, re-simulates, reads results back from the chunk's SQL |

`python -m experiments.smoke_roundtrip --mode receding` selects it. Receding is the **H6
contingency**: slower (one EnergyPlus start-up per chunk) and it cannot react *within* a chunk,
but it depends on nothing except "EnergyPlus can run an IDF". `state` is an opaque
mode-specific token - the E+ handle in live mode, ignored in receding mode - so callers outside
a callback omit it.

`RunPeriod` is day-granular, so a chunk is at minimum one day. To avoid flattening a
time-varying plan to one constant, receding mode renders it as a `Schedule:Compact` with
`Until:` blocks for the chunk.

### Snapshots are never written from inside the callback

Materialising an IDF means an eppy load and save - hundreds of milliseconds of blocking I/O,
which rule R1 forbids on the callback path. So `SimulationBus` only appends to
`bus.control_history` when the applied values actually change (a hash over a few floats), and
the harness replays that history through `SnapshotWriter` **after** the run.

### The version series is deduped by content, not by event

Most planning cycles change nothing. `SnapshotWriter.commit()` returns `None` - writing no file
and no manifest entry - when the state is control-identical to the **head**. Dedupe is against
the head only, so returning to an earlier value is legitimately a new version. `content_hash`
covers schedule values and applied patch ids, and deliberately excludes `sim_time` and
`trigger`: the same setpoints at a different moment are the same model.

### Rollback copies bytes; it does not invert patches

`patching.rollback(v)` copies version `v` forward as a new version. Re-applying an inverse
patch would be clever and wrong - float round-trips and eppy field normalisation mean a
"reversed" patch is not guaranteed to reproduce the original file. History is append-only:
rolling back adds a version, never deletes one. Patches validate by re-parsing before the new
version is accepted, so a bad patch leaves the head untouched.

**`patch_model` has a blast radius the guardian does not cover.** The guardian reviews *plans*,
not patches; a patch can change any object in the model. Autonomous use needs its own review
gate, which does not exist yet.

### The runtime API bites in four specific places

`agent/bus.py` exists to enforce these. Breaking any one fails silently, not loudly:

1. `request_variable()` must be called for **every** output variable *before*
   `run_energyplus()`. A handle for an unrequested variable is `-1` — no exception, no log —
   and then reads as a plausible `0.0` for the rest of the run.
2. No handle lookups until `api_data_fully_ready(state)`. Handles are stable afterwards, so
   fetch once and cache; re-fetching per timestep is slow and pointless.
3. Guard every read and write on `warmup_flag(state)`. Callbacks fire during warmup, those
   values are not physical, and warmup writes are discarded.
4. **PMV is keyed by the People object name, not the zone name.** Zone air temperature and
   occupancy are keyed by zone. That asymmetry is why `PreparedModel` carries a zone→People
   map instead of anyone guessing the key.

### Setpoints are actuated through `Schedule:Constant`, not thermostat actuators

`prepare_idf.py` rewrites each thermostat-referenced `Schedule:Compact` into a
`Schedule:Constant` of the same name, so `Schedule:Constant / Schedule Value` is writable.
Thermostat actuators vary by HVAC template and some prototypes expose none — a missing one is
another silent `-1`. The cost: `agentic.idf` has **no daily setback profile left**, so it is
*not* the A/B control arm. `baseline.idf` is.

### Plans are anchored in simulation time

`Plan.created_at` / `ApprovedPlan.approved_at` are wall-clock UTC; the simulation runs in
simulation time. `PlanStep.offset_minutes` is measured from the sim time at which the bus
*first saw that `plan_id`*, never from the wall clock. Mixing the two produces silent nonsense.
- **`experiments/kpis.py` reads the EnergyPlus SQL; `experiments/kpi_extract.py` reads the HIVE
  telemetry DB.** Two different sources on purpose: the baseline has no agent and no HIVE DB,
  so its only truth is E+'s own `eplusout.sql`.
- **`baseline.idf` / `weather.epw` / `agentic.idf` are gitignored** — built per machine, never
  committed. `agentic_model.json` (the `PreparedModel` index) is written beside `agentic.idf`
  and is what the bus loads; it is never re-derived by re-parsing the IDF.
- **Order of operations:** `fetch_assets` → `prepare_idf` → `smoke_roundtrip` / agent runs.
  `run_baseline` uses `baseline.idf` directly and does not need `prepare_idf`.
- **`simulation/versions/` is gitignored** — it is generated per machine by running the
  harness and packaged at submission time. `manifest.json` is its index; inspect with
  `simulation.snapshots.summarize()`. Snapshots and patches share **one** version series on
  purpose: the deliverable is a single ordered history, not two interleaved ones.
- **The data in `data/` is representative, not authoritative.** Indian ToU tariff and grid
  carbon-intensity curves shaped for realistic behaviour (midday solar dip, dirty evening
  peak). Fine for optimisation and demos; do not present it as billing data.
- Acceptance for every commit: `ruff check .` clean, `pytest -q` green.
