"""
Longevity/audit closure D3 (2026-09-06/07, Dan-ruled). The ingest retry +
quarantine HOW:

  TRANSIENT failure (Qdrant/Ollama down, timeout) →
    exponential backoff (30 s → … → 1 h cap), the file STAYS in the scan
    forever, retried the moment the service recovers. NEVER dropped, NEVER moved.

  DETERMINISTIC failure (no extractable text — scanned/image PDF) →
    QUARANTINE (incoming/_rejected/) — "quarantine, ikke gravplads". NOT
    constantly rescanned, BUT auto-re-evaluated when a RELEVANT capability
    CHANGES (extractor version / new parser / OCR available / the failure
    condition disappears). Manual operator requeue still works as a fallback.
    Manifest per file carries: reason / rejected_at / capability_fingerprint /
    provenance / attempt_state.

Run:  cd /srv/nous/pipeline && .venv/bin/python -m pytest test_ingest_retry.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ingest


@pytest.fixture
def incoming(tmp_path, monkeypatch):
    d = tmp_path / "incoming"
    (d / "jura").mkdir(parents=True)
    monkeypatch.setattr(ingest, "WATCH_DIR", d)
    monkeypatch.setattr(ingest, "_tel", lambda *a, **k: True)
    return d


def _mk(incoming, name="doc.pdf"):
    p = incoming / "jura" / name
    p.write_bytes(b"%PDF-1.4 fake body")
    return p


def _manifest(incoming):
    mp = incoming / "_rejected" / "MANIFEST.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines() if l.strip()] if mp.exists() else []


# ── process_file classification ────────────────────────────────────────────

def test_no_text_is_deterministic_reject(incoming, monkeypatch):
    _mk(incoming)
    monkeypatch.setattr(ingest, "extract_text", lambda p: "")
    assert ingest.process_file(incoming / "jura" / "doc.pdf") == "reject"


def test_parse_failure_is_deterministic_reject_with_distinct_evidence(incoming, monkeypatch):
    _mk(incoming)
    monkeypatch.setattr(ingest, "extract_text", lambda p: None)   # parser could not open it
    assert ingest.process_file(incoming / "jura" / "doc.pdf") == "reject"
    assert "None" in ingest._LAST_REJECT["evidence"]


def test_qdrant_down_is_transient_retry(incoming, monkeypatch):
    _mk(incoming)
    monkeypatch.setattr(ingest, "extract_text", lambda p: "real text " * 50)
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    monkeypatch.setattr(ingest, "upsert_to_qdrant", lambda *a, **k: False)
    assert ingest.process_file(incoming / "jura" / "doc.pdf") == "retry"


def test_embedding_down_is_transient_retry(incoming, monkeypatch):
    _mk(incoming)
    monkeypatch.setattr(ingest, "extract_text", lambda p: "real text " * 50)
    monkeypatch.setattr(ingest, "embed_text", lambda c: None)
    assert ingest.process_file(incoming / "jura" / "doc.pdf") == "retry"


def test_full_success_is_ok(incoming, monkeypatch):
    _mk(incoming)
    monkeypatch.setattr(ingest, "extract_text", lambda p: "real text " * 50)
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    monkeypatch.setattr(ingest, "upsert_to_qdrant", lambda *a, **k: True)
    assert ingest.process_file(incoming / "jura" / "doc.pdf") == "ok"


# ── TRANSIENT — bounded rate, never dropped, never moved ────────────────────

def test_transient_failure_backs_off_but_never_gives_up_and_recovers(incoming, monkeypatch):
    _mk(incoming, "t.pdf")
    monkeypatch.setattr(ingest, "extract_text", lambda p: "real text " * 50)
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    qdrant_up = {"v": False}
    attempts = {"n": 0}
    monkeypatch.setattr(ingest, "upsert_to_qdrant",
                        lambda *a, **k: (attempts.__setitem__("n", attempts["n"] + 1), qdrant_up["v"])[1])
    clock = {"t": 0.0}
    monkeypatch.setattr(ingest.time, "monotonic", lambda: clock["t"])

    state = ingest.new_state()
    key = str(incoming / "jura" / "t.pdf")
    for _ in range(2000):
        ingest.scan_once(state)
        clock["t"] += 60.0                                  # ~33 h
    assert key not in state["done"]
    assert key in state["retry_at"]
    assert attempts["n"] < 60, f"backoff must bound the retry rate (got {attempts['n']} in ~33 h)"
    assert state["backoff"][key] == ingest._BACKOFF_MAX_S

    qdrant_up["v"] = True
    clock["t"] += ingest._BACKOFF_MAX_S + 1
    ingest.scan_once(state)
    assert key in state["done"], "queued work must go through when the service recovers"


def test_never_resolving_transient_is_still_never_moved_or_dropped(incoming, monkeypatch):
    _mk(incoming, "forever.pdf")
    monkeypatch.setattr(ingest, "extract_text", lambda p: "text " * 50)
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    monkeypatch.setattr(ingest, "upsert_to_qdrant", lambda *a, **k: False)
    clock = {"t": 0.0}
    monkeypatch.setattr(ingest.time, "monotonic", lambda: clock["t"])
    state = ingest.new_state()
    key = str(incoming / "jura" / "forever.pdf")
    for _ in range(1000):
        ingest.scan_once(state)
        clock["t"] += 3600.0
    assert key not in state["done"]
    assert (incoming / "jura" / "forever.pdf").exists()          # never moved
    assert not (incoming / "_rejected").exists() or not list((incoming / "_rejected").glob("forever*"))


# ── DETERMINISTIC — quarantine, not gravplads ──────────────────────────────

def _quarantine_a_scanned_pdf(incoming, monkeypatch, name="scan.pdf"):
    _mk(incoming, name)
    calls = {"n": 0}
    monkeypatch.setattr(ingest, "extract_text",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1), "")[1])
    state = ingest.new_state()
    ingest.scan_once(state)
    return state, calls


def test_D3d_manifest_carries_all_five_field_groups(incoming, monkeypatch):
    """(d) reason / rejected_at / capability_fingerprint / provenance / attempt_state."""
    state, _ = _quarantine_a_scanned_pdf(incoming, monkeypatch)
    rows = _manifest(incoming)
    e = rows[-1]
    assert e["reason"] == "no_extractable_text"
    assert e["rejected_at"]
    fp = e["capability_fingerprint"]
    assert set(fp) == {"extractor", "parsers", "ocr", "embedding_model"}
    prov = e["provenance"]
    assert prov["original_path"].endswith("scan.pdf")
    assert prov["source_file"] == "scan.pdf"
    assert "wing" in prov and "scope" in prov and "subcategory" in prov
    assert "content_hash" in prov                                 # computed
    ats = e["attempt_state"]
    assert ats["evaluations"] == 1 and ats["last_attempt_at"]
    assert "chars" in ats["evidence"] or "None" in ats["evidence"]  # real failure detail


def test_D3a_unchanged_capability_never_re_evaluates(incoming, monkeypatch):
    """(a) a quarantined file with an unchanged capability is NOT re-evaluated
    across many cycles — no constant rescan."""
    state, calls = _quarantine_a_scanned_pdf(incoming, monkeypatch)
    assert calls["n"] == 1                                        # the one quarantining attempt
    for _ in range(500):
        ingest.scan_once(state)
    assert calls["n"] == 1, "unchanged capability → never touched again"
    assert list((incoming / "_rejected").glob("scan*.pdf"))       # still quarantined


def test_D3b_relevant_capability_change_returns_file_exactly_once(incoming, monkeypatch):
    """(b) a RELEVANT capability change (new extractor version) → the file is
    moved back into the queue exactly once."""
    state, _ = _quarantine_a_scanned_pdf(incoming, monkeypatch)
    orig = incoming / "jura" / "scan.pdf"
    assert not orig.exists() and list((incoming / "_rejected").glob("scan*.pdf"))

    # a relevant dimension changes: OCR becomes available
    base_fp = ingest.capability_fingerprint()
    changed = {**base_fp, "ocr": True}
    monkeypatch.setattr(ingest, "capability_fingerprint", lambda: changed)
    state["cap_fp"] = changed                                     # force the refresh

    # the extractor STILL returns "" — so the file returns to the queue, is
    # re-processed, and (still failing) re-quarantines against the NEW fingerprint.
    returned_events = {"n": 0}
    _orig_append = ingest._manifest_append
    monkeypatch.setattr(ingest, "_manifest_append",
                        lambda e: (returned_events.__setitem__("n", returned_events["n"] + (e.get("event") == "return")), _orig_append(e))[1])

    ingest.scan_once(state)
    assert returned_events["n"] == 1, "a relevant capability change must return the file to the queue exactly once"

    # run many more cycles — it must NOT ping-pong: it re-quarantined against the
    # NEW fingerprint and does not return again until the NEXT change
    for _ in range(200):
        ingest.scan_once(state)
    assert returned_events["n"] == 1, "must not ping-pong on the same capability state"
    rows = _manifest(incoming)
    reqs = [r for r in rows if r.get("event") == "requarantine"]
    assert reqs, "it re-quarantined after the failed re-evaluation"
    assert reqs[-1]["attempt_state"]["evaluations"] == 2          # incremented
    assert reqs[-1]["capability_fingerprint"]["ocr"] is True      # stored the NEW fp
    assert list((incoming / "_rejected").glob("scan*"))           # still quarantined


def test_D3c_irrelevant_capability_change_leaves_file_quarantined(incoming, monkeypatch):
    """(c) an IRRELEVANT capability change (embedding model) must NOT return a
    'no_extractable_text' quarantined file."""
    state, _ = _quarantine_a_scanned_pdf(incoming, monkeypatch)
    base_fp = ingest.capability_fingerprint()
    changed = {**base_fp, "embedding_model": "some-new-embed-model-v2"}
    monkeypatch.setattr(ingest, "capability_fingerprint", lambda: changed)
    state["cap_fp"] = changed
    for _ in range(50):
        ingest.scan_once(state)
    assert list((incoming / "_rejected").glob("scan*.pdf")), "irrelevant change → stays quarantined"
    assert not (incoming / "jura" / "scan.pdf").exists()
    assert not [r for r in _manifest(incoming) if r.get("event") == "return"]


def test_D3e_auto_return_is_bounded_no_pingpong_capability_actually_fixes_it(incoming, monkeypatch):
    """(e) bounded: if the capability change ACTUALLY fixes the file it ingests
    and is done; if it does NOT, it re-quarantines once (attempt_state++) and
    will not re-trigger until the next change."""
    _mk(incoming, "fix.pdf")
    text_box = {"v": ""}
    monkeypatch.setattr(ingest, "extract_text", lambda p: text_box["v"])
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    monkeypatch.setattr(ingest, "upsert_to_qdrant", lambda *a, **k: True)

    state = ingest.new_state()
    ingest.scan_once(state)                                       # quarantined (no text)
    assert list((incoming / "_rejected").glob("fix*.pdf"))

    # capability changes AND now the extractor works (a real new parser)
    changed = {**ingest.capability_fingerprint(), "parsers": {"pdf": "99.0", "txt": "builtin"}}
    monkeypatch.setattr(ingest, "capability_fingerprint", lambda: changed)
    state["cap_fp"] = changed
    text_box["v"] = "now there is real extractable text " * 20

    ingest.scan_once(state)                                       # returns + re-processes
    key = str(incoming / "jura" / "fix.pdf")
    for _ in range(20):
        ingest.scan_once(state)
    assert key in state["done"], "the fixed file must ingest successfully and finish"
    assert not list((incoming / "_rejected").glob("fix*.pdf")) or \
           not (incoming / "jura" / "fix.pdf").exists()


def test_manual_requeue_still_works_as_a_fallback(incoming, monkeypatch):
    """Moving the file back by hand is still a valid path back in."""
    state, _ = _quarantine_a_scanned_pdf(incoming, monkeypatch, "manual.pdf")
    q = list((incoming / "_rejected").glob("manual*.pdf"))[0]
    # operator moves it back and (say) it now has text
    monkeypatch.setattr(ingest, "extract_text", lambda p: "operator fixed this " * 30)
    monkeypatch.setattr(ingest, "embed_text", lambda c: [0.1] * 8)
    monkeypatch.setattr(ingest, "upsert_to_qdrant", lambda *a, **k: True)
    back = incoming / "jura" / "manual.pdf"
    q.rename(back)
    state["done"].discard(str(back))
    ingest.scan_once(state)
    assert str(back) in state["done"]


def test_quarantine_survives_restart_via_manifest(incoming, monkeypatch):
    state, _ = _quarantine_a_scanned_pdf(incoming, monkeypatch, "restart.pdf")
    # "restart": a fresh state rebuilds the quarantine set from the manifest
    fresh = ingest.new_state()
    ingest.load_quarantine(fresh)
    assert len(fresh["quarantine"]) == 1
    mt = next(iter(fresh["quarantine"]))
    assert fresh["quarantine"][mt]["reason"] == "no_extractable_text"
