#!/usr/bin/env python3
"""
Smoke test for NOUS audit log hash-chain.

Verificerer:
  1. 3 entries skrives korrekt (WRITE + QUERY + SCOPE_VIOLATION)
  2. Hash-kæden er intakt (prev_hash → entry_hash kæde)
  3. Ingen DELETE-endpoint eksisterer i arbiter eller API
  4. Udskriver de første 3 entries med hashes
"""
import json
import sys
from pathlib import Path

# Add NOUS root to path so audit_log is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audit_log import log_event, verify_chain, AUDIT_FILE, AUDIT_DIR

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def main() -> int:
    errors = 0

    section("1. Skriver 3 audit-entries")

    # Start fra ren tilstand for smoke test
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    test_log = AUDIT_DIR / "smoke_test.log"
    if test_log.exists():
        test_log.unlink()

    # Patch AUDIT_FILE temporarily for this test
    import audit_log as _al
    _original_file = _al.AUDIT_FILE
    _al.AUDIT_FILE = test_log

    log_event("WRITE",           "boernesag", "SECRET",  "api",    "upsert 2 point(s) via api")
    log_event("QUERY",           "fbf_data",  "PRIVATE", "dan",    "hvem har sagt hvad om forældremyndighed")
    log_event("SCOPE_VIOLATION", "boernesag", "SECRET",  "unknown","external_chat blocked: SECRET unconfirmed")

    # Also verify that SWARM/PUBLIC are silently ignored
    log_event("WRITE", "nous_projekt", "SWARM", "api", "this should not appear")

    _al.AUDIT_FILE = _original_file

    lines = [ln for ln in test_log.read_text().splitlines() if ln.strip()]
    if len(lines) == 3:
        print(f"  {PASS}  3 entries skrevet (SWARM-entry korrekt ignoreret)")
    else:
        print(f"  {FAIL}  forventede 3 entries, fik {len(lines)}")
        errors += 1

    section("2. Hash-kæde verifikation")

    import audit_log as _al2
    _al2.AUDIT_FILE = test_log
    result = verify_chain(test_log)
    _al2.AUDIT_FILE = _original_file

    if result["ok"]:
        print(f"  {PASS}  Hash-kæde intakt ({result['entries']} entries, 0 fejl)")
    else:
        print(f"  {FAIL}  Hash-kæde BRUDT:")
        for err in result["errors"]:
            print(f"         {err}")
        errors += 1

    section("3. Tjek: ingen DELETE-endpoint i arbiter eller API")

    arbiter_src = Path("/srv/nous/arbiter/arbiter.py").read_text()
    api_src     = Path("/srv/nous/api/main.py").read_text()
    audit_src   = Path("/srv/nous/audit_log.py").read_text()

    # No @app.delete routes for audit in either file
    import re
    audit_delete_routes = re.findall(
        r'@app\.delete.*audit', arbiter_src + api_src, re.IGNORECASE
    )
    if not audit_delete_routes:
        print(f"  {PASS}  Ingen @app.delete audit-endpoints fundet")
    else:
        print(f"  {FAIL}  DELETE audit-endpoint fundet: {audit_delete_routes}")
        errors += 1

    # audit_log.py itself has no delete/unlink/remove of the log file
    dangerous = [ln for ln in audit_src.splitlines()
                 if any(kw in ln for kw in ("unlink", "remove", "rmdir", "shutil.rm"))
                 and "rotate" not in ln.lower()]
    if not dangerous:
        print(f"  {PASS}  audit_log.py indeholder ingen sletningsoperationer")
    else:
        print(f"  {FAIL}  Mistænkelige linier i audit_log.py:")
        for ln in dangerous:
            print(f"         {ln.strip()}")
        errors += 1

    section("4. Første 3 entries med hashes")

    entries = [json.loads(ln) for ln in lines]
    for i, e in enumerate(entries, 1):
        print(f"\n  Entry {i}:")
        print(f"    timestamp:  {e['timestamp']}")
        print(f"    event_type: {e['event_type']}")
        print(f"    wing:       {e['wing']}")
        print(f"    scope:      {e['scope']}")
        print(f"    user:       {e['user']}")
        print(f"    summary:    {e['summary']}")
        print(f"    prev_hash:  {e['prev_hash'] or '(genesis)'}")
        print(f"    entry_hash: {e['entry_hash']}")

    # Verify chain linkage visually
    print(f"\n  Kæde-links:")
    for i in range(1, len(entries)):
        link_ok = entries[i]["prev_hash"] == entries[i - 1]["entry_hash"]
        status  = PASS if link_ok else FAIL
        print(f"  {status}  entry {i} → entry {i+1}: "
              f"{entries[i-1]['entry_hash'][:12]}… → {entries[i]['prev_hash'][:12]}…")

    section("Resultat")
    if errors == 0:
        print(f"  {PASS}  Alle checks bestået")
        # Clean up test file
        test_log.unlink(missing_ok=True)
    else:
        print(f"  {FAIL}  {errors} check(s) fejlede — test-log bevaret: {test_log}")

    return errors


if __name__ == "__main__":
    sys.exit(main())
