"""SQLite ledger: runs, batches, lines, cart_units and a state-history event log.

Every state change is written before the bot moves on, so a crash at any point
leaves enough information to resume safely.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import BATCH_TRANSITIONS, LINE_TRANSITIONS, PENDING, R_PLANNED, IllegalTransition

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, status TEXT,
    mode TEXT, live INTEGER, orders_file TEXT, batch_size INTEGER, options TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    run_id TEXT, batch_no INTEGER, state TEXT, purchase_attempts INTEGER DEFAULT 0,
    expected_total INTEGER, page_total INTEGER,
    order_id TEXT, amount INTEGER, card_last4 TEXT, name TEXT, order_date TEXT, order_date_raw TEXT,
    confirmation_text TEXT, screenshot TEXT, error TEXT, created_at TEXT, updated_at TEXT,
    PRIMARY KEY (run_id, batch_no)
);
CREATE TABLE IF NOT EXISTS lines (
    run_id TEXT, line_no INTEGER, product_input TEXT, qty_input TEXT, max_price_input TEXT,
    prod_id INTEGER, title TEXT, qty_requested INTEGER, qty_planned INTEGER, max_unit_price REAL,
    price_live REAL, price_seen REAL, decision TEXT, batch_no INTEGER, state TEXT, error TEXT,
    created_at TEXT, updated_at TEXT,
    PRIMARY KEY (run_id, line_no)
);
CREATE TABLE IF NOT EXISTS cart_units (
    unit_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, batch_no INTEGER, line_no INTEGER,
    prod_id INTEGER, uuid TEXT, state TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_units_batch ON cart_units (run_id, batch_no);
CREATE INDEX IF NOT EXISTS ix_units_uuid ON cart_units (uuid);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, ref INTEGER,
    from_state TEXT, to_state TEXT, note TEXT, ts TEXT
);
"""

LINE_FIELDS = {"product_input", "qty_input", "max_price_input", "prod_id", "title", "qty_requested",
               "qty_planned", "max_unit_price", "price_live", "price_seen", "decision", "batch_no", "error"}
BATCH_FIELDS = {"purchase_attempts", "expected_total", "page_total", "order_id", "amount", "card_last4",
                "name", "order_date", "order_date_raw", "confirmation_text", "screenshot", "error"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path = "ledger.db"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _event(self, run_id: str, kind: str, ref: int | None, frm: str | None, to: str, note: str | None) -> None:
        self.db.execute(
            "INSERT INTO events (run_id, kind, ref, from_state, to_state, note, ts) VALUES (?,?,?,?,?,?,?)",
            (run_id, kind, ref, frm, to, note, now()),
        )

    @staticmethod
    def _check(fields: dict, allowed: set[str]) -> None:
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown column(s): {bad}")

    # --- runs ------------------------------------------------------------------
    def create_run(self, run_id: str, mode: str, live: bool, orders_file: str, batch_size: int,
                   options: dict[str, Any]) -> None:
        ts = now()
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, ts, ts, R_PLANNED, mode, int(live), orders_file, batch_size, json.dumps(options)),
        )
        self._event(run_id, "run", None, None, R_PLANNED, f"mode={mode} live={live}")

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def set_run_status(self, run_id: str, status: str, note: str | None = None, live: bool | None = None) -> None:
        old = self.get_run(run_id)["status"]
        if live is not None:
            self.db.execute("UPDATE runs SET live=? WHERE run_id=?", (int(live), run_id))
        self.db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=?", (status, now(), run_id))
        if old != status:
            self._event(run_id, "run", None, old, status, note)

    # --- lines -----------------------------------------------------------------
    def add_line(self, run_id: str, line_no: int, **fields: Any) -> None:
        self._check(fields, LINE_FIELDS)
        ts = now()
        cols = ["run_id", "line_no", "state", "created_at", "updated_at", *fields]
        vals = [run_id, line_no, PENDING, ts, ts, *fields.values()]
        self.db.execute(f"INSERT INTO lines ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self._event(run_id, "line", line_no, None, PENDING, None)

    def lines(self, run_id: str, batch_no: int | None = None) -> list[sqlite3.Row]:
        if batch_no is None:
            return self.db.execute("SELECT * FROM lines WHERE run_id=? ORDER BY line_no", (run_id,)).fetchall()
        return self.db.execute("SELECT * FROM lines WHERE run_id=? AND batch_no=? ORDER BY line_no",
                               (run_id, batch_no)).fetchall()

    def line(self, run_id: str, line_no: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM lines WHERE run_id=? AND line_no=?", (run_id, line_no)).fetchone()

    def update_line(self, run_id: str, line_no: int, **fields: Any) -> None:
        self._check(fields, LINE_FIELDS)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE lines SET {sets}, updated_at=? WHERE run_id=? AND line_no=?",
                            (*fields.values(), now(), run_id, line_no))

    def set_line_state(self, run_id: str, line_no: int, state: str, note: str | None = None, **fields: Any) -> None:
        old = self.line(run_id, line_no)["state"]
        if old != state and state not in LINE_TRANSITIONS[old]:
            raise IllegalTransition(f"line {line_no}: {old} -> {state}")
        self.db.execute("BEGIN")
        try:
            self.update_line(run_id, line_no, **fields)
            self.db.execute("UPDATE lines SET state=?, updated_at=? WHERE run_id=? AND line_no=?",
                            (state, now(), run_id, line_no))
            if old != state:
                self._event(run_id, "line", line_no, old, state, note)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- batches ---------------------------------------------------------------
    def add_batch(self, run_id: str, batch_no: int) -> None:
        ts = now()
        self.db.execute("INSERT INTO batches (run_id, batch_no, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (run_id, batch_no, PENDING, ts, ts))
        self._event(run_id, "batch", batch_no, None, PENDING, None)

    def batches(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM batches WHERE run_id=? ORDER BY batch_no", (run_id,)).fetchall()

    def batch(self, run_id: str, batch_no: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM batches WHERE run_id=? AND batch_no=?", (run_id, batch_no)).fetchone()

    def update_batch(self, run_id: str, batch_no: int, **fields: Any) -> None:
        self._check(fields, BATCH_FIELDS)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE batches SET {sets}, updated_at=? WHERE run_id=? AND batch_no=?",
                            (*fields.values(), now(), run_id, batch_no))

    def set_batch_state(self, run_id: str, batch_no: int, state: str, note: str | None = None,
                        lines_too: bool = True, **fields: Any) -> None:
        """Move a batch (and, by default, all its lines) to `state` in one transaction."""
        old = self.batch(run_id, batch_no)["state"]
        if old != state and state not in BATCH_TRANSITIONS[old]:
            raise IllegalTransition(f"batch {batch_no}: {old} -> {state}")
        self.db.execute("BEGIN")
        try:
            self.update_batch(run_id, batch_no, **fields)
            self.db.execute("UPDATE batches SET state=?, updated_at=? WHERE run_id=? AND batch_no=?",
                            (state, now(), run_id, batch_no))
            if old != state:
                self._event(run_id, "batch", batch_no, old, state, note)
            if lines_too:
                for ln in self.lines(run_id, batch_no):
                    if ln["state"] == state:
                        continue
                    if state not in LINE_TRANSITIONS[ln["state"]]:
                        raise IllegalTransition(f"line {ln['line_no']}: {ln['state']} -> {state}")
                    self.db.execute("UPDATE lines SET state=?, updated_at=?, error=COALESCE(?, error) "
                                    "WHERE run_id=? AND line_no=?",
                                    (state, now(), fields.get("error"), run_id, ln["line_no"]))
                    self._event(run_id, "line", ln["line_no"], ln["state"], state, note)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- cart units --------------------------------------------------------------
    def add_unit(self, run_id: str, batch_no: int, line_no: int, prod_id: int, unit_uuid: str | None,
                 state: str) -> None:
        ts = now()
        self.db.execute(
            "INSERT INTO cart_units (run_id, batch_no, line_no, prod_id, uuid, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (run_id, batch_no, line_no, prod_id, unit_uuid, state, ts, ts))

    def units(self, run_id: str, batch_no: int | None = None) -> list[sqlite3.Row]:
        if batch_no is None:
            return self.db.execute("SELECT * FROM cart_units WHERE run_id=? ORDER BY unit_id", (run_id,)).fetchall()
        return self.db.execute("SELECT * FROM cart_units WHERE run_id=? AND batch_no=? ORDER BY unit_id",
                               (run_id, batch_no)).fetchall()

    def set_unit(self, unit_id: int, state: str, unit_uuid: str | None = None) -> None:
        if unit_uuid is None:
            self.db.execute("UPDATE cart_units SET state=?, updated_at=? WHERE unit_id=?", (state, now(), unit_id))
        else:
            self.db.execute("UPDATE cart_units SET state=?, uuid=?, updated_at=? WHERE unit_id=?",
                            (state, unit_uuid, now(), unit_id))

    def known_uuids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT uuid FROM cart_units WHERE uuid IS NOT NULL")}

    # --- history -----------------------------------------------------------------
    def events(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
