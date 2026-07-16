#!/usr/bin/env python3
"""
24-timers swarm shadow test.
Logger heartbeat-status hvert 15. minut. Ingen ændringer i config.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

LOG    = Path("/srv/nous/logs/swarm_test_24h.log")
REPORT = Path("/srv/nous/logs/swarm_test_24h_report.json")

NANO_IP   = os.environ.get("SWARM_TEST_NODE", "localhost")
NANO_PORT = int(os.environ.get("SWARM_TEST_PORT", "8000"))
PI_QDRANT = "http://localhost:6333"

BASELINE   = {"wing_a": 0, "wing_b": 0, "wing_c": 0}
INTERVAL_S = 900    # 15 min
DURATION_S = 86400  # 24h
TIMEOUT    = 8


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str):
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def ping_nano() -> dict:
    try:
        r = httpx.get(f"http://{NANO_IP}:{NANO_PORT}/swarm/health", timeout=TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return {"ok": True, "node_id": d.get("node_id"), "status": d.get("status")}
        return {"ok": False, "http": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def pi_collections() -> dict:
    counts = {}
    for col in BASELINE:
        try:
            r = httpx.get(f"{PI_QDRANT}/collections/{col}", timeout=TIMEOUT)
            counts[col] = r.json()["result"]["points_count"]
        except Exception:
            counts[col] = -1
    return counts


def save_progress(data: dict):
    REPORT.write_text(json.dumps(data, indent=2))


def main():
    log("=" * 64)
    log("Swarm shadow test 24H START")
    log(f"Duration: 1440 min | Interval: 15 min | Rounds: 96")
    log(f"Nano: {NANO_IP}:{NANO_PORT} | Pi Qdrant: {PI_QDRANT}")
    log(f"Baseline: {BASELINE}")
    log("=" * 64)

    start_ts  = time.monotonic()
    start_utc = now_iso()
    round_num = 0

    beats_sent   = 0
    beats_ok     = 0
    beats_missed = 0
    nano_restarts = 0
    col_violations: list[str] = []

    prev_alive = True

    while True:
        elapsed = time.monotonic() - start_ts
        if elapsed >= DURATION_S:
            break

        round_num += 1
        elapsed_h = int(elapsed // 3600)
        elapsed_m = int((elapsed % 3600) // 60)
        pct = round(100 * elapsed / DURATION_S, 1)
        log(f"--- Round {round_num}/96 (t={elapsed_h}h{elapsed_m:02d}m, {pct}%) ---")

        # Heartbeat
        beats_sent += 1
        hb = ping_nano()
        if hb["ok"]:
            beats_ok += 1
            log(f"  Heartbeat OK — node_id={hb.get('node_id')} status={hb.get('status')}")
            if not prev_alive:
                nano_restarts += 1
                log(f"  ** Nano recovered (restart #{nano_restarts})")
            prev_alive = True
        else:
            beats_missed += 1
            log(f"  Heartbeat MISS — {hb}")
            prev_alive = False

        # Pi collection integrity
        cols = pi_collections()
        viols = [f"{c}: expected {BASELINE[c]}, got {cols[c]}"
                 for c in BASELINE if cols.get(c) != BASELINE[c]]
        if viols:
            for v in viols:
                log(f"  VIOLATION: {v}")
                col_violations.append(f"[round {round_num}] {v}")
        else:
            log(f"  Pi collections OK: {cols}")

        # Save progress every round
        progress = {
            "test": "swarm_shadow_24h",
            "start_utc": start_utc,
            "saved_at": now_iso(),
            "elapsed_h": round(elapsed / 3600, 2),
            "elapsed_pct": pct,
            "rounds_done": round_num,
            "beats_sent": beats_sent,
            "beats_ok": beats_ok,
            "beats_missed": beats_missed,
            "beat_success_rate_pct": round(100 * beats_ok / max(beats_sent, 1), 1),
            "nano_restarts": nano_restarts,
            "pi_collections_latest": cols,
            "col_violations": col_violations,
            "verdict": "RUNNING",
        }
        save_progress(progress)

        # Sleep until next interval
        next_wake = start_ts + round_num * INTERVAL_S
        sleep_s = max(0, next_wake - time.monotonic())
        if sleep_s > 0:
            time.sleep(sleep_s)

    # ── Final report ──────────────────────────────────────────────────────────
    elapsed_total = time.monotonic() - start_ts
    final_cols = pi_collections()
    col_ok = all(final_cols.get(c) == BASELINE[c] for c in BASELINE)

    verdict = "PASS" if beats_missed == 0 and col_ok and nano_restarts == 0 else "FAIL"

    report = {
        "test": "swarm_shadow_24h",
        "start_utc": start_utc,
        "end_utc": now_iso(),
        "elapsed_h": round(elapsed_total / 3600, 3),
        "rounds": round_num,
        "beats_sent": beats_sent,
        "beats_ok": beats_ok,
        "beats_missed": beats_missed,
        "beat_success_rate_pct": round(100 * beats_ok / max(beats_sent, 1), 1),
        "nano_restarts": nano_restarts,
        "pi_collections_final": final_cols,
        "pi_collections_baseline_ok": col_ok,
        "col_violations": col_violations,
        "nano_ram_max_mb": None,
        "nano_ram_mean_mb": None,
        "nano_cpu_max_pct": None,
        "nano_cpu_mean_pct": None,
        "verdict": verdict,
    }

    save_progress(report)

    log("=" * 64)
    log(f"SWARM SHADOW TEST 24H COMPLETE — VERDICT: {verdict}")
    log(f"  Heartbeats: {beats_ok}/{beats_sent} OK ({report['beat_success_rate_pct']}%)")
    log(f"  Missed: {beats_missed} | Nano restarts: {nano_restarts}")
    log(f"  Pi collections baseline OK: {col_ok} — {final_cols}")
    if col_violations:
        for v in col_violations:
            log(f"  VIOLATION: {v}")
    log("=" * 64)
    log(f"Rapport gemt: {REPORT}")


if __name__ == "__main__":
    main()
