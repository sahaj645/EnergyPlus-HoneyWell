"""HIVE ops dashboard - ``streamlit run dashboard/app.py``.

Single page, five judge-facing sections, strictly **read-only** over the WAL telemetry SQLite
plus the A/B results export (``reports/results.json``) and the endurance checkpoint:

1. Headline strip - INR saved, kgCO2 avoided, total site kWh delta, comfort-violation %.
2. Cumulative-kWh race chart - baseline vs agent, tariff/carbon high-band hours shaded, the
   agent's pre-cooling windows overlaid so the strategy is visibly aligned with the bands.
3. Per-zone PMV comfort strip with the +/-0.5 envelope.
4. Decision journal - sim-time, trigger, ECMs, rationale, guardian verdict, cache hit vs call.
5. LLMOps - calls made vs avoided, tokens, latency percentiles, retries, verdict counts,
   timeouts, INR-saved vs INR-inference, endurance stat card.

Everything on screen is read from the database/export files - **no number is hardcoded**. The
one constant is the *labeled* API price assumption used for the INR-inference contrast, which
is an assumption, not a result, and is captioned as such next to the figure it produces.

Aggregation happens in SQL (GROUP BY / filtered SELECTs against the read-only connection), so
the page never drags the raw telemetry table into pandas; a week-long run loads well inside
the 2 s budget. Reads use ``common.store.reader`` (``query_only`` pragma), which is what makes
pointing this at the database of a *running* endurance sim safe - WAL readers never block the
writer.

``?section=N`` renders exactly one section (used by ``dashboard/export_screens.py`` for the
per-section screenshots); no param renders all five. Auto-refresh (~5 s) is a
``st.fragment(run_every=...)`` around the page body, toggleable from the sidebar.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ``streamlit run dashboard/app.py`` executes this file with sys.path[0] = dashboard/, not the
# repo root - so the repo's own packages would not resolve. Shim the root in first.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.digest import band, terciles  # noqa: E402
from common.config import Settings  # noqa: E402
from common.log import get_logger  # noqa: E402
from common.store import reader  # noqa: E402
from experiments.kpis import load_carbon, load_tariff  # noqa: E402

log = get_logger("dashboard.app")

REFRESH_SECONDS = 5

#: LABELED ASSUMPTION for the INR-inference contrast only (not a measured result): a
#: representative hosted-API price and FX rate, applied to the *logged* token counts. The
#: local Ollama cost is ~0 by construction; this shows what the same traffic would have cost.
API_USD_PER_1M_PROMPT_TOKENS = 0.15
API_USD_PER_1M_COMPLETION_TOKENS = 0.60
INR_PER_USD = 84.0

_VERDICT_BADGE = {
    "accepted": "🟢 accepted",
    "clamped": "🟡 clipped",
    "clipped": "🟡 clipped",
    "rejected": "🔴 rejected",
    "fallback": "⚪ fallback",
}

# "Hive" palette - amber on charcoal. Chart series pull from here so the whole page reads as
# one system; the guardian verdict colours match the journal badges.
AMBER = "#f59f00"
AMBER_DIM = "#b8860b"
GREY = "#868e96"
GREEN = "#37b24d"
RED = "#f03e3e"
BLUE = "#4dabf7"
ARM_COLORS = {"baseline": GREY, "constant": AMBER_DIM, "agent": AMBER}

_CSS = """
<style>
:root { --amber: #f59f00; --panel: #1b1e29; --line: rgba(245,159,0,.22); }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px; }
#MainMenu, footer, header [data-testid="stToolbar"] { visibility: hidden; }

/* Hero */
.hive-hero { display:flex; align-items:center; gap:.85rem; padding:.2rem 0 1rem 0;
  border-bottom:1px solid var(--line); margin-bottom:1.4rem; }
.hive-hero .logo { font-size:2.1rem; filter:drop-shadow(0 0 8px rgba(245,159,0,.5)); }
.hive-hero .title { font-size:1.7rem; font-weight:800; letter-spacing:-.02em;
  background:linear-gradient(90deg,#ffd43b,#f59f00); -webkit-background-clip:text;
  -webkit-text-fill-color:transparent; }
.hive-hero .sub { color:#909296; font-size:.86rem; margin-top:.15rem; }

/* Section headers */
.hive-sec { display:flex; align-items:baseline; gap:.6rem; margin:.2rem 0 .9rem 0; }
.hive-sec .n { font-size:.72rem; font-weight:700; color:#0b0c10; background:var(--amber);
  border-radius:6px; padding:.12rem .5rem; letter-spacing:.05em; }
.hive-sec .t { font-size:1.22rem; font-weight:700; color:#f1f3f5; }
.hive-sec .d { color:#868e96; font-size:.82rem; }

/* Metric cards */
[data-testid="stMetric"] { background:var(--panel); border:1px solid rgba(255,255,255,.06);
  border-left:3px solid var(--amber); border-radius:12px; padding:.85rem 1rem .7rem 1rem;
  box-shadow:0 1px 3px rgba(0,0,0,.35); }
[data-testid="stMetricValue"] { font-size:1.7rem; font-weight:750; }
[data-testid="stMetricLabel"] p { color:#909296; font-size:.78rem; font-weight:600;
  letter-spacing:.02em; text-transform:uppercase; }

/* Dataframe + charts sit on panels */
[data-testid="stDataFrame"], .stPlotlyChart, [data-testid="stVegaLiteChart"] {
  border:1px solid rgba(255,255,255,.06); border-radius:12px; overflow:hidden; }
hr { border-color:var(--line); }
[data-testid="stCaptionContainer"] { color:#868e96; }
[data-testid="stSidebar"] { border-right:1px solid var(--line); }
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _hero() -> None:
    st.markdown(
        '<div class="hive-hero"><span class="logo">🐝</span>'
        '<div><div class="title">HIVE — building-energy control</div>'
        '<div class="sub">LLM planner · deterministic guardian · EnergyPlus digital twin · '
        "live ops view</div></div></div>",
        unsafe_allow_html=True,
    )


def _section(number: int, title: str, detail: str = "") -> None:
    st.markdown(
        f'<div class="hive-sec"><span class="n">{number}</span>'
        f'<span class="t">{title}</span><span class="d">{detail}</span></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------------------
# Data discovery + loading (cached ~one refresh interval)
# --------------------------------------------------------------------------------------


def _discover_dbs(settings: Settings) -> list[Path]:
    """Every plausible telemetry DB, most recently written first."""
    roots = [settings.repo_root, settings.repo_root / "experiments" / "results"]
    found: set[Path] = set()
    for root in roots:
        if root.is_dir():
            found.update(root.glob("*.sqlite"))
            found.update(root.glob("*/hive.sqlite"))
            found.update(root.glob("*/*/hive.sqlite"))  # endurance chunk_*/ and ab arms
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _discover_checkpoints(settings: Settings) -> list[Path]:
    root = settings.repo_root / "experiments" / "results"
    if not root.is_dir():
        return []
    return sorted(root.glob("endurance_*/checkpoint.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _mtime(path: Path | None) -> float:
    return path.stat().st_mtime if path and path.is_file() else 0.0


def _short_ts(value: str | None) -> str:
    """Render an ISO timestamp as a clean 'YYYY-MM-DD HH:MM' for captions, not raw ISO."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


def _run_label(path_str: str) -> str:
    """A short, human label for a run artifact instead of its full Windows path.

    ``.../results/ab_20260726T225105/agent/hive.sqlite`` -> ``ab_20260726T225105 · agent``;
    ``.../results/endurance_demo/checkpoint.json``       -> ``endurance_demo``.
    """
    path = Path(path_str)
    parts = path.parts
    if "results" in parts:
        tail = parts[parts.index("results") + 1:]
        stem = [p for p in tail if not p.endswith((".sqlite", ".json"))]
        if stem:
            return " · ".join(stem)
    return path.stem


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def load_results(path_str: str, _mtime_key: float) -> dict | None:
    path = Path(path_str)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def load_json_file(path_str: str, _mtime_key: float) -> dict | None:
    path = Path(path_str)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sql(db: str, query: str, params: tuple = ()) -> pd.DataFrame:
    with reader(db) as conn:
        return pd.read_sql_query(query, conn, params=params)


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_comfort_series(db: str, _mtime_key: float) -> pd.DataFrame:
    """Per-zone PMV series. SQL-side filter to rows that actually carry a PMV."""
    frame = _sql(db, "SELECT sim_time, zone, pmv FROM telemetry "
                     "WHERE pmv IS NOT NULL ORDER BY sim_time")
    if not frame.empty:
        frame["sim_time"] = pd.to_datetime(frame["sim_time"], format="ISO8601")
    return frame


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_comfort_violation_pct(db: str, _mtime_key: float) -> float | None:
    """Occupied intervals with |PMV| > 0.5, as a % - aggregated entirely in SQL."""
    frame = _sql(db, """
        SELECT 100.0 * SUM(CASE WHEN ABS(pmv) > 0.5 THEN 1 ELSE 0 END) / COUNT(*) AS pct
        FROM telemetry WHERE occupancy > 0 AND pmv IS NOT NULL
    """)
    value = frame.iloc[0]["pct"] if not frame.empty else None
    return None if value is None or pd.isna(value) else float(value)


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_journal(db: str, _mtime_key: float, limit: int = 300) -> pd.DataFrame:
    """Decision journal: proposed plans LEFT JOINed to their LLM call and guardian verdict.

    A proposed plan with no ``llm_calls`` row at the same instant was served by the cache -
    the planner stamps ``plan.created_at`` and the call log with the same sim-time ``now``,
    so equality on ``at`` within the same run is the join key.
    """
    return _sql(db, """
        SELECT p.at, p.plan_id, p.payload, p.corrects_plan_id,
               l.id IS NOT NULL AS was_llm_call,
               g.decision AS verdict, g.payload AS guardian_payload
        FROM plans p
        LEFT JOIN llm_calls l ON l.run_id = p.run_id AND l.at = p.at
        LEFT JOIN guardian_events g ON g.plan_id = p.plan_id
        WHERE p.stage = 'proposed'
        ORDER BY p.at DESC LIMIT ?
    """, (limit,))


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_watchdog_events(db: str, _mtime_key: float) -> pd.DataFrame:
    return _sql(db, "SELECT at, payload FROM guardian_events "
                    "WHERE plan_id = 'watchdog' ORDER BY at DESC LIMIT 50")


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_llm_stats(db: str, _mtime_key: float) -> dict:
    """LLMOps aggregates, computed in SQL except the percentiles (SQLite has none)."""
    agg = _sql(db, """
        SELECT COUNT(*) AS calls, COALESCE(SUM(ok), 0) AS ok_calls,
               COALESCE(SUM(retries), 0) AS retries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens
        FROM llm_calls
    """).iloc[0].to_dict()
    latency = _sql(db, "SELECT latency_ms FROM llm_calls WHERE latency_ms IS NOT NULL")
    series = latency["latency_ms"]
    agg["p50_ms"] = float(series.quantile(0.50)) if not series.empty else None
    agg["p95_ms"] = float(series.quantile(0.95)) if not series.empty else None
    verdicts = _sql(db, "SELECT decision, COUNT(*) AS n FROM guardian_events "
                        "WHERE plan_id != 'watchdog' GROUP BY decision")
    agg["verdicts"] = dict(zip(verdicts["decision"], verdicts["n"], strict=True))
    cached = _sql(db, """
        SELECT COUNT(*) AS n FROM plans p
        LEFT JOIN llm_calls l ON l.run_id = p.run_id AND l.at = p.at
        WHERE p.stage = 'proposed' AND l.id IS NULL
    """)
    agg["cache_hits_db"] = int(cached.iloc[0]["n"]) if not cached.empty else 0
    return agg


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def q_precool_windows(db: str, _mtime_key: float) -> list[tuple[str, str]]:
    """(start, end) of every action in a proposed plan that declared the precool ECM."""
    frame = _sql(db, "SELECT payload FROM plans WHERE stage = 'proposed'")
    windows: list[tuple[str, str]] = []
    for payload in frame["payload"]:
        try:
            plan = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if "precool" not in plan.get("ecms", []):
            continue
        for action in plan.get("actions", []):
            if action.get("start") and action.get("end"):
                windows.append((action["start"], action["end"]))
    return windows


# --------------------------------------------------------------------------------------
# Section 1 - headline strip
# --------------------------------------------------------------------------------------


def sec_headline(ctx: Ctx) -> None:
    _section(1, "Headline", "agent vs unmodified baseline, identical week")
    if ctx.results is None:
        st.info("No A/B export found - run `python -m experiments.ab` then "
                "`python -m experiments.report`.")
        return
    headline = next((d for d in ctx.results.get("deltas", [])
                     if d["from_arm"] == "baseline" and d["to_arm"] == "agent"), None)
    if headline is None:
        st.warning("results.json has no baseline→agent delta.")
        return

    live_pct = q_comfort_violation_pct(str(ctx.db), _mtime(ctx.db)) if ctx.db else None
    export_pct = _comfort_pct_from_results(ctx.results, "agent")
    comfort_pct = live_pct if live_pct is not None else export_pct

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("₹ saved", f"₹ {headline['cost_saved_inr']:,.0f}",
              help="Baseline cost minus agent cost over the identical RunPeriod.")
    c2.metric("kgCO₂ avoided", f"{headline['carbon_avoided_kg']:,.1f}")
    kwh_pct = headline.get("site_kwh_pct")
    c3.metric("Total site kWh Δ", f"{kwh_pct:+.1f}%" if kwh_pct is not None else "n/a",
              help=f"{headline['site_kwh_from']:.0f} → {headline['site_kwh_to']:.0f} kWh")
    baseline_pct = _comfort_pct_from_results(ctx.results, "baseline")
    delta = (None if comfort_pct is None or baseline_pct is None
             else comfort_pct - baseline_pct)
    c4.metric("Comfort violations (occupied)",
              f"{comfort_pct:.1f}%" if comfort_pct is not None else "n/a",
              delta=f"{delta:+.1f}% vs baseline" if delta is not None else None,
              delta_color="inverse")
    st.caption(f"Run period **{ctx.results.get('spec_label')}** · "
               f"generated {_short_ts(ctx.results.get('generated_at'))}")


def _comfort_pct_from_results(results: dict, arm: str) -> float | None:
    rows = results.get("arms", {}).get(arm, {}).get("comfort", [])
    for row in rows:
        if row.get("zone") == "ALL":
            return float(row["pct_of_occupied_hours"])
    return None


# --------------------------------------------------------------------------------------
# Section 2 - cumulative-kWh race chart
# --------------------------------------------------------------------------------------


def sec_race(ctx: Ctx) -> None:
    import plotly.graph_objects as go

    _section(2, "Cumulative kWh race", "lower line = less energy · shading = costly/dirty hours")
    if ctx.results is None:
        st.info("Needs the A/B export (reports/results.json).")
        return

    fig = go.Figure()
    span: tuple[datetime, datetime] | None = None
    for arm, arm_data in ctx.results.get("arms", {}).items():
        series = arm_data.get("cumulative_kwh", [])
        if not series:
            continue
        times = pd.to_datetime([point[0] for point in series])
        values = [point[1] for point in series]
        emphasis = arm == "agent"
        fig.add_trace(go.Scatter(
            x=times, y=values, name=arm, mode="lines",
            line={"color": ARM_COLORS.get(arm, GREY), "width": 3.5 if emphasis else 2},
            fill="tozeroy" if emphasis else None,
            fillcolor="rgba(245,159,0,.07)" if emphasis else None))
        lo, hi = times.min(), times.max()
        span = (lo, hi) if span is None else (min(span[0], lo), max(span[1], hi))

    if span is not None:
        _shade_bands(fig, span, ctx)
        for start, end in ctx.precool_windows:
            fig.add_vrect(x0=start, x1=end, fillcolor=BLUE, opacity=0.16,
                          layer="below", line_width=0)
    _style_fig(fig, height=430, yaxis_title="cumulative kWh")
    st.plotly_chart(fig, width="stretch")
    st.caption("Shaded: 🟥 high-tariff hours · ⬛ high-carbon hours · 🟦 agent pre-cooling "
               "windows (from the plan journal). Pre-cooling ahead of shaded bands is the "
               "strategy working.")


def _style_fig(fig, *, height: int, yaxis_title: str) -> None:
    """Consistent dark-transparent plotly styling so every chart matches the page."""
    fig.update_layout(
        height=height,
        margin={"l": 46, "r": 18, "t": 26, "b": 38},
        yaxis_title=yaxis_title,
        legend={"orientation": "h", "y": 1.1, "x": 0},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#c1c2c5", "size": 12},
        hovermode="x unified",
    )
    grid = "rgba(255,255,255,.06)"
    fig.update_xaxes(gridcolor=grid, zeroline=False)
    fig.update_yaxes(gridcolor=grid, zerolinecolor="rgba(255,255,255,.15)")


def _merge_high_hours(curve: dict[int, float], span: tuple[datetime, datetime]
                      ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous 'high'-band hours across the span, merged into as few intervals as possible.

    One vrect per merged block instead of one per hour: ~a dozen shapes instead of hundreds,
    which is both cleaner to read and light enough to render in a headless browser.
    """
    lo, hi = terciles(curve)
    hours = pd.date_range(span[0].floor("h"), span[1].ceil("h"), freq="h")
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    run_start: pd.Timestamp | None = None
    for hour in hours:
        is_high = band(curve.get(hour.hour), lo, hi) == "high"
        if is_high and run_start is None:
            run_start = hour
        elif not is_high and run_start is not None:
            intervals.append((run_start, hour))
            run_start = None
    if run_start is not None:
        intervals.append((run_start, hours[-1]))
    return intervals


def _shade_bands(fig, span: tuple[datetime, datetime], ctx: Ctx) -> None:
    """Shade merged tariff/carbon 'high' bands (same terciles the digest uses)."""
    for start, end in _merge_high_hours(ctx.tariff, span):
        fig.add_vrect(x0=start, x1=end, fillcolor=RED, opacity=0.09,
                      layer="below", line_width=0)
    for start, end in _merge_high_hours(ctx.carbon, span):
        fig.add_vrect(x0=start, x1=end, fillcolor="#495057", opacity=0.12,
                      layer="below", line_width=0)


# --------------------------------------------------------------------------------------
# Section 3 - per-zone PMV comfort strip
# --------------------------------------------------------------------------------------


def sec_comfort(ctx: Ctx) -> None:
    import plotly.graph_objects as go

    _section(3, "Comfort — per-zone PMV", "green band is the ±0.5 comfort envelope")
    if ctx.db is None:
        st.info("No telemetry database found.")
        return
    frame = q_comfort_series(str(ctx.db), _mtime(ctx.db))
    if frame.empty:
        st.info(f"`{ctx.db.name}` has no PMV telemetry yet.")
        return

    fig = go.Figure()
    fig.add_hrect(y0=-0.5, y1=0.5, fillcolor=GREEN, opacity=0.12, line_width=0,
                  annotation_text="comfort envelope ±0.5", annotation_position="top left")
    for zone, group in frame.groupby("zone", sort=True):
        fig.add_trace(go.Scatter(x=group["sim_time"], y=group["pmv"],
                                 name=zone, mode="lines", line={"width": 2}))
    fig.add_hline(y=0.5, line_dash="dot", line_color=RED)
    fig.add_hline(y=-0.5, line_dash="dot", line_color=BLUE)
    _style_fig(fig, height=360, yaxis_title="Fanger PMV")
    st.plotly_chart(fig, width="stretch")
    st.caption("Every occupied point inside the ±0.5 band is a comfortable timestep. "
               "Read-only over WAL — safe to view while a run is still writing.")


# --------------------------------------------------------------------------------------
# Section 4 - decision journal
# --------------------------------------------------------------------------------------


def sec_journal(ctx: Ctx) -> None:
    _section(4, "Decision journal", "every planning cycle: trigger → ECMs → guardian verdict")
    if ctx.db is None:
        st.info("No telemetry database found.")
        return
    raw = q_journal(str(ctx.db), _mtime(ctx.db))
    watchdog = q_watchdog_events(str(ctx.db), _mtime(ctx.db))
    if raw.empty and watchdog.empty:
        st.info("No plans journalled yet.")
        return

    rows = [_journal_row(record) for record in raw.to_dict("records")]
    for record in watchdog.to_dict("records"):
        note = ""
        try:
            note = json.loads(record["payload"]).get("note", "")
        except (TypeError, json.JSONDecodeError):
            pass
        rows.append({"sim time": record["at"], "trigger": "watchdog", "ecms": "",
                     "rationale": note, "verdict": _VERDICT_BADGE["fallback"],
                     "source": "—"})
    frame = pd.DataFrame(rows).sort_values("sim time", ascending=False)
    st.dataframe(frame, width="stretch", height=380, hide_index=True)
    st.caption("🟢 accepted · 🟡 clipped · 🔴 rejected · ⚪ fallback — verdicts from "
               "`guardian_events`; 💾 cache hit vs 🤖 LLM call from the `plans`⋈`llm_calls` "
               "join. \"↳ corrects <id>\" (L2) marks a plan generated from the previous "
               "clipped/rejected plan's guardian reasons - `plans.corrects_plan_id`.")


def _journal_row(record: dict) -> dict:
    plan: dict = {}
    try:
        plan = json.loads(record["payload"])
    except (TypeError, json.JSONDecodeError):
        pass
    rationales = "; ".join(
        a.get("rationale", "") for a in plan.get("actions", []) if a.get("rationale")
    )
    verdict_key = str(record.get("verdict") or "").lower()
    guardian_note = ""
    try:
        guardian_note = json.loads(record.get("guardian_payload") or "{}").get("note", "")
    except (TypeError, json.JSONDecodeError):
        pass
    corrects = record.get("corrects_plan_id")
    trigger = plan.get("trigger", "?")
    if corrects:
        trigger = f"{trigger} ↳ corrects {corrects[:12]}"
    return {
        "sim time": record["at"],
        "trigger": trigger,
        "ecms": ", ".join(plan.get("ecms", [])),
        "rationale": rationales or guardian_note,
        "verdict": _VERDICT_BADGE.get(verdict_key, "· pending"),
        "source": "💾 cache" if not record["was_llm_call"] else "🤖 LLM call",
    }


# --------------------------------------------------------------------------------------
# Section 5 - LLMOps
# --------------------------------------------------------------------------------------


def sec_llmops(ctx: Ctx) -> None:
    _section(5, "LLMOps", "what the model cost, and what the cache saved")
    if ctx.db is None:
        st.info("No telemetry database found.")
        return
    stats = q_llm_stats(str(ctx.db), _mtime(ctx.db))
    cache_stats = ctx.cache_stats or {}

    calls = int(stats["calls"])
    avoided = int(cache_stats.get("calls_avoided", stats["cache_hits_db"]))
    holds = int(cache_stats.get("holds", 0))
    total = calls + avoided
    avoided_pct = 100.0 * avoided / total if total else 0.0

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("LLM calls made", f"{calls}")
    c2.metric("Calls avoided", f"{avoided} ({avoided_pct:.0f}%)",
              help=f"cache hits; plus {holds} hold(s) that skipped planning entirely")
    c3.metric("Tokens in / out",
              f"{int(stats['prompt_tokens']):,} / {int(stats['completion_tokens']):,}")
    p50, p95 = stats["p50_ms"], stats["p95_ms"]
    c4.metric("Latency p50 / p95",
              f"{p50 / 1000:.1f}s / {p95 / 1000:.1f}s" if p50 is not None else "n/a")
    c5.metric("Schema retries", f"{int(stats['retries'])}")
    timeouts = (ctx.checkpoint or {}).get("cumulative", {}).get("timeouts")
    c6.metric("Timeouts", f"{timeouts}" if timeouts is not None else "n/a")

    import plotly.graph_objects as go

    verdict_color = {"accepted": GREEN, "clamped": AMBER, "clipped": AMBER,
                     "rejected": RED, "fallback": GREY}
    left, right = st.columns(2)
    with left:
        st.markdown("**Guardian verdicts**")
        verdicts = stats["verdicts"]
        if verdicts:
            fig = go.Figure(go.Bar(
                x=list(verdicts.keys()), y=list(verdicts.values()),
                marker_color=[verdict_color.get(str(k).lower(), GREY) for k in verdicts],
                text=list(verdicts.values()), textposition="outside"))
            _style_fig(fig, height=230, yaxis_title="plans")
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch")
        else:
            st.caption("no guardian events yet")
    with right:
        st.markdown("**₹ saved vs ₹ inference**")
        saved = None
        if ctx.results is not None:
            headline = next((d for d in ctx.results.get("deltas", [])
                             if d["from_arm"] == "baseline" and d["to_arm"] == "agent"), None)
            saved = headline["cost_saved_inr"] if headline else None
        api_inr = ((stats["prompt_tokens"] / 1e6) * API_USD_PER_1M_PROMPT_TOKENS
                   + (stats["completion_tokens"] / 1e6) * API_USD_PER_1M_COMPLETION_TOKENS
                   ) * INR_PER_USD
        labels = ["saved (A/B)", "inference (local Ollama)", "inference (API-equiv.)"]
        values = [saved if saved is not None else 0.0, 0.0, api_inr]
        fig = go.Figure(go.Bar(
            x=labels, y=values, marker_color=[GREEN, GREY, AMBER_DIM],
            text=[f"₹{v:,.0f}" for v in values], textposition="outside"))
        _style_fig(fig, height=230, yaxis_title="₹")
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, width="stretch")
        st.caption(f"API-equivalent is a **labeled assumption**: "
                   f"${API_USD_PER_1M_PROMPT_TOKENS}/M in + "
                   f"${API_USD_PER_1M_COMPLETION_TOKENS}/M out × ₹{INR_PER_USD:.0f}/USD "
                   f"applied to the logged token counts. Local inference ≈ ₹0.")

    if ctx.checkpoint:
        cumulative = ctx.checkpoint.get("cumulative", {})
        st.markdown("**Endurance run**")
        e1, e2, e3, e4, e5 = st.columns(5)
        chunk_days = max(1, ctx.checkpoint.get("chunk_days", 1))
        chunks_total = -(-ctx.checkpoint.get("days_total", 0) // chunk_days)
        e1.metric("Chunks done", f"{ctx.checkpoint.get('next_chunk_index', 0)}/{chunks_total}")
        e2.metric("Timesteps", f"{cumulative.get('timesteps', 0):,}")
        e3.metric("Planner calls", f"{cumulative.get('planner_calls', 0):,}")
        e4.metric("Guardian fallbacks", f"{cumulative.get('guardian_fallbacks', 0):,}")
        e5.metric("Unhandled exceptions", f"{cumulative.get('unhandled_exceptions', 0)}")
        st.caption(f"Resumable day-chunked endurance run · updated "
                   f"{_short_ts(ctx.checkpoint.get('updated_at'))}")


# --------------------------------------------------------------------------------------
# Page assembly
# --------------------------------------------------------------------------------------


@dataclass
class Ctx:
    """Everything the sections read, loaded once per refresh."""

    db: Path | None
    results_path: Path
    results: dict | None
    checkpoint_path: Path | None
    checkpoint: dict | None
    cache_stats: dict | None
    tariff: dict[int, float]
    carbon: dict[int, float]
    precool_windows: list[tuple[str, str]] = field(default_factory=list)


SECTIONS = {
    1: sec_headline,
    2: sec_race,
    3: sec_comfort,
    4: sec_journal,
    5: sec_llmops,
}


def _build_ctx(settings: Settings, db: Path | None, results_path: Path,
               checkpoint_path: Path | None) -> Ctx:
    checkpoint = (load_json_file(str(checkpoint_path), _mtime(checkpoint_path))
                  if checkpoint_path else None)
    cache_stats = None
    for candidate in filter(None, [
        checkpoint_path.parent / "cache_stats.json" if checkpoint_path else None,
        db.parent / "hive_cache_stats.json" if db else None,
    ]):
        cache_stats = load_json_file(str(candidate), _mtime(candidate))
        if cache_stats:
            break
    return Ctx(
        db=db,
        results_path=results_path,
        results=load_results(str(results_path), _mtime(results_path)),
        checkpoint_path=checkpoint_path,
        checkpoint=checkpoint,
        cache_stats=cache_stats,
        tariff=load_tariff(settings.data_dir / "tariff.csv"),
        carbon=load_carbon(settings.data_dir / "carbon_intensity.csv"),
        precool_windows=(q_precool_windows(str(db), _mtime(db)) if db else []),
    )


def main() -> None:
    st.set_page_config(page_title="HIVE ops", page_icon="🐝", layout="wide")
    _inject_css()
    settings = Settings.from_env()

    with st.sidebar:
        st.title("🐝 HIVE")
        st.caption("LLM planner · deterministic guardian · EnergyPlus twin")
        dbs = _discover_dbs(settings)
        db = (Path(st.selectbox("Telemetry DB", [str(p) for p in dbs], format_func=_run_label))
              if dbs else None)
        results_path = settings.repo_root / "reports" / "results.json"
        checkpoints = _discover_checkpoints(settings)
        checkpoint_path = (Path(st.selectbox("Endurance checkpoint",
                                             [str(p) for p in checkpoints], format_func=_run_label))
                           if checkpoints else None)
        auto = st.toggle(f"Auto-refresh ({REFRESH_SECONDS}s)", value=True)

    try:
        only = int(st.query_params.get("section", 0))
    except (TypeError, ValueError):
        only = 0
    # ?static=1 pins a single stable render (no auto-refresh) - used by the screenshot exporter
    # so a capture never lands mid-rerun.
    static = str(st.query_params.get("static", "")).lower() in ("1", "true")

    @st.fragment(run_every=None if (static or not auto) else f"{REFRESH_SECONDS}s")
    def body() -> None:
        started = datetime.now()
        ctx = _build_ctx(settings, db, results_path, checkpoint_path)
        full_page = only not in SECTIONS
        if full_page:
            _hero()
        targets = list(SECTIONS.values()) if full_page else [SECTIONS[only]]
        for section in targets:
            section(ctx)
            if len(targets) > 1:
                st.divider()
        st.caption(f"Rendered in {(datetime.now() - started).total_seconds():.2f}s · "
                   f"live @ {datetime.now():%H:%M:%S} · read-only over WAL SQLite")

    body()


# Streamlit executes this script with ``__name__ == "__main__"``; a plain ``import
# dashboard.app`` (the test suite, CI - machines with no run data) stays side-effect-free.
if __name__ == "__main__":
    main()
