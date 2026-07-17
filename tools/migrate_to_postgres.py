"""One-shot migration: copy the local SQLite scheduler.db into a Postgres database.

Usage (PowerShell, from the project folder):
    $env:DATABASE_URL = "<your Neon connection string>"
    .venv\\Scripts\\python.exe tools\\migrate_to_postgres.py

Idempotent: wipes the target tables first, so re-running is safe. Copies everything,
including the password hash in `meta`, so your existing password keeps working online.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import db  # noqa: E402

# Tables with a SERIAL id whose sequence must be bumped after explicit-id inserts.
ID_TABLES = ("employees", "locations", "requests", "shifts")


def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set the DATABASE_URL environment variable to your Postgres connection "
                 "string first (see DEPLOY.md).")
    if not os.path.exists(db.DB_PATH):
        sys.exit(f"No local database found at {db.DB_PATH} — nothing to migrate.")

    src = sqlite3.connect(db.DB_PATH)
    src.row_factory = sqlite3.Row

    print(f"Source:  {db.DB_PATH}")
    print(f"Target:  {url.split('@')[-1].split('?')[0]}  (postgres)")

    db.init_db()  # creates the schema on the Postgres side

    with db.get_conn() as conn:
        for table in reversed(db.TABLES):
            conn.execute(f"DELETE FROM {table}")
        total = 0
        for table in db.TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"  {table:20s} 0 rows")
                continue
            cols = rows[0].keys()
            placeholders = ",".join("?" for _ in cols)
            conn.executemany(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
            print(f"  {table:20s} {len(rows)} rows")
            total += len(rows)
        # Explicit-id inserts don't advance SERIAL sequences — bump them so future
        # inserts don't collide with migrated ids.
        for table in ID_TABLES:
            conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 0), 1))"
            )

    # Verify counts match.
    mismatches = []
    with db.get_conn() as conn:
        for table in db.TABLES:
            n_src = src.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            n_dst = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            if n_src != n_dst:
                mismatches.append(f"{table}: sqlite={n_src} postgres={n_dst}")
    if mismatches:
        sys.exit("MISMATCH after migration: " + "; ".join(mismatches))
    print(f"Done — {total} rows migrated and verified. Your password carried over too.")


if __name__ == "__main__":
    main()
