#!/usr/bin/env python3
"""
NOUS Legacy — interview state tracker.
SQLite DB der husker hvilke spørgsmål der er besvaret og hvornår.
"""
import random
import sqlite3
from datetime import date, datetime
from pathlib import Path

from questions import QUESTION_BANK, PRIORITY_CATEGORIES, by_id

DB_PATH = Path("/mnt/nous-data/interview_state.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interview_answers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id        TEXT    NOT NULL UNIQUE,
    category           TEXT    NOT NULL,
    question           TEXT    NOT NULL,
    answer             TEXT    NOT NULL,
    parent_question_id TEXT,
    created_at         TEXT    DEFAULT (datetime('now')),
    updated_at         TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interview_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migration: tilføj parent_question_id til eksisterende DB
    try:
        conn.execute("ALTER TABLE interview_answers ADD COLUMN parent_question_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # kolonne eksisterer allerede
    return conn


def get_answered_ids() -> set[str]:
    with _conn() as conn:
        rows = conn.execute("SELECT question_id FROM interview_answers").fetchall()
    return {r["question_id"] for r in rows}


def get_unanswered_questions(category: str | None = None) -> list[dict]:
    answered = get_answered_ids()
    qs = [q for q in QUESTION_BANK if q["id"] not in answered]
    if category:
        qs = [q for q in qs if q["category"] == category]
    return qs


def get_daily_question() -> dict | None:
    """
    Returnerer dagens spørgsmål.
    Samme spørgsmål hele dagen. Nyt spørgsmål dagen efter.
    Prioriterer til_boernene og vaerdier kategorier.
    Returnerer None hvis alle er besvaret.
    """
    today = date.today().isoformat()
    with _conn() as conn:
        meta_date = conn.execute(
            "SELECT value FROM interview_meta WHERE key='last_daily_question_date'"
        ).fetchone()
        meta_id = conn.execute(
            "SELECT value FROM interview_meta WHERE key='last_daily_question_id'"
        ).fetchone()

        if meta_date and meta_date["value"] == today and meta_id:
            q = by_id(meta_id["value"])
            if q:
                answered = get_answered_ids()
                if meta_id["value"] not in answered:
                    return q

        unanswered = get_unanswered_questions()
        if not unanswered:
            return None

        priority = [q for q in unanswered if q["category"] in PRIORITY_CATEGORIES]
        pool = priority if priority else unanswered
        chosen = random.choice(pool)

        conn.execute(
            "INSERT OR REPLACE INTO interview_meta(key, value) VALUES('last_daily_question_date', ?)",
            (today,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO interview_meta(key, value) VALUES('last_daily_question_id', ?)",
            (chosen["id"],),
        )
        conn.commit()
    return chosen


def save_answer(
    question_id: str,
    answer: str,
    parent_question_id: str | None = None,
    question_text: str | None = None,
) -> bool:
    """
    Gem svar. Returnerer True ved success.
    For opfølgningsspørgsmål (ikke i question bank): brug question_text + parent_question_id.
    """
    q = by_id(question_id)
    if q:
        cat      = q["category"]
        q_text   = q["question"]
    elif question_text:
        cat      = "followup"
        q_text   = question_text
    else:
        return False
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO interview_answers
                   (question_id, category, question, answer, parent_question_id, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(question_id) DO UPDATE SET
                   answer=excluded.answer,
                   updated_at=excluded.updated_at""",
            (question_id, cat, q_text, answer, parent_question_id, now, now),
        )
        conn.commit()
    return True


def get_answer(question_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM interview_answers WHERE question_id=?", (question_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_answers() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interview_answers ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_progress() -> dict:
    """Statistik per kategori og totalt."""
    answered = get_answered_ids()
    cats: dict[str, dict] = {}
    for q in QUESTION_BANK:
        cat = q["category"]
        if cat not in cats:
            cats[cat] = {"total": 0, "answered": 0}
        cats[cat]["total"] += 1
        if q["id"] in answered:
            cats[cat]["answered"] += 1
    return {
        "total":      len(QUESTION_BANK),
        "answered":   len(answered),
        "categories": cats,
    }


def get_all_questions_with_status() -> list[dict]:
    """Hent alle spørgsmål med besvaret-status og evt. svar."""
    answered_map: dict[str, dict] = {}
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM interview_answers").fetchall()
        for r in rows:
            answered_map[r["question_id"]] = dict(r)
    result = []
    for q in QUESTION_BANK:
        item = dict(q)
        ans = answered_map.get(q["id"])
        item["answered"] = ans is not None
        item["answer"] = ans["answer"] if ans else None
        item["answered_at"] = ans["updated_at"] if ans else None
        result.append(item)
    return result
