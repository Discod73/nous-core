"""
SQLite WAL intent bus for Memory Arbiter.
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS heat_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    collection  TEXT NOT NULL,
    point_id    TEXT NOT NULL,
    accessed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_heat_coll_point ON heat_records(collection, point_id);
CREATE INDEX IF NOT EXISTS idx_heat_accessed   ON heat_records(accessed_at);

CREATE TABLE IF NOT EXISTS curator_shadow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    point_id        TEXT NOT NULL,
    actual_wing     TEXT NOT NULL,
    actual_scope    TEXT NOT NULL,
    predicted_wing  TEXT NOT NULL,
    predicted_scope TEXT NOT NULL,
    confidence      REAL NOT NULL,
    error_direction TEXT NOT NULL,
    is_test         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts    ON curator_shadow_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_shadow_scope ON curator_shadow_log(actual_scope, error_direction);
"""

HEAT_WINDOW_DAYS = 180


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
        # Migration: is_test-kolonne tilføjet efter baseline-start
        try:
            await db.execute(
                "ALTER TABLE curator_shadow_log ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # kolonnen eksisterer allerede
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


# ── Heat tracking ─────────────────────────────────────────────────────────────

async def record_heat(collection: str, point_ids: list[str]) -> None:
    """Registrér at disse punkt-ID'er blev tilgået (søgeresultat, RAG-kald osv.)."""
    if not point_ids:
        return
    async with _conn() as db:
        await db.executemany(
            "INSERT INTO heat_records (collection, point_id) VALUES (?, ?)",
            [(collection, pid) for pid in point_ids],
        )
        await db.commit()


async def get_heat_score(collection: str, point_id: str, days: int = HEAT_WINDOW_DAYS) -> float:
    """Heat-score = antal accesses i vinduet / window_days.

    Score < 0.02 med 180-dages vindue svarer til under ~3.6 tilgange på 180 dage.
    """
    async with _conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM heat_records "
            "WHERE collection = ? AND point_id = ? "
            "AND accessed_at >= datetime('now', ? || ' days')",
            (collection, point_id, f"-{days}"),
        )
        row = await cur.fetchone()
        count = row[0] if row else 0
    return count / days


async def get_cold_points(collection: str, threshold: float = 0.02,
                          days: int = HEAT_WINDOW_DAYS) -> list[str]:
    """Returnér point_ids der har heat < threshold OG ALDRIG er set i vinduet."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT DISTINCT point_id FROM heat_records WHERE collection = ? "
            "AND accessed_at >= datetime('now', ? || ' days')",
            (collection, f"-{days}"),
        )
        warm = {row[0] for row in await cur.fetchall()}
    return warm  # returnerer de VARME; kaldere skal sortere resten fra ekstern scroll


async def purge_old_heat(days: int = HEAT_WINDOW_DAYS) -> int:
    """Slet heat-records ældre end window. Returnerer antal slettede rækker."""
    async with _conn() as db:
        cur = await db.execute(
            "DELETE FROM heat_records WHERE accessed_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        await db.commit()
        return cur.rowcount


# ── Curator v1 shadow logging ─────────────────────────────────────────────────

async def log_shadow_prediction(
    point_id: str,
    actual_wing: str,
    actual_scope: str,
    predicted_wing: str,
    predicted_scope: str,
    confidence: float,
    direction: str,
    is_test: bool = False,
) -> None:
    """Log én Curator v1-forudsigelse ved siden af den faktiske klassifikation.

    is_test=True markerer testdata — audit-log bevares,
    men disse poster filtreres fra i metrics og shadow-rapport.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with _conn() as db:
        await db.execute(
            "INSERT INTO curator_shadow_log "
            "(timestamp, point_id, actual_wing, actual_scope, "
            " predicted_wing, predicted_scope, confidence, error_direction, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, point_id, actual_wing, actual_scope,
             predicted_wing, predicted_scope, confidence, direction, int(is_test)),
        )
        await db.commit()


async def get_shadow_stats(days: int | None = None, exclude_test: bool = True) -> dict:
    """Hent aggregerede shadow-statistikker for seneste N dage (None = al tid).

    exclude_test=True (default): testdata filtreres fra — påvirker aldrig metrics.
    exclude_test=False: medtager alle poster incl. testdata (til audit/debug).
    """
    async with _conn() as db:
        clauses = ["1=1"]
        if days is not None:
            clauses.append(f"timestamp >= datetime('now', '-{days} days')")
        if exclude_test:
            clauses.append("is_test = 0")
        where = "WHERE " + " AND ".join(clauses)

        cur = await db.execute(f"SELECT COUNT(*) FROM curator_shadow_log {where}")
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            f"""
            SELECT actual_scope, error_direction, COUNT(*) as cnt
            FROM curator_shadow_log
            {where}
            GROUP BY actual_scope, error_direction
            """
        )
        rows = await cur.fetchall()

    by_scope: dict[str, dict] = {}
    for scope, direction, cnt in rows:
        if scope not in by_scope:
            by_scope[scope] = {"total": 0, "correct": 0, "dangerous": 0,
                               "safe": 0, "scope_unknown": 0}
        by_scope[scope]["total"] += cnt
        by_scope[scope][direction] = cnt

    for scope, d in by_scope.items():
        t = d["total"]
        d["dangerous_rate"] = round(d["dangerous"] / t, 4) if t else 0.0
        d["safe_rate"]      = round(d["safe"]      / t, 4) if t else 0.0
        d["correct_rate"]   = round(d["correct"]   / t, 4) if t else 0.0

    return {"total": total, "by_scope": by_scope}


async def get_shadow_baseline_date() -> str | None:
    """Returner ISO-timestamp for den første rigtige (ikke-test) shadow-log-post."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT MIN(timestamp) FROM curator_shadow_log WHERE is_test = 0"
        )
        row = await cur.fetchone()
        return row[0] if row else None
