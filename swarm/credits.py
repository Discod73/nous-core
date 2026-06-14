"""NOUS Swarm — Credit system: fairness-lag for bidrag/forbrug per peer."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

CREDITS_DB = Path("/mnt/nous-data/swarm_credits.db")
_lock = threading.Lock()

POINT_RULES: dict[str, int] = {
    "contribute_fact":    +2,
    "consume_fact":       -1,
    "contribute_compute": +3,
    "consume_compute":    -2,
}


def _db() -> sqlite3.Connection:
    CREDITS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CREDITS_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credits (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    DEFAULT (datetime('now')),
            action    TEXT    NOT NULL,
            node_id   TEXT,
            points    INTEGER NOT NULL,
            details   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credit_summary (
            node_id         TEXT PRIMARY KEY,
            points_given    INTEGER DEFAULT 0,
            points_received INTEGER DEFAULT 0,
            last_updated    TEXT
        )
    """)
    conn.commit()
    return conn


def add_credit(
    action: str,
    node_id: str | None = None,
    multiplier: int = 1,
    details: str | None = None,
) -> int:
    """Registrér credit-transaktion. Returner ny total-balance."""
    points = POINT_RULES.get(action, 0) * multiplier
    if points == 0:
        return get_balance()
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _db()
        conn.execute(
            "INSERT INTO credits (action, node_id, points, details) VALUES (?, ?, ?, ?)",
            (action, node_id, points, details),
        )
        if node_id:
            # points_given  = hvad DE har givet OS (consume-events = vi modtager)
            # points_received = hvad VI har givet DEM (contribute-events = vi giver)
            given    = abs(points) if points < 0 else 0
            received = points      if points > 0 else 0
            conn.execute(
                """INSERT INTO credit_summary (node_id, points_given, points_received, last_updated)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     points_given    = points_given    + excluded.points_given,
                     points_received = points_received + excluded.points_received,
                     last_updated    = excluded.last_updated""",
                (node_id, given, received, now),
            )
        conn.commit()
        row = conn.execute("SELECT COALESCE(SUM(points), 0) FROM credits").fetchone()
        balance = row[0]
        conn.close()
    return balance


def get_balance() -> int:
    conn = _db()
    row = conn.execute("SELECT COALESCE(SUM(points), 0) FROM credits").fetchone()
    conn.close()
    return row[0]


def get_peer_balance(node_id: str) -> dict:
    conn = _db()
    row = conn.execute(
        "SELECT points_given, points_received FROM credit_summary WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    conn.close()
    if row:
        return {"points_given": row["points_given"], "points_received": row["points_received"]}
    return {"points_given": 0, "points_received": 0}


def get_credit_summary() -> dict:
    conn = _db()
    balance = conn.execute("SELECT COALESCE(SUM(points), 0) FROM credits").fetchone()[0]
    rows = conn.execute(
        """SELECT node_id, points_given, points_received, last_updated
           FROM credit_summary ORDER BY points_given DESC"""
    ).fetchall()
    conn.close()
    return {
        "balance": balance,
        "peers":   [dict(r) for r in rows],
    }


def get_priority_for_peer(node_id: str) -> int:
    """Prioritet 1–10: peers der har givet os mest relativ til hvad vi har givet dem."""
    b = get_peer_balance(node_id)
    given    = b["points_given"]     # de gav os
    received = b["points_received"]  # vi gav dem
    if given == 0:
        return 1
    ratio = given / max(received, 1)
    return max(1, min(int(ratio * 5), 10))
