"""
NOUS Fact Extractor v2 — universal, schema-enforcet fact extraction.
Bruger Ollama structured output (fuld JSON schema) + few-shot prompting.
Max 12 facts per dokument, max 120 tegn per fact, input trunceret til 8.000 tegn.
"""
import json
import logging
import os
import re
import sys

import httpx

log = logging.getLogger("fact_extractor")

OLLAMA_URL   = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
LLM_MODEL    = "qwen3:14b"
MAX_FACTS    = 12
MAX_CHARS    = 8_000
FACT_MAX_LEN = 120

VALID_FACT_TYPES = {"claim", "event", "observation", "decision", "communication"}

# Confidence-floor per fact_type (verificerbarhed)
CONFIDENCE_FLOOR = {
    "decision":      0.50,
    "event":         0.50,
    "observation":   0.40,
    "claim":         0.30,
    "communication": 0.30,
}

# Fuld JSON-schema sendt til Ollama — tvinger struktureret output på token-niveau
FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": MAX_FACTS,
            "items": {
                "type": "object",
                "properties": {
                    "fact_text":  {"type": "string", "maxLength": FACT_MAX_LEN},
                    "actor":      {"type": ["string", "null"]},
                    "date":       {"type": ["string", "null"]},
                    "fact_type":  {
                        "type": "string",
                        "enum": list(VALID_FACT_TYPES),
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "source_ref": {"type": "string"},
                },
                "required": ["fact_text", "actor", "date", "fact_type", "confidence", "source_ref"],
            },
        }
    },
    "required": ["facts"],
}

FEW_SHOT = (
    'Eksempel INPUT: "Den 12. april 2024 traf Ankestyrelsen afgørelse om at samvær '
    "skulle genetableres inden 30 dage. Socialrådgiveren anbefalede forældreguide-forløb. "
    'Faderen havde konsekvent overholdt alle aftaler."\n\n'
    'Eksempel OUTPUT:\n{"facts": ['
    '{"fact_text": "Ankestyrelsen traf afgørelse om genetablering af samvær inden 30 dage", '
    '"actor": "Ankestyrelsen", "date": "2024-04-12", "fact_type": "decision", "confidence": 0.95, "source_ref": "eksempel.pdf"}, '
    '{"fact_text": "Socialrådgiver anbefalede forældreguide-forløb", '
    '"actor": "Socialrådgiver", "date": "2024-04-12", "fact_type": "observation", "confidence": 0.80, "source_ref": "eksempel.pdf"}, '
    '{"fact_text": "Faderen har konsekvent overholdt alle aftaler", '
    '"actor": "Faderen", "date": null, "fact_type": "claim", "confidence": 0.70, "source_ref": "eksempel.pdf"}'
    "]}"
)

SYSTEM_PROMPT = (
    "Du er en præcis fact-extraktor. Ekstraher op til {max_facts} konkrete, atomare facts fra teksten.\n"
    "REGLER:\n"
    "- fact_text: MAX 120 tegn. Ét verificerbart udsagn. Ingen gentagelser.\n"
    "- actor: Den primære aktør (person, myndighed, institution) — eller null.\n"
    "- date: ISO-format YYYY-MM-DD — eller null.\n"
    "- fact_type: claim | event | observation | decision | communication\n"
    "- confidence: 0.0–1.0 (din vurdering af verificerbarhed)\n"
    "- source_ref: \"{source_ref}\"\n\n"
    "{few_shot}\n\n"
    "Dokument ({source_ref}):\n{text}"
)


def _build_prompt(text: str, source_ref: str) -> str:
    return SYSTEM_PROMPT.format(
        max_facts=MAX_FACTS,
        source_ref=source_ref,
        few_shot=FEW_SHOT,
        text=text[:MAX_CHARS],
    )


def _call_ollama(prompt: str, timeout: float) -> str | None:
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": FACT_SCHEMA,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 1024,  # 256 er for lavt til 12 facts á 120 tegn
                },
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception as e:
        log.warning(f"  Ollama fejl: {e}")
        return None


def _parse_and_validate(raw: str, source_ref: str) -> list[dict]:
    # Structured output returner objekt med "facts" array
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: prøv at udtrække array direkte
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return []
        try:
            items = json.loads(m.group())
            data = {"facts": items}
        except Exception:
            return []

    items = data.get("facts", [])
    if not isinstance(items, list):
        return []

    seen: set[str] = set()
    facts: list[dict] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        fact_text = str(item.get("fact_text", "")).strip()[:FACT_MAX_LEN]
        if not fact_text or len(fact_text) < 5:
            continue

        key = fact_text.lower()
        if key in seen:
            continue
        seen.add(key)

        ft = str(item.get("fact_type", "observation")).lower().strip()
        if ft not in VALID_FACT_TYPES:
            ft = "observation"

        try:
            conf = float(item.get("confidence", 0.5))
            conf = max(CONFIDENCE_FLOOR.get(ft, 0.3), min(1.0, conf))
        except (TypeError, ValueError):
            conf = CONFIDENCE_FLOOR.get(ft, 0.5)

        actor = str(item.get("actor") or "").strip() or None
        date  = str(item.get("date")  or "").strip() or None

        # Lille boost: alle tre centrale felter sat + høj-tillids type
        if actor and date and ft in ("decision", "event"):
            conf = min(1.0, round(conf + 0.05, 3))

        facts.append({
            "fact_text":  fact_text,
            "actor":      actor,
            "date":       date,
            "fact_type":  ft,
            "confidence": round(conf, 3),
            "source_ref": source_ref,
        })

        if len(facts) >= MAX_FACTS:
            break

    return facts


def extract_facts_from_document(text: str, source_ref: str) -> list[dict]:
    """
    Ekstraher op til 12 universelle facts fra et dokument.
    Returnerer liste af dicts: {fact_text, actor, date, fact_type, confidence, source_ref}.
    Forsøger med 8.000 tegn; retry med 4.000 tegn ved timeout.
    """
    prompt = _build_prompt(text, source_ref)
    raw = _call_ollama(prompt, timeout=120.0)

    if raw is None:
        log.info(f"  Retry med 4.000 tegn for {source_ref!r}")
        raw = _call_ollama(_build_prompt(text[:4_000], source_ref), timeout=90.0)

    if raw is None:
        log.warning(f"  Fact extraction fejlede for {source_ref!r}")
        return []

    facts = _parse_and_validate(raw, source_ref)
    log.info(f"  {len(facts)} v2-facts ekstraheret fra {source_ref!r}")
    return facts


if __name__ == "__main__":
    # Smoke-test: læs tekst fra stdin eller argument
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    result = extract_facts_from_document(text, source_ref="test")
    print(json.dumps(result, ensure_ascii=False, indent=2))
