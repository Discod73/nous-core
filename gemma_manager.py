#!/usr/bin/env python3
"""Gemma 4 llama.cpp-server livscyklus — start/stop on-demand."""
import asyncio
import os

import httpx

_SERVICE = "nous-llama-server.service"
_LLAMA_URL = os.environ.get("NOUS_NX_LLAMA_URL", "http://localhost:8181")
_STARTUP_TIMEOUT = 150  # sekunder — 12B model load tager ~60-120s


async def start() -> None:
    """Start servicen og vent til Gemma er klar til at modtage anmodninger."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "start", _SERVICE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"systemctl start fejlede: {stderr.decode()[:300]}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_TIMEOUT
    async with httpx.AsyncClient() as client:
        while loop.time() < deadline:
            try:
                r = await client.get(f"{_LLAMA_URL}/health", timeout=5.0)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
    raise TimeoutError(f"Gemma server ikke klar inden {_STARTUP_TIMEOUT}s")


async def stop() -> None:
    """Stop servicen og frigiv GPU-hukommelse på NX."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "stop", _SERVICE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    # exit 5 = unit ikke loaded (allerede stoppet) — ikke en fejl
    if proc.returncode not in (0, 5):
        raise RuntimeError(f"systemctl stop fejlede: {stderr.decode()[:300]}")
