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
- **Current state: full pipeline + live dashboard + self-correction landed.** Implemented:
  `simulation/{fetch_assets,run_baseline,prepare_idf,idf_io,snapshots,patching,receding}.py`,
  `agent/{bus,digest,planner,scheduler,prompts,events,cache,feedback,repair}.py`,
  `common/{store,planslot,generated_enums}.py`,
  `guardian/{core,executor,watchdog,fallback,supervisor,limits}.py`,
  `mcp_server/{server,tools,providers}.py`,
  `experiments/{kpis,smoke_roundtrip,smoke_llm_loop,mcp_exercise,ab,report,endurance,
  self_heal_demo}.py`, `dashboard/{app,export_screens}.py`. Still `NotImplementedError` stubs by
  design: `agent/ollama_client.py` (superseded by `agent/planner.py`), `agent/plan_cache.py`
  (superseded by `agent/cache.py`), `experiments/ab_harness.py` (superseded by
  `experiments/ab.py`), `experiments/kpi_extract.py`.

### L2: the guardian's own reasons become next cycle's planning context

`agent/feedback.py` (`FeedbackTracker`) closes the loop Session 5 left open (`agent/digest.py`'s
empty `PREVIOUS PLAN FEEDBACK` slot). It watches `guardian.executor.Executor.guardian_events` -
the same in-memory buffer the harness drains to SQLite after the run, read *live* here via a new
lock-guarded `Executor.events_snapshot()` - and the moment a plan comes back anything but cleanly
`accepted`, its reasons (`GuardianEvent.note`, already the guardian's stable machine grammar) are
latched as the next cycle's feedback.

- Wiring lives in the harness, not the scheduler: `experiments/ab.py` and
  `experiments/smoke_llm_loop.py` both construct the `Executor` *before* `digest_provider`, so the
  closure can call `feedback.observe(executor.events_snapshot())` right before building the digest
  that will carry it. `agent/scheduler.py`'s `Scheduler` takes an optional `feedback=` and, on the
  LLM branch only (never cache/hold - those skip the digest entirely), captures
  `feedback.pending_plan_id()` alongside the digest and threads it through as the new plan's
  `corrects_plan_id`, resolving the tracker once that plan is written.
- **`plans.corrects_plan_id`** (migrated column, like the Session 8 `llm_calls` additions) links a
  corrected plan back to the one it fixes. The dashboard journal (`dashboard/app.py`) renders it as
  `"<trigger> ↳ corrects <id>"` - the clipped/rejected → corrected chain, at a glance.
- The system prompt (`agent/prompts.py`) gained one line making the correction explicit: this
  cycle's plan is a fix, not a resubmission of what was just clipped.

### L3: self_heal_demo.py exercises the real fault→diagnose→patch→resume loop

`experiments/self_heal_demo.py` is scripted and re-runnable, built from existing primitives, not a
new subsystem: `plant_fault` removes a zone's cooling schedule via the ordinary
`simulation.patching.apply_patch` three-verb primitive (syntactically valid, semantically a
dangling reference); one `experiments.ab.run_bare` call is the receding-horizon chunk; the **real**
`agent.events.DriftEventDetector` (not a demo-only check) watches that chunk's `eplusout.err` and
fires `severe_error`; `agent/repair.py` (`RepairPlanner`, structurally `agent/planner.py`'s sibling
- static system prompt, `PatchSpec.model_json_schema()` as the constrained-decoding grammar, one L1
repair retry, every call logged) turns the filtered error log into a candidate `PatchSpec`;
`apply_patch` validates-before-accepting it as a new version; the chunk re-runs, and
`simulation.patching.rollback` returns the series to a registered-clean v1 if the patch didn't
parse *or* the error persists - "the loop resumes" either way (healed or safely rolled back), never
stuck on a broken model.

- Every step is journaled: a plain JSON list (`Journal`, printed and saved as
  `<out_dir>/journal.json`) plus the one real LLM call, which still goes through
  `TelemetryStore.write_llm_call` like any other planner call.
- **`--replay`** skips the LLM entirely: it looks up the newest manifest entry whose applied patch
  is tagged `repair-<id>` (fault patches are tagged `fault-<id>`, so the two are never ambiguous)
  under a **persistent** `--versions-dir` (default `experiments/results/self_heal_versions`,
  distinct from the per-invocation, timestamped `--out`) and reuses that exact `.idf` file - so the
  demo is deterministic and does not depend on Ollama being up during a live presentation. Labeled
  `REPLAY` in the journal and in `main`'s printed summary.

### The dashboard is read-only over WAL - it never opens a write connection

`dashboard/app.py` (Streamlit, single page, ~5 s auto-refresh via `st.fragment(run_every=)`)
reads the telemetry SQLite through `common.store.reader` (the `query_only` pragma), the A/B
`reports/results.json`, and the endurance `checkpoint.json` - **nothing on screen is
hardcoded**, and it is safe to point at the database of a *running* endurance sim because WAL
readers never block the writer (proven: reader saw 900→984 rows grow live). Five sections in
fixed order: headline strip, cumulative-kWh race chart (tariff/carbon high-band hours shaded,
pre-cool windows overlaid), per-zone PMV strip (±0.5 envelope), decision journal
(`plans`⋈`llm_calls` for cache-vs-call, `guardian_events` for the verdict), LLMOps
(calls/avoided, tokens, p50/p95 latency, retries, verdict counts, timeouts, ₹-saved vs a
**labeled** API-price assumption, endurance card). Aggregation is SQL-side (`GROUP BY`/filtered
selects, band shading merged into a dozen rects); a week loads in <0.5 s.

- **`?section=N&static=1`** renders exactly one section with auto-refresh off - that is what
  `dashboard/export_screens.py` screenshots. Capture is **CDP-driven** (open a page target on
  a headless Edge/Chrome, poll the DOM until the section's content actually mounted, *then*
  capture) - `--screenshot`/`--virtual-time-budget` fire before Streamlit's websocket paints
  and yield blanks. Screenshots land in `reports/screens/` (gitignored, regenerated per run).
- **`llm_calls` gained `prompt_tokens`/`completion_tokens`/`retries`** this session. `init_db`
  now runs an idempotent `ALTER TABLE ... ADD COLUMN` migration (`_MIGRATIONS`) so databases
  from older builds keep working - `CREATE TABLE IF NOT EXISTS` alone cannot retrofit columns.

### The A/B harness is the scored run - three arms, identical conditions

`experiments/ab.py` runs **baseline** (`baseline.idf`, unmodified `Schedule:Compact` day/night
setback - the true control arm), an optional **constant** secondary baseline
(`--secondary-baseline constant`: a copy of `agentic.idf` with nothing actuating it, isolating
"lost the setback profile" from "the agent's own contribution"), and **agent** (the full closed
loop) - all three sharing the same `RunPeriod` (default: the hottest week), EPW, and timestep,
via one shared IDF-prep function (`prepare_arm_idf`). Neither `baseline.idf` nor `agentic.idf`
is ever edited in place; each arm gets its own `<label>_patched.idf` under
`experiments/results/ab_<timestamp>/<label>/`.

- **`run_agent_arm`** (in `ab.py`) is the reusable "run the full live loop over one RunPeriod"
  building block - the week-long agent arm here, and each day-chunk of `experiments/endurance.py`
  are both just calls to it with a different `spec`/`out_dir`.
- Both the constant and baseline arms need PMV to be computable for the comfort table even
  though `baseline.idf` never goes through `prepare_idf` - so `prepare_arm_idf` also calls
  `simulation.prepare_idf.ensure_fanger_comfort`/`ensure_outputs` on every arm, not just the
  agentic ones.
- Exit gate: `python -m experiments.ab [--secondary-baseline constant]` completes all arms,
  then `python -m experiments.report --ab-dir <dir>` writes `reports/results.{json,md}`.

### The report reads SQL, computes deltas, prints everything - no filtering

`experiments/report.py` reuses `experiments.kpis.compute_kpis` for the headline numbers (site
kWh, HVAC subsystem = cooling+fans+pumps electricity, peak kW, cost, carbon - already tested)
and adds its own full-timestamp SQL reads (`read_meter_series`, `read_zone_series`, reusing
`experiments.kpis`'s warmup/run-period filtering so the two never disagree about which rows
count) for the per-day breakdown, the cumulative-kWh series (dashboard race-chart data), and a
per-zone comfort-violation table (`|PMV| > 0.5` in occupied intervals, matching the threshold
already used in `mcp_server/providers.py`). Percentage deltas are `None` - never a fabricated
number - when the baseline value is zero. When `constant` is present, three deltas are computed
(baseline→constant, constant→agent, baseline→agent) so "the agent's own contribution" is a real
number, not folded into "everything changed at once".

### Endurance chunks by day because EnergyPlus cannot pause and resume

`experiments/endurance.py` cannot checkpoint a *running* EnergyPlus process - the runtime API
has no such capability - so resumability is chunk-level: the month is split into
`--chunk-days`-sized pieces (default 1), each a separate call to `experiments.ab.run_agent_arm`,
with a JSON checkpoint (`next_chunk_index` + cumulative counters) written atomically
(temp file + `os.replace`) after every chunk. `--resume` continues from the first *incomplete*
chunk; a checkpoint that already exists without `--resume` is refused, not silently clobbered.

- **The `PlanCache` is reused across chunks within one process** (cache hit rate keeps
  accumulating); the `DriftEventDetector` is rebuilt fresh every chunk (it keys severe-error
  detection off one `.err` file's mtime, which is a new file every chunk - carrying it forward
  would let a stale count shadow a real error in the new file). A resumed run's cache starts
  cold - safe, just a few more planner calls than an uninterrupted run.
- **Exceptions are counted, never swallowed**: a chunk that raises is logged with its full
  traceback, counted in `cumulative.unhandled_exceptions`, checkpointed *without* advancing
  past it, and re-raised - the run stops loudly rather than skipping a broken day.
- Verified (mocked `run_agent_arm`, no EnergyPlus needed for this part): a failure on chunk 3
  leaves `next_chunk_index` unmoved and `unhandled_exceptions=1`; `--resume` retries that exact
  chunk (same RunPeriod) and completes; a second run without `--resume` is refused.

### MCP tool surface: tools are pure, the server is a thin wrapper

`mcp_server/tools.py` holds the six tools as **pure functions over a `ToolContext`** - no `mcp`
import, so they are directly callable and testable. `mcp_server/server.py` (FastMCP, stdio,
lazy `mcp` import) wraps them with LLM-facing docstrings; `mcp_server/providers.py` builds the
state/forecast/KPI providers the context needs. `ToolContext` is injected once, so the live
server and the exercise script run identical tool code.

- **`submit_plan` has no bypass (R2).** It lowers the plan and runs it through the *same*
  `guardian.core.Guardian` the executor uses, returning the verdict - it cannot actuate. The
  exercise script proves an abusive plan gets the identical clip/rate/strip verdict through the
  tool as through the guardian directly. `patch_model` is the one model-mutating tool; it uses
  the Session 3 primitive, which validates-before-accept, so a bad patch never lands (the
  auto-rollback is "nothing was written").
- Exit gate: `python -m experiments.mcp_exercise` (every tool once; submit_plan == internal).

### Reactive events + plan cache cut LLM calls hard

`agent/events.py` (`DriftEventDetector`, implements the scheduler's `EventDetector`) fires on
comfort drift (occupied `|PMV|>0.4` or within 0.3 C of the envelope edge, **2 consecutive
steps**), **rising** demand into the top 15% of the trailing-7-day peak, an **edge-triggered**
tariff/carbon band change within the hour, or a new E+ Severe error. All debounced to 10
sim-minutes. `should_trigger` runs on the callback thread, so its only I/O - the `.err` read -
is gated on mtime + a once-per-minute clock.

`agent/cache.py` (`PlanCache`) keys the planning situation as `(hour band, occupancy bucket,
2 C outdoor bin, tariff band, carbon band)`. A hit replays the stored plan with timestamps
shifted to now - **still lowered and still guardian-filtered by the executor**, zero LLM calls.
The **hold pre-filter** short-circuits an hourly tick with no event and comfort pressure below
epsilon straight to "do nothing, no call". Both wired into `Scheduler` (new `cache=` /
`event_detector=` params, default off so Session 5 behaviour is unchanged); counters
(`calls_made`/`calls_avoided`/`holds`) are persisted. In a representative day these take the
planner from one-call-per-cycle down to a handful (~70-85% avoided).

### Two plan contracts: `Plan` (LLM) and `SetpointPlan` (actuation)

R4 says `common/models.py` holds the plan schema; there are now **two levels** of it, and
mixing them up is the easy mistake:

- **`Plan` / `PlanAction`** — what the **LLM emits**. Enum-typed `zone`/`actuator` (codegen'd),
  a value window (`start`/`end`), per-action `rationale`, an `ecms` playbook, a `trigger`, and
  `horizon_hours` (4–6). `Plan.model_json_schema()` is the constrained-decoding grammar handed
  to Ollama (`format=`).
- **`SetpointPlan` / `PlanStep`** — what the **guardian and executor** operate on: flat,
  relative-time (`offset_minutes`) setpoint moves. This is the old `Plan`, renamed this session.
- **`Plan.to_setpoint_plan(now, baseline)`** lowers one to the other. The **scheduler** does
  this before `planslot.commit()`; the slot holds a `SetpointPlan`. `ApprovedPlan` (guardian
  output) is unchanged.

If you write `Plan(steps=...)` you want `SetpointPlan`. If you want `actions`/`ecms`/`trigger`
you want `Plan`.

### Zone/Actuator enums are codegen'd from the IDF

`common/generated_enums.py` (`ZoneEnum`, `ActuatorEnum`) is **generated** by
`simulation/prepare_idf.py` (`render_generated_enums`/`emit_enums`) from the prepared model, so
the LLM is constrained at decode time to name only zones/actuators that exist. A **default** is
committed so the package imports with no IDF prepared (CI/tests); `prepare_idf` rewrites it in
place from the real model. `GENERATED_FROM` starting with "default" means it has not been
regenerated yet. Do not hand-edit it.

### The planner never runs on the callback thread

`agent/planner.py` (Ollama, `temperature=0`, `format=schema`, `keep_alive`, one-shot L1 schema
repair, logs every call to `llm_calls`) is called only by `agent/scheduler.py`, which runs it on
a **worker thread**. The callback calls `Scheduler.on_timestep` (cheap, non-blocking); the
worker builds the digest, plans, lowers, and commits to the slot. Triggers: startup + hourly
(sim time); event is a stubbed `EventDetector` for Session 6. Hard 30 s wall-clock budget;
late results are discarded via an epoch check. The digest reads a callback-captured state
snapshot, never the live E+ exchange (that is callback-thread-only).

- **System prompt is a byte-identical constant** (`agent/prompts.py`) — editing it invalidates
  the prompt-prefix cache that `keep_alive` relies on. Digest trails it; schema leads via
  `format=`. `agent/digest.py` renders a <=1.5K-token, deterministic, fixed-vocabulary digest
  (arrows `up/down/flat`, bands `low/mid/high`); the `PREVIOUS PLAN FEEDBACK` section is empty
  until Session 9.
- **`TelemetryStore` is now thread-safe** (reentrant lock): the callback writes telemetry while
  the planner worker writes `llm_calls`/`plans` on the same connection.
- Exit gate: `python -m experiments.smoke_llm_loop` (one live day; >=1 accepted plan actuated)
  and `python -m experiments.smoke_llm_loop --timeout 0.1` (every cycle preempted, day still
  completes on baseline).

### The guardian: `guardian/core.py` is the safety kernel

`Guardian.filter(plan, state: ZoneState, history: RateHistory) -> GuardianVerdict` is the
single pure entry point, built to be hammered by property tests later. Three protections in a
fixed order: **whitelist** (off-whitelist actuators stripped, never fatal) → **comfort
envelope** (occupied `23 ± 1.5 C` + a PMV "don't make discomfort worse" guard; unoccupied the
wider `20-30 C` ECM band; occupancy read from `state`, never the plan) → **rate limit**
(`1.0 C`/timestep and `2.0 C`/hour).

- **No hidden state.** The rate limiter's memory lives in an explicit, immutable `RateHistory`
  passed in and returned anew (`record()` prunes to the trailing hour). `filter` never mutates
  it and never reads a clock — the executor records the *applied* value after each write. This
  purity is deliberate: the property proofs land on exactly this interface.
- **Reasons are a stable machine grammar.** `clip: <zone> <orig>-><new> <rule>`,
  `rate: ... rate_step|rate_hour`, `strip: <zone> <actuator> whitelist`. Fed to the planner
  verbatim in a later session — do not reword them casually.
- **`GuardianVerdict.safe_plan` is a `Plan`.** Turning survivors into the `ApprovedPlan` the
  actuator accepts is `Guardian.approve()` — the guardian stays the only producer of
  `ApprovedPlan` (rule R2). No LLM or network import may ever appear anywhere in `guardian/`.

**Two `Guardian` classes coexist, on purpose (for now).** `guardian/supervisor.py`
(`review() -> ApprovedPlan`, per-actuator bounds, deadband) is the older first cut still used by
the live-bus dumb-plan path and its tests. `guardian/core.py` (`filter() -> GuardianVerdict`,
occupancy-driven envelope, explicit RateHistory) is the definitive kernel the **executor** uses.
Retiring the supervisor onto core is future work — until then, do not assume "the guardian"
means one file.

### The executor runs the building with or without a planner

`guardian/executor.py` is the actuator's hand. Each timestep it reads the latest plan from the
thread-safe `common/planslot.py` (`PlanSlot.get/commit`, holds exactly the newest plan), *holds*
it to the value in force now, filters every zone through `core.Guardian`, and calls
`control.write_setpoints` — the one actuator write. With no plan, a rejected plan, or a tripped
watchdog it writes the **baseline** (`guardian/fallback.py`), so the building runs safely
forever on a silent planner.

- **`guardian/watchdog.py`** trips when no *fresh* valid plan has arrived for `> 2` planning
  intervals (sim time; freshness is per new commit, not per timestep — a plan that just sits in
  the slot goes stale). On trip it yields a `WATCHDOG_TIMEOUT` `GuardianEvent` and the executor
  forces the baseline.
- **R1 on the executor path.** `Executor.provide` runs inside the EnergyPlus callback, so it
  does **no** DB I/O: guardian events and the verdict stream are buffered and drained by the
  harness *after* the run (`Executor.drain_events`). Only the store's already-batched telemetry
  writes happen during the run.
- Exit gate for this layer: `python -m experiments.smoke_roundtrip --abusive` drives an
  18 C-occupied / 5 C-jump / off-whitelist plan through the executor and prints the verdict
  stream; clip + rate + strip must all fire, the sim must complete, and `guardian_events` rows
  must land.

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

### The consolidated test suite - properties, contracts, seams, CI, packaging

- **`tests/test_guardian_properties.py`** - Hypothesis on `guardian.core.Guardian.filter`, the
  first adversarial coverage that interface has had. Five properties: envelope containment ("no
  reachable plan can exit the comfort envelope"), rate-limit adherence replayed across cycles,
  whitelist totality, never-raises on garbage (NaN/inf/huge magnitudes/unknown zones/empty
  plans), idempotence on an already-safe plan. **Uncovered two real bugs**, both fixed in
  `guardian/core.py`: `_apply_pmv`/`_apply_rate` used the *observed* setpoint / rate-history
  anchor as a correction target without checking it was finite - a corrupt sensor reading
  (NaN cooling setpoint) could propagate straight through the rate clamp into the output value,
  and a hostile-but-finite observed setpoint could pull a PMV correction outside the
  already-envelope-clamped range. Fixed with `math.isfinite` guards on every reference value plus
  a final re-clamp back into the envelope after the PMV/rate passes (belt-and-suspenders: the
  containment property must hold regardless of *why* a reference value was bad). **This changes
  guardian behaviour on hostile/corrupt input only** - real telemetry is always finite and
  in-range, so it should not move the A/B numbers, but the A/B has not been re-run against it
  (no EnergyPlus in this environment) - do that before trusting `reports/results.json` again.
  500 examples/property locally (the `dev` Hypothesis profile, default); CI loads the capped
  `ci` profile (`tests/conftest.py`, 25 examples) via `HIVE_HYPOTHESIS_PROFILE=ci` so an
  adversarial suite never makes every push slow.
- **`tests/test_contracts.py`** - `Plan.model_json_schema()` round-trips a schema-legal sample
  (the exact guarantee `agent/planner.py`'s `format=` argument depends on) and lowers cleanly to
  `SetpointPlan`; `agent/digest.py` stays within its ~1.5K token budget for the *real* building's
  full zone count (`ZoneEnum`, not a guessed size) with a full 6-hour forecast and a non-empty
  feedback section; `common/planslot.py`'s `PlanSlot` survives 8 writer + 4 reader threads
  hammering `commit`/`get`/`snapshot` concurrently with no torn read and no exception.
- **`tests/test_seams.py`** - `agent/cache.py`'s discretization boundaries (`hour_band`,
  `occupancy_bucket`, `outdoor_bin`) at the exact values the docstrings claim; the rate limiter's
  behaviour across a lost `RateHistory` (a process restart: the observed setpoint becomes the
  anchor, so the first post-restart cycle is still rate-limited, not a free jump); L2's
  feedback-injection end to end with a mocked planner (`_FakePlanner`, no Ollama) - cycle 2's
  digest carries cycle 1's guardian reasons verbatim and cycle 2's plan records the right
  `corrects_plan_id`. The KPI ₹/CO2 join already had fixture-SQL coverage from Session 2
  (`tests/test_kpis.py`) - not duplicated here.
- **CI**: `.github/workflows/ci.yml` now runs `pytest --cov=guardian --cov=common
  --cov-fail-under=80` (currently ~83%) under the `ci` Hypothesis profile. The gate is scoped to
  `guardian/` + `common/` on purpose - the safety kernel and the shared contracts - not chased
  elsewhere, since the rest of the pipeline needs EnergyPlus/Ollama to exercise meaningfully and
  CI has neither.
- **Packaging** (`Dockerfile`, `docker-compose.yml`, `deploy/README.md`) - not part of CI. The
  demo runs bare-metal on purpose (shortest path from command to a live loop for a presentation);
  compose is the gateway-appliance deployment story: one `agent` container (EnergyPlus + guardian
  + planner) and one read-only `dashboard` container sharing the `experiments/results/` and
  `simulation/` volumes, Ollama left on the host via `OLLAMA_HOST` rather than containerized
  (model weights have their own lifecycle, and one Ollama instance is meant to serve more than
  one appliance). `EPLUS_DEB_URL`/`EPLUS_DEB_SHA256` build-args are required, not guessed -
  see `deploy/README.md` for where to get the pinned v24.1.0 asset.
