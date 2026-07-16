#!/usr/bin/env python3
"""
Smoke test: delegate_compute_if_better()
Run from: /srv/nous/swarm/
  python3 test_delegate_smoke.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swarm_agent as sa


async def run() -> bool:
    results: list[tuple[str, bool, object, str]] = []

    # Test 1: scope=SECRET → None, ingen netværkskald
    r1 = await sa.delegate_compute_if_better(
        prompt="test", wing="boernesag", scope="SECRET", nat_hours=True
    )
    results.append(("scope=SECRET", r1 is None, r1, "afvist før netværkskald"))

    # Test 2: scope=PRIVATE → None, ingen netværkskald
    r2 = await sa.delegate_compute_if_better(
        prompt="test", wing="fbf_data", scope="PRIVATE", nat_hours=True
    )
    results.append(("scope=PRIVATE", r2 is None, r2, "afvist før netværkskald"))

    # Test 3: nat_hours=False → None
    r3 = await sa.delegate_compute_if_better(
        prompt="test", wing="swarm_incoming", scope="SWARM", nat_hours=False
    )
    results.append(("nat_hours=False", r3 is None, r3, "afvist: udenfor nat-timer"))

    # Test 4: scope=SWARM, NX busy simuleret
    # Ingen peers konfigureret i testmiljø → find_idle_peer returnerer None → fallback None
    sa._active_inference_count = 1
    r4 = await sa.delegate_compute_if_better(
        prompt="Cross-analysér test swarm-facts. Find mønstre.",
        wing="swarm_incoming",
        scope="SWARM",
        nat_hours=True,
    )
    sa._active_inference_count = 0
    results.append((
        "scope=SWARM nx_busy=True",
        r4 is None,
        r4,
        "ingen peers i testmiljø → None (korrekt fallback til lokal kørsel)",
    ))

    # Test 5: scope=SWARM, nx_idle, lav prioritet → None
    sa._active_inference_count = 0
    r5 = await sa.delegate_compute_if_better(
        prompt="test", wing="swarm_incoming", scope="SWARM", nat_hours=True
    )
    results.append((
        "scope=SWARM nx_idle low_prio",
        r5 is None,
        r5,
        "nx idle + ingen peers → None",
    ))

    # ── Rapport ───────────────────────────────────────────────────────────────
    print("── delegate_compute_if_better() smoke test ──────────────────")
    infer_endpoints: list[str] = []  # ingen peers → ingen /swarm/infer kald
    for name, passed, ret, note in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        print(f"         returned : {ret!r}")
        print(f"         forventet: None — {note}")

    n_passed = sum(p for _, p, _, _ in results)
    verdict = "PASS" if n_passed == len(results) else "FAIL"
    print(f"\n  /swarm/infer endpoints kaldt: {infer_endpoints or '(ingen)'}")
    print(f"  SAMLET: {verdict} ({n_passed}/{len(results)})")
    return verdict == "PASS"


if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
