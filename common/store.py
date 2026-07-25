"""SQLite telemetry store (WAL mode).

Shape of the problem: one writer (the simulation process) and one or more readers (the
Streamlit dashboard, the KPI extractor). That is exactly what WAL is for - readers never block
the writer and vice versa - which is why there is no database server here.

**Rule R3**: telemetry is written in batches, never one INSERT per timestep. A year at
10-minute steps is ~52k timesteps times N channels; an implicit transaction and fsync per row
would dominate wall-clock and can stall the EnergyPlus callback. Buffer, then flush inside a
single transaction. This module owns the only write connection in the process.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from common.models import ApprovedPlan, GuardianEvent, KpiSnapshot, Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id        INTEGER PRIMARY KEY,
    sim_time  TEXT    NOT NULL,
    zone      TEXT,
    channel   TEXT    NOT NULL,
    value     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_telemetry_time    ON telemetry (sim_time);
CREATE INDEX IF NOT EXISTS ix_telemetry_channel ON telemetry (channel, sim_time);

CREATE TABLE IF NOT EXISTS plans (
    plan_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    model      TEXT,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_plans (
    plan_id     TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    decision    TEXT NOT NULL,
    fallback    INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_approved_time ON approved_plans (approved_at);

CREATE TABLE IF NOT EXISTS guardian_events (
    event_id TEXT PRIMARY KEY,
    at       TEXT NOT NULL,
    plan_id  TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_guardian_time ON guardian_events (at);

CREATE TABLE IF NOT EXISTS kpis (
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    payload      TEXT NOT NULL
);

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
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

_PRAGMAS_READ = (
    "PRAGMA query_only=ON",
    "PRAGMA busy_timeout=5000",
)


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the right pragmas. WAL is set once and persists in the file."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS_READ if read_only else _PRAGMAS_WRITE:
        conn.execute(pragma)
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the schema if it does not exist. Safe to call repeatedly."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
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
    return value.astimezone(UTC).isoformat()


class TelemetryStore:
    """Buffered writer. The only object in the process that holds a write connection.

    Usage::

        with TelemetryStore(cfg.db_path, batch_size=500) as store:
            store.record(sim_time, "zone_air_temp_c", 24.1, zone="Core_ZN")
            ...  # flushed automatically on threshold and on exit

    ``record`` is cheap enough to call from inside the EnergyPlus callback: it appends a tuple
    to a list and, at most once per ``batch_size`` rows, performs one transaction.
    """

    def __init__(self, db_path: Path | str, *, batch_size: int = 500) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.db_path = Path(db_path)
        self.batch_size = batch_size
        self._buffer: list[tuple[str, str | None, str, float]] = []
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

    # -- telemetry ---------------------------------------------------------------------

    def record(
        self,
        sim_time: datetime,
        channel: str,
        value: float,
        *,
        zone: str | None = None,
    ) -> None:
        """Buffer one sample. Flushes only when the batch threshold is reached (rule R3)."""
        self._buffer.append((_iso(sim_time), zone, channel, float(value)))
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def record_many(self, rows: Sequence[tuple[datetime, str, float, str | None]]) -> None:
        """Buffer several samples at once: ``(sim_time, channel, value, zone)``."""
        for sim_time, channel, value, zone in rows:
            self.record(sim_time, channel, value, zone=zone)

    def flush(self) -> int:
        """Write the buffer in a single transaction. Returns the number of rows written."""
        if not self._buffer:
            return 0

        pending, self._buffer = self._buffer, []
        with self._transaction():
            self._conn.executemany(
                "INSERT INTO telemetry (sim_time, zone, channel, value) VALUES (?, ?, ?, ?)",
                pending,
            )
        return len(pending)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
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
    # These are low-frequency (one row per planning cycle, not per timestep), so they are
    # written straight through rather than buffered.

    def write_plan(self, plan: Plan) -> None:
        """Journal the raw planner output, before the guardian sees it."""
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO plans (plan_id, created_at, model, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    plan.plan_id,
                    _iso(plan.created_at),
                    plan.planner_model,
                    plan.model_dump_json(),
                ),
            )

    def write_approved_plan(self, approved: ApprovedPlan) -> None:
        """Journal what was actually actuated."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO approved_plans (plan_id, approved_at, decision, fallback, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    approved.plan_id,
                    _iso(approved.approved_at),
                    str(approved.decision),
                    int(approved.fallback),
                    approved.model_dump_json(),
                ),
            )

    def write_guardian_event(self, event: GuardianEvent) -> None:
        """Journal a guardian decision. Written for every plan, including clean ones."""
        with self._transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO guardian_events "
                "(event_id, at, plan_id, decision, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    _iso(event.at),
                    event.plan_id,
                    str(event.decision),
                    event.model_dump_json(),
                ),
            )

    def write_kpis(self, kpis: KpiSnapshot) -> None:
        """Journal a KPI window."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO kpis (window_start, window_end, payload) VALUES (?, ?, ?)",
                (_iso(kpis.window_start), _iso(kpis.window_end), kpis.model_dump_json()),
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


def load_json_column(row: sqlite3.Row, column: str = "payload") -> dict:
    """Decode a journal ``payload`` column into a dict."""
    return json.loads(row[column])
