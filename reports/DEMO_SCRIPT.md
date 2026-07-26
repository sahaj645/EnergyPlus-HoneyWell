# Demo script — 3 minutes

Every command below is real and runnable on a machine with EnergyPlus 24.1.0 + Ollama (model
pulled) set up per the root [README.md](../README.md). Run them in order once, ahead of time, to
warm caches and confirm timings; the recording itself can then run the same commands live or cut
to the pre-generated screenshots in `reports/screens/`.

## Before recording

```bash
# 1. Assets in place (see simulation/README.md), then:
python -m simulation.fetch_assets
python -m simulation.prepare_idf

# 2. A real A/B + report, so results.json is a measured number, not this repo's synthetic one:
python -m experiments.ab --secondary-baseline constant
python -m experiments.report --ab-dir experiments/results/ab_<timestamp>

# 3. Dashboard up, and the deck screenshots regenerated against the *real* run above:
streamlit run dashboard/app.py &
python -m dashboard.export_screens
```

Package the submission's model-series deliverable while assets exist locally (git ignores these
by design — see CLAUDE.md — so this step is what actually gets them into the zip):

```bash
python -c "from simulation.snapshots import summarize; print(summarize('simulation/versions'))"
# then include simulation/baseline.idf, simulation/versions/ (manifest.json + every v*.idf),
# and reports/screens/*.png in the submitted archive alongside the git checkout.
```

## Shot list

| Time | Shot | Command / action | What it proves |
|---|---|---|---|
| 0:00–0:20 | Cold open on the architecture diagram | Show `reports/architecture.md` §1 (mermaid diagram) | The supervisory-over-deterministic split, stated once, up front |
| 0:20–0:50 | Live loop, one day | `python -m experiments.smoke_llm_loop` | The planner is genuinely in the loop: ≥1 plan accepted and actuated, the 5-line scoreboard printed at the end |
| 0:50–1:10 | Kill the model mid-run | `python -m experiments.smoke_llm_loop --timeout 0.1` (let it run ~15 s, point out it still finishes) | Every planning cycle preempted; the building runs safely on baseline the whole time — R1 in action |
| 1:10–1:40 | Dashboard, section 2 (race chart) | `streamlit run dashboard/app.py`, scroll to the cumulative-kWh race chart | Pre-cooling windows visibly aligned with the shaded tariff/carbon peaks |
| 1:40–2:05 | Dashboard, section 4 (journal) | Scroll to the decision journal | Trigger → ECMs → guardian verdict, cache-hit vs LLM-call, and (if a clip fired this run) the `↳ corrects <id>` L2 chain |
| 2:05–2:25 | Self-heal, live | `python -m experiments.self_heal_demo` | Plant a fault → the real `DriftEventDetector` fires → a repair patch is generated and applied → the model runs clean again |
| 2:25–2:35 | Self-heal, replay | `python -m experiments.self_heal_demo --replay` | The same repair, deterministically reused, no LLM call — reliability under demo conditions |
| 2:35–2:55 | Results table | `cat reports/results.md` (from the *real* run generated above) | baseline vs constant vs agent — "the agent's own contribution" isolated from "lost the setback profile" |
| 2:55–3:00 | Close | One line: repo + `CLAUDE.md` pointer | Where a judge verifies every claim just made |

## What NOT to demo live

- `experiments.endurance` (a full month/year) — run it ahead of time and show the checkpoint +
  dashboard's endurance card instead; it is not a 3-minute operation.
- `patch_model` free-form — the self-heal demo already exercises it end to end with a real,
  bounded scenario; an ad-hoc patch on camera risks landing on an unreviewed edit with the real
  blast radius CLAUDE.md names.
