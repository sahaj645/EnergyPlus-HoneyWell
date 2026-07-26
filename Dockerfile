# HIVE agent image: python:3.11-slim + EnergyPlus 24.1.0.
#
# This is the *gateway appliance* half of the deployment story (see deploy/README.md): the
# closed control loop (EnergyPlus + guardian + planner + telemetry) in one container, talking to
# an Ollama instance that stays on the host (or another container) via OLLAMA_HOST - not baked
# into this image, because the model weights are gigabytes and belong in their own lifecycle.
#
# EnergyPlus is NOT on PyPI (see common/eplus_path.py) and ships as a platform installer, not a
# wheel. It is installed here from NREL's own .deb release asset - pin EPLUS_DEB_URL /
# EPLUS_DEB_SHA256 to the exact asset for the Ubuntu base this image uses (match
# EnergyPlus/releases for v24.1.0); the placeholders below are deliberately not filled with a
# guessed URL/hash, since a wrong one fails the build loudly (checksum mismatch) rather than
# silently installing the wrong binary.

FROM python:3.11-slim AS base

ARG EPLUS_VERSION=24.1.0
ARG EPLUS_INSTALL_DIR=/usr/local/EnergyPlus-24-1-0
# Fill these from https://github.com/NREL/EnergyPlus/releases/tag/v24.1.0 - the Ubuntu 22.04
# x86_64 .deb asset for this exact version, and its sha256sum from the release's checksums file.
ARG EPLUS_DEB_URL=""
ARG EPLUS_DEB_SHA256=""

ENV ENERGYPLUS_DIR=${EPLUS_INSTALL_DIR} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps EnergyPlus's installer and eppy need; curl only to fetch the .deb.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libx11-6 libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install EnergyPlus from the pinned .deb. Fails the build (not silently skips) if the two ARGs
# above were left blank - better than shipping an image that looks built but has no simulator.
RUN set -eux; \
    if [ -z "$EPLUS_DEB_URL" ] || [ -z "$EPLUS_DEB_SHA256" ]; then \
        echo "EPLUS_DEB_URL / EPLUS_DEB_SHA256 build-args are required - see the ARG comments" \
             "above for where to get them" >&2; \
        exit 1; \
    fi; \
    curl -fsSL -o /tmp/energyplus.deb "$EPLUS_DEB_URL"; \
    echo "${EPLUS_DEB_SHA256}  /tmp/energyplus.deb" | sha256sum -c -; \
    apt-get update && apt-get install -y --no-install-recommends /tmp/energyplus.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/energyplus.deb

COPY pyproject.toml README.md ./
COPY agent/ agent/
COPY common/ common/
COPY dashboard/ dashboard/
COPY experiments/ experiments/
COPY guardian/ guardian/
COPY mcp_server/ mcp_server/
COPY simulation/ simulation/
COPY data/ data/

RUN pip install --upgrade pip && pip install -e .

# `simulation/` holds the fetched/prepared IDF+EPW+model index (`fetch_assets`/`prepare_idf`,
# gitignored on purpose - see CLAUDE.md) and `experiments/results/` holds every run's telemetry
# SQLite + A/B export. Both are mounted as named volumes in docker-compose.yml so they survive a
# container restart and so the dashboard container can read the same files read-only.
VOLUME ["/app/simulation", "/app/experiments/results"]

ENV OLLAMA_HOST=http://host.docker.internal:11434

ENTRYPOINT ["python", "-m"]
CMD ["experiments.smoke_llm_loop"]
