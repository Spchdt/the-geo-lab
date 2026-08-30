from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("INTENTTWIN_DB", ROOT / "intenttwin.db"))


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def migrate() -> None:
    with connect() as db:
        db.executescript((ROOT / "migrations" / "001_initial.sql").read_text())


def one(sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(sql, args).fetchone()
        return dict(row) if row else None


def all(sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(sql, args).fetchall()]


def decode(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    for field in fields:
        if row.get(field):
            row[field] = json.loads(row[field])
    return row
