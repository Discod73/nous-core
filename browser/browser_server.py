"""
NOUS Browser Agent — FastAPI server running inside Docker container.

Security model:
- ALLE network requests fanges via Playwright route-interceptor
- POST/PUT/DELETE/PATCH pauser og sender confirmation-request til NOUS API
- Timeout: sessions auto-lukkes efter BROWSER_SESSION_TIMEOUT sekunder (default 1800)
- Intet Qdrant, ingen filsystem-adgang til NOUS data — kun callback til NOUS API

Environment variables (sættes via .env / Docker):
  BROWSER_NOUS_API_URL    NOUS API callback URL (default: http://host.docker.internal:8000)
  BROWSER_SERVER_PORT     Port denne server lytter på (default: 8030)
  BROWSER_SESSION_TIMEOUT Idle-timeout i sekunder (default: 1800)
  BROWSER_SESSION_KEY     Fernet-nøgle til krypteret session-persistens (valgfri)
  BROWSER_HEADLESS        true/false (default: true)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import (
    BrowserContext,
    Page,
    Route,
    Request as PWRequest,
    async_playwright,
)
from pydantic import BaseModel

from risk_detector import RiskAssessment, assess_request, assess_button
from session_manager import manager as _session_mgr

# ── Config ────────────────────────────────────────────────────────────────────
_NOUS_API      = os.environ.get("BROWSER_NOUS_API_URL", "http://host.docker.internal:8000")
_PORT          = int(os.environ.get("BROWSER_SERVER_PORT", "8030"))
_HEADLESS      = os.environ.get("BROWSER_HEADLESS", "true").lower() != "false"
_TIMEOUT_MS    = int(os.environ.get("BROWSER_NAV_TIMEOUT", "30000"))
_CONFIRM_WAIT  = int(os.environ.get("BROWSER_CONFIRM_WAIT", "120"))  # sec to wait for user

log = logging.getLogger("browser_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── State ─────────────────────────────────────────────────────────────────────
_pw_instance  = None
_browser      = None

# active playwright contexts, keyed by session_id
_contexts: dict[str, BrowserContext] = {}
_pages:    dict[str, Page]           = {}

# pending confirmation gates: action_id → asyncio.Event + result
_pending_gates: dict[str, dict] = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pw_instance, _browser
    _pw_instance = await async_playwright().start()
    _browser     = await _pw_instance.chromium.launch(headless=_HEADLESS)
    log.info("Chromium launched (headless=%s)", _HEADLESS)
    asyncio.create_task(_timeout_watchdog())
    yield
    for sid in list(_contexts.keys()):
        await _close_context(sid)
    await _browser.close()
    await _pw_instance.stop()
    log.info("Chromium stopped")


app = FastAPI(title="NOUS Browser Agent", lifespan=lifespan)


# ── Pydantic models ───────────────────────────────────────────────────────────
class NavigateReq(BaseModel):
    session_id: str
    url: str

class ClickReq(BaseModel):
    session_id: str
    selector: str
    text: str | None = None  # button text for risk assessment

class FillReq(BaseModel):
    session_id: str
    selector: str
    value: str

class ConfirmReq(BaseModel):
    action_id: str
    approved: bool


# ── Session helpers ───────────────────────────────────────────────────────────
async def _open_context(session_id: str) -> BrowserContext:
    storage = _session_mgr.load_storage_state(session_id)
    ctx = await _browser.new_context(
        storage_state=storage,
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    # Intercept ALL network requests for risk assessment
    await ctx.route("**/*", _route_handler)
    page = await ctx.new_page()
    page.set_default_timeout(_TIMEOUT_MS)
    _contexts[session_id] = ctx
    _pages[session_id]    = page
    return ctx


async def _close_context(session_id: str) -> None:
    page = _pages.pop(session_id, None)
    ctx  = _contexts.pop(session_id, None)
    if page and not page.is_closed():
        try:
            state = await _contexts.get(session_id, ctx).storage_state() if ctx else None
            if state:
                _session_mgr.save_storage_state(session_id, state)
        except Exception:
            pass
        await page.close()
    if ctx:
        await ctx.close()
    _session_mgr.destroy(session_id)
    log.info("Session %s closed", session_id)


def _get_page(session_id: str) -> Page:
    page = _pages.get(session_id)
    if page is None or page.is_closed():
        raise HTTPException(404, "Session not found or closed")
    s = _session_mgr.get(session_id)
    if s is None:
        raise HTTPException(404, "Session expired")
    s.touch()
    return page


# ── Route interceptor (core security mechanic) ────────────────────────────────
async def _route_handler(route: Route, request: PWRequest) -> None:
    assessment = assess_request(
        url    = request.url,
        method = request.method,
        body   = request.post_data,
    )

    if assessment.level == "READ":
        await route.continue_()
        _audit_to_nous(assessment)
        return

    # WRITE — gate requires confirmation before proceeding
    action_id = str(uuid.uuid4())
    gate: dict = {
        "event":    asyncio.Event(),
        "approved": False,
        "assessment": assessment,
        "created_at": time.time(),
    }
    _pending_gates[action_id] = gate

    await _request_confirmation(action_id, assessment)

    try:
        await asyncio.wait_for(gate["event"].wait(), timeout=_CONFIRM_WAIT)
    except asyncio.TimeoutError:
        log.warning("Confirmation timeout for action %s — aborting", action_id)
        _pending_gates.pop(action_id, None)
        await route.abort("timedout")
        return

    _pending_gates.pop(action_id, None)
    if gate["approved"]:
        _audit_to_nous(assessment, confirmed=True)
        await route.continue_()
    else:
        _audit_to_nous(assessment, confirmed=False)
        await route.abort("failed")


async def _request_confirmation(action_id: str, a: RiskAssessment) -> None:
    """POST confirmation request to NOUS API (fire-and-forget)."""
    payload = {
        "action_id":   action_id,
        "level":       a.level,
        "reason":      a.reason,
        "url":         a.url,
        "method":      a.method,
        "body_sample": a.body_sample,
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{_NOUS_API}/browser/pending", json=payload)
    except Exception as exc:
        log.warning("Could not reach NOUS API for confirmation: %s", exc)


def _audit_to_nous(a: RiskAssessment, confirmed: bool | None = None) -> None:
    """Send audit event to NOUS API (non-blocking)."""
    payload = {
        "event_type":  a.level,
        "url":         a.url,
        "method":      a.method,
        "reason":      a.reason,
        "confirmed":   confirmed,
    }
    asyncio.create_task(_post_audit(payload))


async def _post_audit(payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{_NOUS_API}/browser/audit", json=payload)
    except Exception:
        pass  # audit failure must never crash agent


# ── Timeout watchdog ──────────────────────────────────────────────────────────
async def _timeout_watchdog() -> None:
    while True:
        await asyncio.sleep(60)
        expired = [
            sid for sid, s in {sid: _session_mgr.get(sid) for sid in list(_pages.keys())}.items()
            if s is None or s.is_expired()
        ]
        for sid in expired:
            log.info("Auto-closing idle session %s", sid)
            await _close_context(sid)


# ── API endpoints ─────────────────────────────────────────────────────────────
@app.post("/session/start")
async def session_start():
    s   = _session_mgr.create()
    ctx = await _open_context(s.session_id)
    log.info("Session started: %s", s.session_id)
    return {"session_id": s.session_id, "status": "ok"}


@app.post("/session/stop")
async def session_stop(body: dict):
    sid = body.get("session_id", "")
    if sid in _contexts:
        await _close_context(sid)
    return {"status": "closed"}


@app.get("/session/status")
async def session_status():
    return {"sessions": _session_mgr.list_active()}


@app.post("/navigate")
async def navigate(req: NavigateReq):
    page = _get_page(req.session_id)
    try:
        resp = await page.goto(req.url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        title = await page.title()
        log.info("Navigated to %s (title: %s)", req.url, title)
        return {"status": "ok", "url": page.url, "title": title, "http_status": resp.status if resp else None}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/click")
async def click(req: ClickReq):
    page = _get_page(req.session_id)
    # Pre-assess button click risk
    text = req.text or ""
    assessment = assess_button(text)
    if assessment.level == "WRITE":
        # Gate the click before executing it
        action_id = str(uuid.uuid4())
        gate: dict = {
            "event":      asyncio.Event(),
            "approved":   False,
            "assessment": assessment,
            "created_at": time.time(),
        }
        _pending_gates[action_id] = gate
        await _request_confirmation(action_id, assessment)
        try:
            await asyncio.wait_for(gate["event"].wait(), timeout=_CONFIRM_WAIT)
        except asyncio.TimeoutError:
            _pending_gates.pop(action_id, None)
            return {"status": "rejected", "reason": "confirmation timeout"}
        _pending_gates.pop(action_id, None)
        if not gate["approved"]:
            _audit_to_nous(assessment, confirmed=False)
            return {"status": "rejected", "reason": "user rejected"}
        _audit_to_nous(assessment, confirmed=True)

    try:
        await page.click(req.selector, timeout=_TIMEOUT_MS)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/fill")
async def fill(req: FillReq):
    page = _get_page(req.session_id)
    # Form fill is READ-level (user explicitly chose to fill this field)
    try:
        await page.fill(req.selector, req.value, timeout=_TIMEOUT_MS)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/screenshot")
async def screenshot(session_id: str):
    page = _get_page(session_id)
    try:
        import base64
        data = await page.screenshot(type="png")
        return {"screenshot": base64.b64encode(data).decode()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/pending")
async def pending_confirmations():
    now = time.time()
    return {
        "pending": [
            {
                "action_id":   aid,
                "level":       g["assessment"].level,
                "reason":      g["assessment"].reason,
                "url":         g["assessment"].url,
                "method":      g["assessment"].method,
                "body_sample": g["assessment"].body_sample,
                "waiting_s":   round(now - g["created_at"], 1),
            }
            for aid, g in _pending_gates.items()
        ]
    }


@app.post("/confirm/{action_id}")
async def confirm_action(action_id: str):
    gate = _pending_gates.get(action_id)
    if gate is None:
        raise HTTPException(404, "No pending action with that id")
    gate["approved"] = True
    gate["event"].set()
    return {"status": "approved"}


@app.post("/reject/{action_id}")
async def reject_action(action_id: str):
    gate = _pending_gates.get(action_id)
    if gate is None:
        raise HTTPException(404, "No pending action with that id")
    gate["approved"] = False
    gate["event"].set()
    return {"status": "rejected"}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_pages)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=_PORT, log_level="info")
