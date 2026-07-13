"""SQLite persistence layer.

The schedule is modeled as a single planning week (Mon..Sun, day indices 0..6).
Everything is a plain table so the .db file can be inspected or backed up easily.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    target_hours  REAL NOT NULL DEFAULT 32,
    max_hours     REAL NOT NULL DEFAULT 40,
    min_hours     REAL NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    open_hour   REAL NOT NULL DEFAULT 8,
    close_hour  REAL NOT NULL DEFAULT 20
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
    start_hour   REAL NOT NULL,
    end_hour     REAL NOT NULL,
    PRIMARY KEY (employee_id, day)
);

-- Per-store, per-weekday opening hours. NULL open/close = closed that day.
CREATE TABLE IF NOT EXISTS location_hours (
    location_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    open_hour    REAL,
    close_hour   REAL,
    PRIMARY KEY (location_id, day)
);

-- Required staff-hours per location per weekday (the coverage target the solver fills).
CREATE TABLE IF NOT EXISTS coverage (
    location_id     INTEGER NOT NULL,
    day             INTEGER NOT NULL,
    required_hours  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (location_id, day)
);

-- The request log: time-off / availability and shift preferences.
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    kind         TEXT NOT NULL,          -- 'Time off', 'Unavailable', 'Prefer off', 'Prefer on'
    location_id  INTEGER,                -- optional, for 'Prefer on' a specific spot
    status       TEXT NOT NULL DEFAULT 'Approved',   -- 'Approved', 'Pending', 'Denied'
    note         TEXT
);

-- The working schedule: one row per shift.
CREATE TABLE IF NOT EXISTS shifts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    location_id  INTEGER NOT NULL,
    hours        REAL NOT NULL,
    start_hour   REAL NOT NULL DEFAULT 9,
    locked       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Lightweight migrations for databases created before these columns existed.
        loc_cols = _column_names(conn, "locations")
        if "open_hour" not in loc_cols:
            conn.execute("ALTER TABLE locations ADD COLUMN open_hour REAL NOT NULL DEFAULT 8")
        if "close_hour" not in loc_cols:
            conn.execute("ALTER TABLE locations ADD COLUMN close_hour REAL NOT NULL DEFAULT 20")
        if "start_hour" not in _column_names(conn, "shifts"):
            conn.execute("ALTER TABLE shifts ADD COLUMN start_hour REAL NOT NULL DEFAULT 9")
        # Every location gets a store-hours row per weekday, seeded from its base
        # open/close. Idempotent: existing rows are never overwritten.
        for r in conn.execute("SELECT id, open_hour, close_hour FROM locations").fetchall():
            for d in range(7):
                conn.execute(
                    "INSERT OR IGNORE INTO location_hours(location_id,day,open_hour,close_hour) "
                    "VALUES(?,?,?,?)",
                    (r["id"], d, r["open_hour"], r["close_hour"]),
                )


def load_df(table: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(f"SELECT * FROM {table}", conn)


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
