"""
SQLite WAL intent bus for Memory Arbiter.
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

DB_PATH = Path("/mnt/nous-data/intent_bus.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'pending',
    wing        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    operation   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    source      TEXT NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON intents(status);
CREATE INDEX IF NOT EXISTS idx_wing   ON intents(wing);
"""


@asynccontextmanager
async def _conn():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        db.row_factory = aiosqlite.Row
        yield db


async def init_db() -> None:
    async with _conn() as db:
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await db.execute(stmt)
        # Reset stale "processing" intents from a prior crashed run
        await db.execute("UPDATE intents SET status='pending' WHERE status='processing'")
        await db.commit()


async def insert_intent(wing: str, scope: str, operation: str, payload: dict, source: str) -> int:
    async with _conn() as db:
        cur = await db.execute(
            "INSERT INTO intents (wing, scope, operation, payload, source) VALUES (?,?,?,?,?)",
            (wing, scope, operation, json.dumps(payload), source),
        )
        await db.commit()
        return cur.lastrowid


async def get_intent(intent_id: int) -> dict | None:
    async with _conn() as db:
        cur = await db.execute("SELECT * FROM intents WHERE id=?", (intent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_status(intent_id: int, status: str, error: str | None = None) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE intents SET status=?, error=? WHERE id=?",
            (status, error, intent_id),
        )
        await db.commit()


async def get_pending(limit: int = 10) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM intents WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_recent(limit: int = 100) -> list[dict]:
    async with _conn() as db:
        cur = await db.execute(
            "SELECT * FROM intents ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
