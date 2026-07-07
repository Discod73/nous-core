"""
NOUS Fact Extractor v2 — universal, schema-enforcet fact extraction.
Bruger Ollama structured output (fuld JSON schema) + few-shot prompting.
Max 12 facts per dokument, max 120 tegn per fact, input trunceret til 8.000 tegn.
"""
import json
import logging
import re
import sys

import httpx

log = logging.getLogger("fact_extractor")

import os as _os
OLLAMA_URL   = _os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
LLM_MODEL    = "qwen3:14b"
MAX_FACTS    = 12
MAX_CHARS    = 8_000   # Absolut øvre grænse (bruges af _build_prompt som clip)
FIRST_CHARS  = 4_000   # Bruges ved første forsøg — kortere = hurtigere, færre timeouts på 14B
FACT_MAX_LEN = 120
FACT_RAW_WARN = 200   # Advarsel hvis LLM genererer fact_text > dette (før truncation)
MAX_TRUNCATED_RATIO = 0.5  # Retry hvis mere end halvdelen trunceres

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
    "Du er en præcis fact-extraktor. Ekstraher op til {max_facts} kompakte, atomare facts.\n"
    "OBLIGATORISKE REGLER:\n"
    "- fact_text: STRIKT MAX 120 tegn. Subjekt-prædikat-objekt-stil. Én afsluttet kendsgerning.\n"
    "  KORREKT: 'Ankestyrelsen traf afgørelse om genetablering af samvær'\n"
    "  FORKERT: 'Den 12. april 2024 traf Ankestyrelsen afgørelse om at samvær skulle genetableres inden 30 dage, idet faderen...'\n"
    "- ALDRIG: lange sætninger, rå tekstuddrag, baggrundsbeskrivelser, eller tekst der fortsætter.\n"
    "- ALDRIG newlines eller linjeskift i fact_text.\n"
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
    is_qwen3 = "qwen3" in LLM_MODEL.lower()
    # /no_think i message + think=false API-param (dobbelt sikring mod CoT på qwen3)
    content = "/no_think\n" + prompt if is_qwen3 else prompt
    body: dict = {
        "model":    LLM_MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream":   True,
        # Ingen "format": FACT_SCHEMA — JSON-grammar-constraint er 3-5× langsommere
        # end fri generation. _parse_and_validate() håndterer al output-validering.
        "options": {
            "temperature": 0.0,
            "num_predict": 4096,  # Plads til tænkning (qwen3) + JSON-output
            "num_ctx":     8192,  # 4000 chars input ≈ 1500 tokens + 3000 tænkning + 500 JSON
        },
    }
    if is_qwen3:
        body["think"] = False  # Ollama native think-disable for qwen3

    try:
        chunks: list[str] = []
        with httpx.stream(
            "POST",
            f"{OLLAMA_URL}/api/chat",
            json=body,
            timeout=timeout,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = obj.get("message", {}).get("content", "")
                if token:
                    chunks.append(token)
                if obj.get("done"):
                    break
        return "".join(chunks).strip()
    except Exception as e:
        log.warning(f"  Ollama fejl: {e}")
        return None


def _strip_think(raw: str) -> str:
    """Fjern qwen3 <think>...</think> blokke — inkl. ufuldstændige blokke uden afsluttende tag."""
    # Fulde blokke: <think>...</think>
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE)
    # Ufuldstændig blok (ramt num_predict-grænse mid-tænkning): alt fra <think> til slut
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_json(raw: str) -> dict | None:
    """
    Forsøg at udtrække {"facts": [...]} fra rå LLM-output.
    Prøver i rækkefølge: direkte parse → søg {"facts": → søg JSON-array direkte.
    """
    # 1. Direkte parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Find {"facts": blokken — robust overfor tekst før/efter JSON
    m = re.search(r'\{[^{}]*"facts"\s*:\s*\[[\s\S]*\]\s*\}', raw)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # 3. Fallback: find første komplet JSON-objekt
    m = re.search(r'\{[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def _parse_and_validate(raw: str, source_ref: str) -> tuple[list[dict], int]:
    """
    Returnerer (facts, truncated_count).
    truncated_count = antal facts hvor LLM genererede mere end FACT_MAX_LEN tegn.
    """
    cleaned = _strip_think(raw)
    data = _extract_json(cleaned)
    if data is None:
        log.warning(f"  JSON-parsing fejlede for {source_ref!r}. Rå output (500 tegn): {cleaned[:500]!r}")
        return [], 0

    items = data.get("facts", [])
    if not isinstance(items, list):
        return [], 0

    seen: set[str] = set()
    facts: list[dict] = []
    truncated_count = 0

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_text = str(item.get("fact_text", "")).strip()

        # Afvis facts med newlines — tegn på rå tekstuddrag
        if "\n" in raw_text or "\r" in raw_text:
            log.debug(f"  Afvist (newline i fact_text): {raw_text[:60]!r}")
            continue

        # Log hvis LLM ignorerede længde-begrænsningen
        if len(raw_text) > FACT_MAX_LEN:
            truncated_count += 1
            if len(raw_text) > FACT_RAW_WARN:
                log.debug(f"  LLM genererede {len(raw_text)} tegn (trunceres til {FACT_MAX_LEN}): {raw_text[:60]!r}")

        fact_text = raw_text[:FACT_MAX_LEN]
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

    return facts, truncated_count


_STRICT_SUFFIX = (
    "\n\nHUSK: fact_text MÅ ABSOLUT IKKE overstige 120 tegn. Skriv KORTERE end du tror er nødvendigt."
)


def extract_facts_from_document(text: str, source_ref: str) -> list[dict]:
    """
    Ekstraher op til 12 universelle facts fra et dokument.
    Returnerer liste af dicts: {fact_text, actor, date, fact_type, confidence, source_ref}.
    Første forsøg: FIRST_CHARS (4.000) tegn, timeout 360s — fri generation (ingen format-constraint).
    Retry ved timeout: samme 4.000 tegn, 240s (model er varm, ingen prefill-overhead).
    Ekstra retry med strengere prompt hvis LLM ignorerer længde-begrænsningen.
    """
    prompt = _build_prompt(text[:FIRST_CHARS], source_ref)
    raw = _call_ollama(prompt, timeout=360.0)

    if raw is None:
        log.info(f"  Retry med {FIRST_CHARS} tegn for {source_ref!r}")
        raw = _call_ollama(_build_prompt(text[:FIRST_CHARS], source_ref), timeout=240.0)

    if raw is None:
        log.warning(f"  Fact extraction fejlede for {source_ref!r}")
        return []

    facts, truncated = _parse_and_validate(raw, source_ref)

    if not facts:
        log.warning(f"  0 facts efter parsing for {source_ref!r}. Rå output (800 tegn): {raw[:800]!r}")

    # Retry med strengere prompt hvis LLM ignorerede længde-begrænsningen
    if facts and truncated / len(facts) > MAX_TRUNCATED_RATIO:
        log.warning(
            f"  {truncated}/{len(facts)} facts truncated — LLM ignorerer MAX 120 tegn. "
            f"Retry med strengere prompt for {source_ref!r}"
        )
        strict_prompt = _build_prompt(text[:FIRST_CHARS], source_ref) + _STRICT_SUFFIX
        raw2 = _call_ollama(strict_prompt, timeout=180.0)
        if raw2:
            facts2, truncated2 = _parse_and_validate(raw2, source_ref)
            if facts2:
                log.info(f"  Strengere retry: {len(facts2)} facts, {truncated2} truncated")
                facts = facts2

    log.info(f"  {len(facts)} v2-facts ekstraheret fra {source_ref!r}")
    return facts


if __name__ == "__main__":
    # Smoke-test: læs tekst fra stdin eller argument
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    result = extract_facts_from_document(text, source_ref="test")
    print(json.dumps(result, ensure_ascii=False, indent=2))
