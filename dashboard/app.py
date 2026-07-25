"""HIVE dashboard - ``streamlit run dashboard/app.py``.

Panels planned: live zone temperatures against the comfort band, tariff and grid-carbon
curves with actuation overlaid, the guardian's intervention log, and cumulative KPIs for the
agent arm versus the baseline arm.

Scaffold only: renders a placeholder so the app boots.
"""

from __future__ import annotations

import streamlit as st

from common.config import Settings


def main() -> None:
    settings = Settings.from_env()

    st.set_page_config(page_title="HIVE", page_icon="🐝", layout="wide")
    st.title("HIVE — building-energy control agent")
    st.caption("LLM planner · deterministic guardian · EnergyPlus digital twin")

    st.info(
        "Scaffold. No telemetry is being written yet — the control loop has not been "
        "implemented. This page will read the run database read-only once it exists."
    )

    with st.sidebar:
        st.subheader("Configuration")
        st.write({"database": str(settings.db_path), "model": settings.ollama_model})

    st.subheader("Panels (planned)")
    st.markdown(
        "- Zone temperatures vs comfort band\n"
        "- Tariff & grid carbon with actuation overlay\n"
        "- Guardian interventions (clamped / rejected / fallback)\n"
        "- KPIs: energy, cost, carbon, peak demand — agent vs baseline"
    )


if __name__ == "__main__":
    main()
