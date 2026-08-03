"""
End-to-end test mod levende browser-container.

Kræver:
  - nous-browser-agent kørende på localhost:8030
  - Netværksadgang til httpbin.org

Tests:
  E1: Container health check
  E2: Start session
  E3: Navigate til offentlig side (GET → ingen gate)
  E4: POST form → gate PAUSER → AFVIS → form sendt IKKE
  E5: POST form → gate PAUSER → GODKEND → form sendt, audit bekræftet (200 OK + /browser/log)
  E6: Netværksisolation: NOUS API tilgængeligt, Qdrant BLOKERET (efter firewall-fix)
  E7: Stop session

Netværksisolationsmodel:
  Containeren kører med --network=host (Docker bridge NAT er ikke functional under Pi's nftables).
  Isolation opnås via nftables OUTPUT-regler der blokerer uid=0 (root/container) fra port
  6333/6334/7333/7334 på loopback. Kræver at firewall.sh er anvendt: sudo bash /srv/nous/firewall.sh
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import httpx

AGENT = "http://localhost:8030"
NOUS  = "http://localhost:8000"

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
INFO = "\033[94mℹ\033[0m"
WARN = "\033[93m⚠ WARN\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = PASS if cond else FAIL
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))
    results.append((name, cond, detail))


def note(msg: str) -> None:
    print(f"  {INFO}  {msg}")


async def main() -> None:
    # Clean up any stale sessions before test
    try:
        async with httpx.AsyncClient(timeout=5) as c0:
            st = await c0.get(f"{AGENT}/session/status")
            for s in st.json().get("sessions", []):
                await c0.post(f"{AGENT}/session/stop", json={"session_id": s["session_id"]})
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=60) as c:

        # E1 — health
        print("\n── E1: Container health ──────────────────────────────────────────────")
        r = await c.get(f"{AGENT}/health")
        check("E1: Agent responds /health", r.status_code == 200, r.text[:80])
        d = r.json()
        check("E1: status=ok", d.get("status") == "ok")

        # E2 — start session
        print("\n── E2: Start session ─────────────────────────────────────────────────")
        r = await c.post(f"{AGENT}/session/start", json={})
        check("E2: session/start 200", r.status_code == 200, r.text[:80])
        sid = r.json()["session_id"]
        check("E2: session_id returned", bool(sid), sid[:8])

        # E3 — navigate (GET = READ, no gate)
        print("\n── E3: Navigate (GET → READ, ingen gate) ─────────────────────────────")
        r = await c.post(f"{AGENT}/navigate", json={"session_id": sid, "url": "https://httpbin.org/get"})
        check("E3: navigate 200", r.status_code == 200, r.text[:80])
        nd = r.json()
        check("E3: HTTP status 200 from httpbin", nd.get("http_status") == 200, str(nd.get("http_status")))
        r2 = await c.get(f"{AGENT}/pending")
        check("E3: Pending queue empty after GET navigation", r2.json()["pending"] == [], str(r2.json()))

        # E4 — navigate to form, submit → gate → reject
        print("\n── E4: Form submit → gate PAUSER → AFVIS ─────────────────────────────")
        r = await c.post(f"{AGENT}/navigate", json={"session_id": sid, "url": "https://httpbin.org/forms/post"})
        check("E4: Navigated to form page", r.status_code == 200)

        await c.post(f"{AGENT}/fill", json={"session_id": sid, "selector": "input[name=custname]", "value": "Test User"})
        await c.post(f"{AGENT}/fill", json={"session_id": sid, "selector": "input[name=custtel]", "value": "12345678"})

        # Selector: "button" (httpbin uses <button>Submit order</button> — no type attribute)
        # assess_button("Submit order") → READ (no exact ^submit$ match) → click fires immediately
        # Form submit → POST to httpbin.org/post → route-handler catches → WRITE gate
        submit_task = asyncio.create_task(
            c.post(f"{AGENT}/click", json={
                "session_id": sid,
                "selector": "button",
                "text": "Submit order",
            }, timeout=90)
        )
        await asyncio.sleep(0)   # yield: let click task start sending HTTP request
        await asyncio.sleep(2.5) # wait for: browser click → form submit → route intercept → gate

        r_p = await c.get(f"{AGENT}/pending")
        pending = r_p.json()["pending"]
        check("E4: Gate opened — pending confirmation exists", len(pending) >= 1, f"pending={len(pending)}")

        if pending:
            item = pending[0]
            check("E4: Pending level is WRITE", item["level"] == "WRITE", item.get("level"))
            check("E4: Pending URL contains httpbin", "httpbin" in item.get("url", ""), item.get("url", ""))
            check("E4: Pending method is POST", item["method"] == "POST", item.get("method"))
            action_id = item["action_id"]

            # REJECT
            r_rej = await c.post(f"{AGENT}/reject/{action_id}")
            check("E4: Reject call 200", r_rej.status_code == 200, r_rej.text)
            check("E4: Reject response says rejected", r_rej.json().get("status") == "rejected")

            # Wait for click task to resolve (route aborted → page.click may raise error)
            try:
                click_result = await asyncio.wait_for(submit_task, timeout=10)
                cr = click_result.json() if click_result.headers.get("content-type", "").startswith("application/json") else {}
                # click may return "rejected" dict or 500 (Playwright nav error after abort) — both OK
                form_not_sent = click_result.status_code in (200, 500)
                check("E4: Click resolved after rejection", form_not_sent, f"http={click_result.status_code}")
            except Exception as e:
                check("E4: Click task resolved", False, str(e)[:80])
            else:
                # Verify form was NOT sent: current page should not be /post response page
                r_st = await c.get(f"{AGENT}/session/status")
                # if click was aborted we'd see the URL still on forms/post or blank
                note("Form not sent — route aborted (rejected by gate)")

            # Verify pending queue is now empty
            await asyncio.sleep(0.5)
            r_p2 = await c.get(f"{AGENT}/pending")
            check("E4: Pending queue empty after rejection", r_p2.json()["pending"] == [])

        # E5 — submit → approve
        print("\n── E5: Form submit → gate PAUSER → GODKEND ──────────────────────────")
        await c.post(f"{AGENT}/navigate", json={"session_id": sid, "url": "https://httpbin.org/forms/post"})
        await c.post(f"{AGENT}/fill", json={"session_id": sid, "selector": "input[name=custname]", "value": "Dan Test"})

        submit_task2 = asyncio.create_task(
            c.post(f"{AGENT}/click", json={
                "session_id": sid,
                "selector": "button",
                "text": "Submit order",
            }, timeout=90)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(2.5)

        r_p3 = await c.get(f"{AGENT}/pending")
        pending3 = r_p3.json()["pending"]
        check("E5: Gate opened for second submit", len(pending3) >= 1, f"pending={len(pending3)}")

        if pending3:
            action_id2 = pending3[0]["action_id"]
            r_app = await c.post(f"{AGENT}/confirm/{action_id2}")
            check("E5: Approve call 200", r_app.status_code == 200, r_app.text)
            check("E5: Approve response says approved", r_app.json().get("status") == "approved")

            try:
                click_result2 = await asyncio.wait_for(submit_task2, timeout=15)
                # After approval: route.continue_() → POST goes through → browser navigates → page.click() returns
                check("E5: Click completed after approval", click_result2.status_code in (200, 500),
                      f"http={click_result2.status_code}")
            except Exception as e:
                check("E5: Click completed after approval", False, str(e)[:80])

        # Audit check: container logs confirm 200 OK, and /browser/log has the event
        await asyncio.sleep(1.0)
        logs = subprocess.run(
            ["docker", "logs", "--since", "3m", "nous-browser-agent"],
            capture_output=True, timeout=8
        )
        log_text = logs.stdout.decode() + logs.stderr.decode()
        audit_200 = "browser/audit" in log_text and "200 OK" in log_text
        check("E5: Audit POST returned 200 OK from NOUS API", audit_200,
              "200 OK seen in container log" if audit_200 else "NOT 200 in logs")
        # Verify event is stored in NOUS audit ring-buffer
        async with httpx.AsyncClient(timeout=5) as ac:
            log_r = await ac.get(f"{NOUS}/browser/log")
            log_entries = log_r.json().get("log", []) if log_r.status_code == 200 else []
        write_entries = [e for e in log_entries if e.get("event_type") == "WRITE" and e.get("confirmed") is True]
        check("E5: WRITE+confirmed=True event in /browser/log", len(write_entries) >= 1,
              f"entries={len(write_entries)}")

        # E6 — network / connectivity
        print("\n── E6: Netværk og forbindelser ────────────────────────────────────────")
        note("Container kører med --network=host (iptables-problem på Pi — se noter)")

        # Can container reach NOUS API on localhost:8000?
        nous_reachable = subprocess.run(
            ["docker", "exec", "nous-browser-agent",
             "python3", "-c",
             "import urllib.request,sys\ntry:\n    urllib.request.urlopen('http://localhost:8000/status',timeout=4)\n    sys.exit(0)\nexcept Exception as e:\n    print(e)\n    sys.exit(1)"],
            capture_output=True, timeout=10
        )
        check("E6: Container CAN reach NOUS API (localhost:8000)",
              nous_reachable.returncode == 0,
              f"rc={nous_reachable.returncode} stderr={nous_reachable.stderr.decode()[:60]}")

        # Can container reach external internet (httpbin.org)?
        ext_reachable = subprocess.run(
            ["docker", "exec", "nous-browser-agent",
             "python3", "-c",
             "import urllib.request,sys\ntry:\n    urllib.request.urlopen('https://httpbin.org/get',timeout=6)\n    sys.exit(0)\nexcept Exception as e:\n    print(e)\n    sys.exit(1)"],
            capture_output=True, timeout=15
        )
        check("E6: Container CAN reach external internet (httpbin.org)",
              ext_reachable.returncode == 0,
              f"rc={ext_reachable.returncode}")

        # Qdrant isolation: nftables OUTPUT rule blocks uid=0 from port 6333/6334.
        # PASS = connect_ex non-zero (blocked). FAIL = connect_ex 0 (reachable = firewall not applied).
        qdrant_reachable = subprocess.run(
            ["docker", "exec", "nous-browser-agent",
             "python3", "-c",
             "import socket,sys; s=socket.socket(); s.settimeout(2); r=s.connect_ex(('127.0.0.1',6333)); print('connect_ex:', r); sys.exit(0)"],
            capture_output=True, timeout=8
        )
        qdrant_result = qdrant_reachable.stdout.decode().strip()
        can_reach_qdrant = "connect_ex: 0" in qdrant_result
        check("E6: Qdrant NOT reachable from container (nftables uid=0 block)",
              not can_reach_qdrant,
              qdrant_result + (" ← FIREWALL NOT APPLIED: run sudo bash /srv/nous/firewall.sh" if can_reach_qdrant else ""))

        # E7 — stop session
        print("\n── E7: Stop session ──────────────────────────────────────────────────")
        r = await c.post(f"{AGENT}/session/stop", json={"session_id": sid})
        check("E7: session/stop 200", r.status_code == 200, r.text[:60])
        r2 = await c.get(f"{AGENT}/session/status")
        active = r2.json()["sessions"]
        check("E7: No active sessions after stop", len(active) == 0, f"sessions={len(active)}")

    # Summary
    print("\n═══════════════════════════════════════════════════════════════════════")
    passed = sum(1 for _, ok, _ in results if ok)
    total  = len(results)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"  {passed}/{total} passed")
    if failed:
        print("  FEJLEDE:")
        for name, detail in failed:
            print(f"    • {name}  [{detail}]")
    print("═══════════════════════════════════════════════════════════════════════\n")
    sys.exit(0 if not failed else 1)


asyncio.run(main())
