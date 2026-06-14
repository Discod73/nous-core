"""
Kør anonymisering igen på pending rækker med NULL anonymized_text.
Bruges til at reparere items der timede ud under første kørsel.
"""
import os
import sqlite3
import sys
import re
import httpx
from pathlib import Path

DB_PATH    = Path("/mnt/nous-data/swarm_queue.db")
OLLAMA_URL = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
LLM_MODEL  = "qwen3:14b"

ANON_PROMPT = """Du er en anonymiserings-agent. Omskriv følgende fact så den bliver generelt \
anvendelig uden at afsløre hvem den handler om.

Regler:
- Fjern ALLE egennavne (personer, steder, organisationer, institutioner)
- Fjern specifikke datoer — behold kun år eller årstid hvis relevant
- Fjern relationer ("min datter", "min bror") — erstat med generiske termer
- Bevar den juridiske, faktuelle eller praktiske kerne af informationen
- Hvis informationen er for personspecifik til at anonymisere sikkert → returner kun teksten "IKKE_ANONYMISERBAR"
- Svar KUN med den anonymiserede tekst eller "IKKE_ANONYMISERBAR" — intet andet

Original fact:
{fact_text}"""

_LEGAL_TERMS    = re.compile(r"\b(lov|paragraf|bekendtgørelse|§|serviceloven|retsplejeloven)\b", re.I)
_SENSITIVE_NUMS = re.compile(r"\b\d{6}[-\s]?\d{4}\b|\b\d{8}\b|\b\d{10}\b|\b\+?45\s*\d{8}\b")


def _anonymize(fact_text: str) -> str | None:
    prompt = ANON_PROMPT.format(fact_text=fact_text[:3000])
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=240.0,  # Øget fra 180 → 240s
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception as e:
        print(f"  FEJL: {e}")
        return None


def _score_confidence(original: str, anonymized: str) -> float:
    score = 0.5
    orig_len = len(original)
    anon_len = len(anonymized)
    if orig_len > 0 and (orig_len - anon_len) / orig_len > 0.5:
        score += 0.2
    if _SENSITIVE_NUMS.search(anonymized):
        score -= 0.3
    if _LEGAL_TERMS.search(anonymized):
        score += 0.1
    return round(max(0.0, min(1.0, score)), 3)


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, original_point_id, original_text FROM promotion_queue "
        "WHERE anonymized_text IS NULL AND confidence = 0.0 AND status = 'pending'"
    ).fetchall()

    if not rows:
        print("Ingen rækker at behandle.")
        conn.close()
        return

    print(f"{len(rows)} rækker at anonymisere...\n")
    ok = failed = not_anon = 0

    for row in rows:
        row_id = row["id"]
        point_id = row["original_point_id"]
        original_text = row["original_text"]
        print(f"[{row_id}] {point_id[:8]}... ", end="", flush=True)

        anon_text = _anonymize(original_text)

        if anon_text is None:
            print("TIMEOUT/FEJL — springer over, forsøges igen næste gang")
            failed += 1
            continue

        if anon_text == "IKKE_ANONYMISERBAR":
            conn.execute(
                "UPDATE promotion_queue SET status='not_anonymizable' WHERE id=?",
                (row_id,),
            )
            conn.commit()
            print("IKKE_ANONYMISERBAR → status opdateret")
            not_anon += 1
            continue

        confidence = _score_confidence(original_text, anon_text)
        conn.execute(
            "UPDATE promotion_queue SET anonymized_text=?, confidence=? WHERE id=?",
            (anon_text, confidence, row_id),
        )
        conn.commit()
        print(f"OK (conf={confidence})")
        ok += 1

    conn.close()
    print(f"\nFærdig — {ok} anonymiseret, {not_anon} ikke-anonymiserbare, {failed} fejlede")


if __name__ == "__main__":
    main()
