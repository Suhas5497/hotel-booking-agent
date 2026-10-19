"""SQLite persistence. SQLite is boring, battle-tested and zero-ops: the right call for a
prototype that must run anywhere. The repository boundary makes Postgres a drop-in later."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    concept TEXT NOT NULL,
    area TEXT NOT NULL,
    address TEXT NOT NULL,
    phone TEXT NOT NULL,
    cuisines TEXT NOT NULL,          -- JSON list
    price_per_person INTEGER NOT NULL,
    rating REAL NOT NULL,
    review_count INTEGER NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    tables TEXT NOT NULL,            -- JSON {"2": n, "4": n, "6": n, "8": n}
    total_seats INTEGER NOT NULL,
    private_dining_capacity INTEGER NOT NULL DEFAULT 0,
    features TEXT NOT NULL,          -- JSON list
    signature_dishes TEXT NOT NULL,  -- JSON list
    description TEXT NOT NULL,
    veg_only INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reservations (
    code TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    res_date TEXT NOT NULL,
    res_time TEXT NOT NULL,
    table_size INTEGER NOT NULL,     -- -1 = private dining room
    status TEXT NOT NULL,            -- confirmed | cancelled
    special_requests TEXT,
    source TEXT NOT NULL DEFAULT 'agent',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_slot ON reservations(restaurant_id, res_date, status);
CREATE INDEX IF NOT EXISTS idx_res_phone ON reservations(phone, status);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    code TEXT,
    detail TEXT
);
"""

JSON_COLS = ("cuisines", "tables", "features", "signature_dishes")


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        """Serialised write transaction: availability check + insert happen atomically,
        so two users can never be given the same last table."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._decode(dict(r)) for r in rows]

    @staticmethod
    def _decode(row: dict) -> dict:
        for col in JSON_COLS:
            if col in row and isinstance(row[col], str):
                row[col] = json.loads(row[col])
        if "veg_only" in row:
            row["veg_only"] = bool(row["veg_only"])
        return row

    def restaurant_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]

    def insert_restaurant(self, r: dict) -> None:
        row = dict(r)
        for col in JSON_COLS:
            row[col] = json.dumps(row[col])
        row["veg_only"] = int(bool(row.get("veg_only")))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.lock:
            self.conn.execute(f"INSERT INTO restaurants ({cols}) VALUES ({marks})", tuple(row.values()))
