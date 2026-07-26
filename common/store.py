"""SQLite telemetry store (WAL mode).

Shape of the problem: one writer (the simulation process) and one or more readers (the
Streamlit dashboard, the KPI extractor). That is exactly what WAL is for - readers never block
the writer and vice versa - which is why there is no database server here.

**Rule R3**: telemetry is written in batches, never one INSERT per timestep. A year at
10-minute steps is ~52k timesteps times N zones; an implicit transaction and fsync per row
would dominate wall-clock and can stall the EnergyPlus callback. The writer buffers in memory
and flushes on two triggers:

* every ``flush_every_timesteps`` timesteps (default 12 - two hours at a 10-minute step), and
* on **plan commit**, so a plan is never journalled ahead of the telemetry that justified it.

Ordering matters there: if the process died between the two, a plan with no supporting
telemetry is a mystery, whereas telemetry with no plan is just a gap. Flush first, then write
the plan.

This module owns the only write connection in the process.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import pandas as pd

from common.models import ApprovedPlan, BuildingState, GuardianEvent, Plan, SetpointPlan

DEFAULT_FLUSH_EVERY_TIMESTEPS = 12

SCHEMA = """
-- One row per (timestep, zone). Facility- and site-level channels are repeated on each of a
-- timestep's zone rows: it denormalises a handful of floats in exchange for every dashboard
-- query being a single SELECT with no join, which is the right trade at this scale.
CREATE TABLE IF NOT EXISTS telemetry (
    id                 INTEGER PRIMARY KEY,
    run_id             TEXT    NOT NULL,
    sim_time           TEXT    NOT NULL,
    zone               TEXT    NOT NULL,
    air_temp_c         REAL,
    pmv                REAL,
    occupancy          REAL,
    cooling_setpoint_c REAL,
    heating_setpoint_c REAL,
    outdoor_air_temp_c REAL,
    facility_power_w   REAL,
    facility_kwh_step  REAL
);
CREATE INDEX IF NOT EXISTS ix_telemetry_run  ON telemetry (run_id, sim_time);
CREATE INDEX IF NOT EXISTS ix_telemetry_zone ON telemetry (run_id, zone, sim_time);

-- Both the raw planner output and the guardian-approved result, distinguished by `stage`.
-- Keeping them in one table makes "what did the guardian change?" a self-join rather than a
-- cross-table reconciliation.
CREATE TABLE IF NOT EXISTS plans (
    plan_id       TEXT NOT NULL,
    stage         TEXT NOT NULL CHECK (stage IN ('proposed', 'approved')),
    run_id        TEXT,
    at            TEXT NOT NULL,
    planner_model TEXT,
    decision      TEXT,
    fallback      INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL,
    PRIMARY KEY (plan_id, stage)
);
CREATE INDEX IF NOT EXISTS ix_plans_at ON plans (at);

CREATE TABLE IF NOT EXISTS guardian_events (
    event_id TEXT PRIMARY KEY,
    run_id   TEXT,
    at       TEXT NOT NULL,
    plan_id  TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_guardian_at ON guardian_events (at);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    run_id            TEXT,
    at                TEXT NOT NULL,
    model             TEXT,
    latency_ms        REAL,
    ok                INTEGER NOT NULL DEFAULT 1,
    error             TEXT,
    prompt            TEXT,
    response          TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    retries           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_llm_at ON llm_calls (at);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    label      TEXT,
    notes      TEXT
);
"""

_PRAGMAS_WRITE = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)

_PRAGMAS_READ = (
    "PRAGMA query_only=ON",
    "PRAGMA busy_timeout=5000",
)

_TELEMETRY_COLUMNS = (
    "run_id",
    "sim_time",
    "zone",
    "air_temp_c",
    "pmv",
    "occupancy",
    "cooling_setpoint_c",
    "heating_setpoint_c",
    "outdoor_air_temp_c",
    "facility_power_w",
    "facility_kwh_step",
)

_INSERT_TELEMETRY = (
    f"INSERT INTO telemetry ({', '.join(_TELEMETRY_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _TELEMETRY_COLUMNS)})"
)


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the right pragmas. WAL is set once and persists in the file."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS_READ if read_only else _PRAGMAS_WRITE:
        conn.execute(pragma)
    return conn


#: Columns added after the original schema shipped. ``CREATE TABLE IF NOT EXISTS`` cannot
#: retrofit them onto a database created by an older build, so :func:`init_db` ALTERs them in
#: when absent. SQLite's ADD COLUMN is metadata-only - instant even on a month of telemetry.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("llm_calls", "prompt_tokens", "INTEGER"),
    ("llm_calls", "completion_tokens", "INTEGER"),
    ("llm_calls", "retries", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db_path: Path | str) -> None:
    """Create the schema if it does not exist, and migrate older files. Safe to repeat."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def reader(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Read-only connection for the dashboard and the KPI extractor."""
    conn = connect(db_path, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()


class TelemetryStore:
    """Buffered writer. The only object in the process that holds a write connection.

    ``record_timestep`` is cheap enough to call from inside the EnergyPlus callback: it appends
    one tuple per zone to a list and, at most once every ``flush_every_timesteps``, performs a
    single transaction.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        flush_every_timesteps: int = DEFAULT_FLUSH_EVERY_TIMESTEPS,
    ) -> None:
        if flush_every_timesteps < 1:
            raise ValueError("flush_every_timesteps must be >= 1")
        self.db_path = Path(db_path)
        self.flush_every_timesteps = flush_every_timesteps
        self._buffer: list[tuple] = []
        self._timesteps_buffered = 0
        self.rows_written = 0
        # The sim writes telemetry from the callback thread; the planner logs llm_calls/plans
        # from its worker thread. Both share this one connection, so every mutating method
        # serialises on this reentrant lock (write_* call flush(), hence RLock). Contention is
        # near-zero - planner writes are hourly - so the callback path pays nothing measurable.
        self._lock = threading.RLock()
        init_db(self.db_path)
        self._conn = connect(self.db_path)

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self) -> TelemetryStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Flush whatever is buffered and release the connection."""
        try:
            self.flush()
        finally:
            self._conn.close()

    @property
    def buffered_rows(self) -> int:
        return len(self._buffer)

    # -- telemetry ---------------------------------------------------------------------

    def record_timestep(
        self,
        run_id: str,
        state: BuildingState,
        *,
        facility_kwh_step: float | None = None,
    ) -> None:
        """Buffer one timestep: one row per zone. Flushes on the timestep threshold (R3)."""
        stamp = _iso(state.sim_time)
        with self._lock:
            for zone in state.zones:
                self._buffer.append(
                    (
                        run_id,
                        stamp,
                        zone.zone,
                        zone.air_temp_c,
                        zone.pmv,
                        zone.occupancy,
                        zone.cooling_setpoint_c,
                        zone.heating_setpoint_c,
                        state.outdoor_air_temp_c,
                        state.facility_power_w,
                        facility_kwh_step,
                    )
                )
            self._timesteps_buffered += 1
            if self._timesteps_buffered >= self.flush_every_timesteps:
                self.flush()

    def flush(self) -> int:
        """Write the buffer in a single transaction. Returns the number of rows written."""
        with self._lock:
            self._timesteps_buffered = 0
            if not self._buffer:
                return 0

            pending, self._buffer = self._buffer, []
            with self._transaction():
                self._conn.executemany(_INSERT_TELEMETRY, pending)
            self.rows_written += len(pending)
            return len(pending)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- journal -----------------------------------------------------------------------
    #
    # Low-frequency (one row per planning cycle, not per timestep), so written straight
    # through. Each plan write flushes telemetry first - see the module docstring.

    def write_plan(self, plan: Plan | SetpointPlan, *, run_id: str | None = None) -> None:
        """Journal a proposed plan - the raw LLM ``Plan`` or its lowered ``SetpointPlan``.

        Both carry ``plan_id`` / ``created_at`` / ``planner_model`` and serialise via
        ``model_dump_json``; the ``payload`` records whichever shape it was handed.
        """
        self.flush()
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO plans "
                "(plan_id, stage, run_id, at, planner_model, decision, fallback, payload) "
                "VALUES (?, 'proposed', ?, ?, ?, NULL, 0, ?)",
                (
                    plan.plan_id,
                    run_id,
                    _iso(plan.created_at),
                    plan.planner_model,
                    plan.model_dump_json(),
                ),
            )

    def write_approved_plan(self, approved: ApprovedPlan, *, run_id: str | None = None) -> None:
        """Journal what was actually actuated."""
        self.flush()
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO plans "
                "(plan_id, stage, run_id, at, planner_model, decision, fallback, payload) "
                "VALUES (?, 'approved', ?, ?, NULL, ?, ?, ?)",
                (
                    approved.plan_id,
                    run_id,
                    _iso(approved.approved_at),
                    str(approved.decision),
                    int(approved.fallback),
                    approved.model_dump_json(),
                ),
            )

    def write_guardian_event(self, event: GuardianEvent, *, run_id: str | None = None) -> None:
        """Journal a guardian decision. Written for every plan, including clean ones."""
        self.flush()
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO guardian_events "
                "(event_id, run_id, at, plan_id, decision, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    run_id,
                    _iso(event.at),
                    event.plan_id,
                    str(event.decision),
                    event.model_dump_json(),
                ),
            )

    def write_llm_call(
        self,
        *,
        model: str,
        latency_ms: float,
        ok: bool = True,
        error: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
        run_id: str | None = None,
        at: datetime | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retries: int = 0,
    ) -> None:
        """Journal one call to the planner - including the failures, which are the interesting
        ones when explaining a fallback. Token counts come from Ollama's own eval counters
        (``prompt_eval_count``/``eval_count``); they feed the dashboard's LLMOps panel."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO llm_calls "
                "(run_id, at, model, latency_ms, ok, error, prompt, response, "
                " prompt_tokens, completion_tokens, retries) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    _iso(at or datetime.now(UTC)),
                    model,
                    latency_ms,
                    int(ok),
                    error,
                    prompt,
                    response,
                    prompt_tokens,
                    completion_tokens,
                    int(retries),
                ),
            )

    def start_run(self, run_id: str, label: str = "", notes: str = "") -> None:
        """Mark the beginning of a simulation run."""
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, label, notes) "
                "VALUES (?, ?, ?, ?)",
                (run_id, _iso(datetime.now(UTC)), label, notes),
            )

    def end_run(self, run_id: str) -> None:
        """Mark a run finished. Flushes telemetry first so the run is self-consistent."""
        self.flush()
        with self._transaction():
            self._conn.execute(
                "UPDATE runs SET ended_at = ? WHERE run_id = ?",
                (_iso(datetime.now(UTC)), run_id),
            )


# --------------------------------------------------------------------------------------
# Reader side (dashboard, harnesses)
# --------------------------------------------------------------------------------------


def _read_sql(db_path: Path | str, sql: str, params: tuple) -> pd.DataFrame:
    with reader(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def read_telemetry(db_path: Path | str, run_id: str | None = None) -> pd.DataFrame:
    """Telemetry as a tidy frame, with ``sim_time`` parsed to datetime."""
    sql = "SELECT * FROM telemetry"
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    sql += " ORDER BY sim_time, zone"
    frame = _read_sql(db_path, sql, params)
    if not frame.empty:
        frame["sim_time"] = pd.to_datetime(frame["sim_time"], format="ISO8601")
    return frame


def read_plans(db_path: Path | str, run_id: str | None = None) -> pd.DataFrame:
    """Proposed and approved plans."""
    sql = "SELECT * FROM plans"
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    return _read_sql(db_path, sql + " ORDER BY at", params)


def read_guardian_events(db_path: Path | str, run_id: str | None = None) -> pd.DataFrame:
    """Guardian decisions, one row per reviewed plan."""
    sql = "SELECT * FROM guardian_events"
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    return _read_sql(db_path, sql + " ORDER BY at", params)


def read_llm_calls(db_path: Path | str, run_id: str | None = None) -> pd.DataFrame:
    """Planner calls, successes and failures alike."""
    sql = "SELECT * FROM llm_calls"
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    return _read_sql(db_path, sql + " ORDER BY at", params)


def read_runs(db_path: Path | str) -> pd.DataFrame:
    """Every run recorded in this database."""
    return _read_sql(db_path, "SELECT * FROM runs ORDER BY started_at", ())


def load_json_column(row: sqlite3.Row, column: str = "payload") -> dict:
    """Decode a journal ``payload`` column into a dict."""
    return json.loads(row[column])


__all__ = [
    "DEFAULT_FLUSH_EVERY_TIMESTEPS",
    "SCHEMA",
    "TelemetryStore",
    "connect",
    "init_db",
    "load_json_column",
    "read_guardian_events",
    "read_llm_calls",
    "read_plans",
    "read_runs",
    "read_telemetry",
    "reader",
]
