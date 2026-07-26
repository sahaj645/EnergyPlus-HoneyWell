# Deployment

Two distinct stories, deliberately not conflated:

## The demo runs bare-metal

Every exit gate in this repo (`experiments.smoke_llm_loop`, `experiments.ab`,
`experiments.endurance`, `dashboard/app.py` via `streamlit run`) is designed to run directly on a
machine that already has EnergyPlus installed and Ollama running - see the root
[`README.md`](../README.md) for setup. That is intentional: a live demo benefits from the
shortest possible path from "run a command" to "watch the loop," and a container build step adds
latency and failure surface for a one-off, one-machine presentation. **Do not containerize the
demo path.**

## Compose is the gateway-appliance deployment story

`Dockerfile` + `docker-compose.yml` at the repo root describe how HIVE would actually ship to a
building: one appliance process (EnergyPlus + guardian + planner + telemetry, `agent` service)
next to a read-only dashboard (`dashboard` service), sharing the telemetry/version-series volumes
so the dashboard can watch a live run exactly the way it already does bare-metal (WAL readers
never block the writer - see `CLAUDE.md`, "the dashboard is read-only over WAL").

Ollama is **not** a compose service. Model weights are gigabytes with their own upgrade/rollback
lifecycle, and a real site is far more likely to already run one Ollama instance shared by several
things than to want one baked into this appliance's image. `OLLAMA_HOST` points at it - by default
`http://host.docker.internal:11434` (the host machine).

```bash
# One-time: get the pinned EnergyPlus 24.1.0 Ubuntu .deb URL + its sha256sum from
# https://github.com/NREL/EnergyPlus/releases/tag/v24.1.0, then:
export EPLUS_DEB_URL="https://github.com/NREL/EnergyPlus/releases/download/v24.1.0/<asset>.deb"
export EPLUS_DEB_SHA256="<sha256sum of that asset>"

docker compose build
docker compose up
# dashboard: http://localhost:8501
```

Not part of CI (`.github/workflows/ci.yml` runs `ruff` + `pytest` only) - a Docker build needs
network access to fetch the EnergyPlus installer and is slow enough that it does not belong on
every push; build and smoke-test it manually before a release instead.

## Future: the real-world path beyond this repo

The building side of this project is EnergyPlus because that is what makes the closed loop
demonstrable without hardware. The intended path from here to an actual building:

- **BACnet points, not `Schedule:Constant`.** `simulation/prepare_idf.py` rewrites thermostat
  schedules into a writable `Schedule:Constant` because that is what the EnergyPlus runtime API
  exposes; a real BMS integration would have the guardian/executor write BACnet `Analog Value`
  points instead, behind the same `ControlInterface` (`common/models.py`) the live bus and the
  receding-horizon driver already share - the agent code would not need to change, only the third
  implementation of that interface.
- **Hardware-in-the-loop before full autonomy.** The guardian's own admission ("`patch_model` has
  a blast radius the guardian does not cover" - CLAUDE.md) is exactly the reason a real deployment
  would run supervised (a human approves patches, or patches are disabled entirely) before
  granting the self-heal loop (Session 9's L3) unattended write access to a live building's model.
- **Fleet supervision.** One Ollama instance, `keep_alive`-resident, already amortises across
  planning cycles for one building (`agent/planner.py`); the same instance can serve several
  appliances' digests, which is the natural way this scales to a portfolio of buildings without
  a GPU per site.
