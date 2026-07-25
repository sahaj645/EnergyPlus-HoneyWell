# HIVE — architecture

> Status: scaffold. This document describes the design the scaffold is shaped for; the
> control loop itself is not implemented yet. Sections marked **(planned)** have no code
> behind them.

## 1. The problem

A commercial building's HVAC schedule is written once and then runs for years. It does not
know that electricity costs twice as much at 19:00 as at 13:00, that the grid is nearly twice
as carbon-intense at the evening peak, or that tomorrow is cooler than today. Closing that
loop is an optimisation problem with a hard constraint: occupants must stay comfortable, and
the equipment must not be damaged.

An LLM is a good fit for the *planning* half — it can weigh weather, price, carbon and
occupancy in natural units and explain itself. It is a catastrophic fit for the *actuation*
half, because it is stochastic and occasionally confidently wrong.

HIVE's whole design follows from splitting those two halves apart.

## 2. Component map

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

Everything runs in **one Python 3.11 process**. The EnergyPlus callback executes on the
simulation thread; the planner runs off it. They meet at exactly one place — the plan cache.

## 3. The control loop

| Cadence | What happens |
|---|---|
| Every zone timestep (~10 min sim time) | Callback reads sensors, appends to the telemetry buffer, reads the cached approved plan (non-blocking), writes actuators. |
| Every planning interval (default 15 min sim time) | Digest built from recent telemetry + forecasts + KPIs → Ollama → `Plan` → guardian → `ApprovedPlan` → plan cache. |
| On buffer threshold | Telemetry flushed to SQLite in one transaction. |
| On any failure | Fallback to the baseline schedule; the run continues. |

## 4. Why the guardian is a separate deterministic layer

The guardian is not a filter bolted on for safety theatre — it is what makes the LLM usable
at all. Three properties:

1. **Type-level enforcement.** The planner emits `Plan`. The actuator accepts only
   `ApprovedPlan`. Only the guardian constructs the latter. "No bypass path" is therefore
   checkable by reading signatures, not by trusting a code review.
2. **Determinism.** Given the same plan and state it returns the same verdict. This is what
   makes the safety layer testable with property-based tests (hypothesis): generate hostile
   plans, assert the output is always inside the envelope.
3. **Graceful degradation.** It never raises and never returns nothing. Worst case it returns
   the baseline schedule marked `fallback=True`. A run that finishes on fallback still
   produces a complete dataset; a run that crashes produces nothing.

The safety envelope itself lives in `guardian/limits.py` as data, so widening it is a
reviewable diff rather than a code change buried in a branch.

## 5. Why the callback is treated as an interrupt handler

`pyenergyplus` calls into Python from C, synchronously, on the simulation's own thread. Three
consequences drive the design:

- **Blocking blocks the simulation.** A 4-second local inference call inside the callback,
  at 52,560 timesteps a year, is 58 days of wall-clock. The planner therefore never runs on
  this thread — hence the cache.
- **No event loop exists.** Starting one re-entrantly from a C callback deadlocks. No
  `asyncio` on this path, ever.
- **An exception crosses the C boundary badly.** It surfaces as an opaque failure and takes
  the run down. The entire callback body is wrapped in `try/except Exception`: log, actuate
  the fallback, return normally.

## 6. Telemetry (planned)

One writer, many readers — the shape WAL exists for, and the reason there is no database
server here. Writes are batched (buffer, then one transaction) because an implicit
transaction and fsync per timestep would dominate wall-clock and can stall the callback.

Tables: `telemetry`, `plans`, `approved_plans`, `guardian_events`, `kpis`, `runs`. The plan
journal is deliberately append-only — the raw model output is stored *before* the guardian
sees it, so every intervention can be reconstructed after the fact.

## 7. Evaluation (planned)

- **A/B:** identical IDF, EPW and simulation period; one arm with the agent, one without.
  Reported: energy, cost (`data/tariff.csv`), carbon (`data/carbon_intensity.csv`), peak
  demand, comfort-violation hours in occupied periods only.
- **Endurance:** a full simulated year. The metric is survival, not savings — planning cycles
  attempted vs failed, guardian clamp and rejection rates, fallback timestep count.

A cost or carbon number from this repo is only as good as `data/`, which is **representative,
not measured**. Any headline claim needs the real utility schedule and a real grid feed.

## 8. Open questions

- Which prototype IDF and which Indian TMY city? (Cooling-dominated is the more interesting
  case for ToU shifting.)
- Does `patch_model` earn its place, or is model self-modification a demo feature with a
  large blast radius? It stays behind the same guardian discipline either way.
- Planning cadence vs local inference latency on the target machine — 15 min is a guess until
  measured.
