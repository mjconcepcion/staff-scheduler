"""Persistence layer with two interchangeable backends.

- **SQLite** (default): a local `scheduler.db` file next to the app. Zero setup.
- **Postgres**: used automatically when the `DATABASE_URL` environment variable is set
  (the deployed app sets it from Streamlit secrets). This is what makes the app
  machine-independent: state lives in a hosted database instead of a local file.

Everything above this module (app, seed, solver plumbing) is backend-agnostic: it calls
`get_conn()` and uses `?` placeholders + by-name row access, and this module adapts both
to whichever backend is active.

The schedule is modeled as a single planning week (Mon..Sun, day indices 0..6).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler.db")

TABLES = ("employees", "locations", "employee_locations", "employee_days",
          "employee_hours", "location_hours", "coverage", "requests", "shifts", "meta")

# {ID} and {FLOAT} are replaced per backend (SQLite vs Postgres types).
SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS employees (
    id            {ID},
    name          TEXT NOT NULL,
    target_hours  {FLOAT} NOT NULL DEFAULT 32,
    max_hours     {FLOAT} NOT NULL DEFAULT 40,
    min_hours     {FLOAT} NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS locations (
    id          {ID},
    name        TEXT NOT NULL,
    open_hour   {FLOAT} NOT NULL DEFAULT 8,
    close_hour  {FLOAT} NOT NULL DEFAULT 20
);

-- Which locations an employee is allowed to work. No rows for an employee = allowed anywhere.
CREATE TABLE IF NOT EXISTS employee_locations (
    employee_id  INTEGER NOT NULL,
    location_id  INTEGER NOT NULL,
    PRIMARY KEY (employee_id, location_id)
);

-- Which weekdays an employee can work. No rows for an employee = available any day.
-- A single row with day = -1 means "no days at all".
CREATE TABLE IF NOT EXISTS employee_days (
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    PRIMARY KEY (employee_id, day)
);

-- Optional hourly availability window per employee per weekday.
-- No row = any time that day (subject to employee_days and store hours).
CREATE TABLE IF NOT EXISTS employee_hours (
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    start_hour   {FLOAT} NOT NULL,
    end_hour     {FLOAT} NOT NULL,
    PRIMARY KEY (employee_id, day)
);

-- Per-store, per-weekday opening hours. NULL open/close = closed that day.
CREATE TABLE IF NOT EXISTS location_hours (
    location_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    open_hour    {FLOAT},
    close_hour   {FLOAT},
    PRIMARY KEY (location_id, day)
);

-- Required staff-hours per location per weekday (the coverage target the solver fills).
CREATE TABLE IF NOT EXISTS coverage (
    location_id     INTEGER NOT NULL,
    day             INTEGER NOT NULL,
    required_hours  {FLOAT} NOT NULL DEFAULT 0,
    PRIMARY KEY (location_id, day)
);

-- The request log: time-off / availability and shift preferences.
CREATE TABLE IF NOT EXISTS requests (
    id           {ID},
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    location_id  INTEGER,
    status       TEXT NOT NULL DEFAULT 'Approved',
    note         TEXT
);

-- The working schedule: one row per shift.
CREATE TABLE IF NOT EXISTS shifts (
    id           {ID},
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    location_id  INTEGER NOT NULL,
    hours        {FLOAT} NOT NULL,
    start_hour   {FLOAT} NOT NULL DEFAULT 9,
    locked       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def backend() -> str:
    return "postgres" if _database_url() else "sqlite"


# --------------------------------------------------------------------------------------
# Postgres support: one cached connection (reconnects if the server hangs up, which
# Neon does after idle periods) and a thin wrapper so `conn.execute("... ? ...")` and
# by-name row access work exactly like sqlite3.
# --------------------------------------------------------------------------------------
_PG_LOCK = threading.Lock()
_PG_RAW = None


def _pg_raw():
    global _PG_RAW
    import psycopg2
    if _PG_RAW is None or _PG_RAW.closed:
        _PG_RAW = psycopg2.connect(_database_url())
    return _PG_RAW


class _PgConn:
    """Adapts a psycopg2 connection to the sqlite3.Connection API surface we use."""

    def __init__(self, raw):
        self._raw = raw

    @staticmethod
    def _fix(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        from psycopg2.extras import RealDictCursor
        cur = self._raw.cursor(cursor_factory=RealDictCursor)
        cur.execute(self._fix(sql), tuple(params))
        return cur

    def executemany(self, sql: str, seq):
        cur = self._raw.cursor()
        cur.executemany(self._fix(sql), [tuple(p) for p in seq])
        return cur

    def executescript(self, sql: str):
        cur = self._raw.cursor()
        cur.execute(sql)  # psycopg2 runs multi-statement strings fine
        return cur


@contextmanager
def get_conn():
    if backend() == "postgres":
        import psycopg2
        with _PG_LOCK:
            try:
                raw = _pg_raw()
                raw.cursor().execute("SELECT 1")  # cheap liveness probe
            except psycopg2.Error:
                global _PG_RAW
                try:
                    if _PG_RAW is not None:
                        _PG_RAW.close()
                except Exception:
                    pass
                _PG_RAW = None
                raw = _pg_raw()
            try:
                yield _PgConn(raw)
                raw.commit()
            except Exception:
                raw.rollback()
                raise
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def insert_id(conn, sql: str, params=()) -> int:
    """INSERT and return the new row's id, on either backend."""
    if backend() == "postgres":
        cur = conn.execute(sql + " RETURNING id", params)
        return int(cur.fetchone()["id"])
    return int(conn.execute(sql, params).lastrowid)


def _column_names_sqlite(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    if backend() == "postgres":
        schema = SCHEMA_TEMPLATE.format(ID="SERIAL PRIMARY KEY", FLOAT="DOUBLE PRECISION")
        ensure_hours_sql = ("INSERT INTO location_hours(location_id,day,open_hour,close_hour) "
                            "VALUES(?,?,?,?) ON CONFLICT DO NOTHING")
    else:
        schema = SCHEMA_TEMPLATE.format(ID="INTEGER PRIMARY KEY AUTOINCREMENT", FLOAT="REAL")
        ensure_hours_sql = ("INSERT OR IGNORE INTO location_hours(location_id,day,open_hour,close_hour) "
                            "VALUES(?,?,?,?)")
    with get_conn() as conn:
        conn.executescript(schema)
        if backend() == "sqlite":
            # Lightweight migrations for SQLite files created before these columns existed.
            loc_cols = _column_names_sqlite(conn, "locations")
            if "open_hour" not in loc_cols:
                conn.execute("ALTER TABLE locations ADD COLUMN open_hour REAL NOT NULL DEFAULT 8")
            if "close_hour" not in loc_cols:
                conn.execute("ALTER TABLE locations ADD COLUMN close_hour REAL NOT NULL DEFAULT 20")
            if "start_hour" not in _column_names_sqlite(conn, "shifts"):
                conn.execute("ALTER TABLE shifts ADD COLUMN start_hour REAL NOT NULL DEFAULT 9")
        # Every location gets a store-hours row per weekday, seeded from its base
        # open/close. Idempotent: existing rows are never overwritten.
        for r in conn.execute("SELECT id, open_hour, close_hour FROM locations").fetchall():
            for d in range(7):
                conn.execute(ensure_hours_sql, (r["id"], d, r["open_hour"], r["close_hour"]))


def load_df(table: str) -> pd.DataFrame:
    with get_conn() as conn:
        cur = conn.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return pd.DataFrame([dict(r) for r in rows], columns=cols)


def replace_table(table: str, df: pd.DataFrame, columns: list[str]):
    """Replace an entire table's contents with `df` (single-user, whole-table saves)."""
    rows = [tuple(r) for r in df[columns].itertuples(index=False, name=None)]
    placeholders = ",".join("?" for _ in columns)
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {table}")
        if rows:
            conn.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", rows
            )


def get_meta(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def is_empty() -> bool:
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM employees").fetchone()["c"]
        return n == 0
