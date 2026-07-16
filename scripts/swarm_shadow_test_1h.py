#!/usr/bin/env python3
"""
1-times swarm shadow test.
Logger heartbeat-status hvert 5. minut. Ingen ændringer i config.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

LOG = Path("/srv/nous/logs/swarm_test_1h.log")
REPORT = Path("/srv/nous/logs/swarm_test_1h_report.json")

NANO_IP    = os.environ.get("SWARM_TEST_NODE", "localhost")
NANO_PORT  = int(os.environ.get("SWARM_TEST_PORT", "8000"))
PI_QDRANT  = "http://localhost:6333"

BASELINE = {"wing_a": 0, "wing_b": 0, "wing_c": 0}
WATCH_COLS = list(BASELINE.keys())

INTERVAL_S  = 300   # 5 min
DURATION_S  = 3600  # 60 min
TIMEOUT     = 8


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


def nano_stats() -> dict:
    """Memory + CPU på Nano via /status endpoint."""
    try:
        r = httpx.get(f"http://{NANO_IP}:{NANO_PORT}/status", timeout=TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return {
                "ram_mb": d.get("ram_used_mb"),
                "cpu_pct": d.get("cpu_pct"),
            }
    except Exception:
        pass
    # Fallback: /proc via SSH-less method not available — return None
    return {"ram_mb": None, "cpu_pct": None}


def nano_proc_alive() -> bool:
    """Check if uvicorn is answering — proxy for process alive."""
    try:
        r = httpx.get(f"http://{NANO_IP}:{NANO_PORT}/swarm/health", timeout=TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


def pi_collections() -> dict:
    counts = {}
    for col in WATCH_COLS:
        try:
            r = httpx.get(f"{PI_QDRANT}/collections/{col}", timeout=TIMEOUT)
            counts[col] = r.json()["result"]["points_count"]
        except Exception:
            counts[col] = -1
    return counts


def main():
    log("=" * 60)
    log("Swarm shadow test START — 60 min, interval 5 min")
    log(f"Nano: {NANO_IP}:{NANO_PORT} | Pi Qdrant: {PI_QDRANT}")
    log(f"Baseline: {BASELINE}")
    log("=" * 60)

    start_ts   = time.monotonic()
    round_num  = 0

    beats_sent     = 0
    beats_ok       = 0
    beats_missed   = 0
    nano_restarts  = 0   # inferred fra failures efter ok
    ram_samples    = []
    cpu_samples    = []

    prev_alive = True  # assume up at start

    while True:
        elapsed = time.monotonic() - start_ts
        if elapsed >= DURATION_S:
            break

        round_num += 1
        log(f"--- Round {round_num} (t={int(elapsed/60)}m{int(elapsed%60):02d}s) ---")

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

        # Nano resource stats
        stats = nano_stats()
        if stats["ram_mb"] is not None:
            ram_samples.append(stats["ram_mb"])
            cpu_samples.append(stats["cpu_pct"])
            log(f"  Nano stats: RAM={stats['ram_mb']}MB CPU={stats['cpu_pct']}%")
        else:
            log("  Nano stats: N/A (no /status data)")

        # Pi collection integrity
        cols = pi_collections()
        violations = []
        for col, cnt in cols.items():
            base = BASELINE.get(col, -1)
            if cnt != base:
                violations.append(f"{col}: expected {base}, got {cnt}")
        if violations:
            log(f"  VIOLATION: {'; '.join(violations)}")
        else:
            log(f"  Pi collections OK: {cols}")

        # Sleep until next interval
        next_wake = start_ts + round_num * INTERVAL_S
        sleep_s = max(0, next_wake - time.monotonic())
        if sleep_s > 0:
            time.sleep(sleep_s)

    # ── Final report ──────────────────────────────────────────────────────────
    elapsed_total = time.monotonic() - start_ts
    final_cols = pi_collections()
    col_ok = all(final_cols.get(c) == BASELINE[c] for c in BASELINE)

    report = {
        "test": "swarm_shadow_1h",
        "start_utc": now_iso(),
        "elapsed_min": round(elapsed_total / 60, 2),
        "rounds": round_num,
        "beats_sent": beats_sent,
        "beats_ok": beats_ok,
        "beats_missed": beats_missed,
        "beat_success_rate_pct": round(100 * beats_ok / max(beats_sent, 1), 1),
        "nano_restarts": nano_restarts,
        "pi_collections_final": final_cols,
        "pi_collections_baseline_ok": col_ok,
        "nano_ram_max_mb": max(ram_samples) if ram_samples else None,
        "nano_ram_mean_mb": round(sum(ram_samples) / len(ram_samples), 1) if ram_samples else None,
        "nano_cpu_max_pct": max(cpu_samples) if cpu_samples else None,
        "nano_cpu_mean_pct": round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None,
        "verdict": "PASS" if beats_missed == 0 and col_ok and nano_restarts == 0 else "FAIL",
    }

    log("=" * 60)
    log(f"SWARM SHADOW TEST COMPLETE — VERDICT: {report['verdict']}")
    log(f"  Heartbeats: {beats_ok}/{beats_sent} OK ({report['beat_success_rate_pct']}%)")
    log(f"  Missed: {beats_missed}")
    log(f"  Nano restarts: {nano_restarts}")
    log(f"  Pi collections baseline OK: {col_ok} — {final_cols}")
    if ram_samples:
        log(f"  Nano RAM max/mean: {report['nano_ram_max_mb']}/{report['nano_ram_mean_mb']} MB")
        log(f"  Nano CPU max/mean: {report['nano_cpu_max_pct']}/{report['nano_cpu_mean_pct']} %")
    log("=" * 60)

    REPORT.write_text(json.dumps(report, indent=2))
    log(f"Rapport gemt: {REPORT}")


if __name__ == "__main__":
    main()
