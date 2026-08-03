"""
Browser-agent: risk detector.

Classifies network requests and UI interactions as READ (safe) or
WRITE (requires confirmation before execution).  Runs inside the
browser container — no NOUS-internal imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RiskLevel = Literal["READ", "WRITE"]

# URL fragments that almost always indicate a state-mutating action
_WRITE_PATH_PATTERNS: list[re.Pattern] = [re.compile(p, re.I) for p in [
    r"/checkout",
    r"/payment",
    r"/pay\b",
    r"/purchase",
    r"/order",
    r"/confirm",
    r"/delete",
    r"/remove",
    r"/submit",
    r"/send\b",
    r"/post\b",
    r"/publish",
    r"/sign[_-]?up",
    r"/register",
    r"/transfer",
    r"/withdraw",
    r"/deposit",
    r"/reply",
    r"/comment",
    r"/like",
    r"/share",
    r"/retweet",
    r"/unsubscribe",
    r"/logout",
    r"/account/close",
    r"/api/.*write",
]]

# Button/link text that signals irreversible intent
_WRITE_BUTTON_PATTERNS: list[re.Pattern] = [re.compile(p, re.I) for p in [
    r"^submit$",
    r"^confirm",
    r"^delete",
    r"^remove",
    r"^send$",
    r"^send message",
    r"^post$",
    r"^publish",
    r"^place order",
    r"^buy now",
    r"^pay",
    r"^checkout",
    r"^complete purchase",
    r"^transfer",
    r"^sign up",
    r"^register",
    r"^log ?out",
    r"^unsubscribe",
    r"^close account",
    r"^i agree",
]]


@dataclass
class RiskAssessment:
    level:       RiskLevel
    reason:      str
    url:         str
    method:      str
    body_sample: str = ""
    extra:       dict = field(default_factory=dict)


def assess_request(
    url: str,
    method: str,
    body: str | bytes | None = None,
    headers: dict | None = None,
) -> RiskAssessment:
    """Assess a network request.  Returns READ or WRITE."""
    method = (method or "GET").upper()
    url_lc = url.lower()
    body_sample = _truncate(body)

    # Safe HTTP verbs — no state mutation
    if method == "GET":
        return RiskAssessment("READ", "GET request", url, method, body_sample)

    # Resource preloads / telemetry GET-alikes
    if method == "HEAD" or method == "OPTIONS":
        return RiskAssessment("READ", f"{method} request", url, method, body_sample)

    # All mutating verbs: POST, PUT, PATCH, DELETE
    reason = f"HTTP {method}"
    for pat in _WRITE_PATH_PATTERNS:
        if pat.search(url_lc):
            reason = f"HTTP {method} to high-risk path ({pat.pattern})"
            break

    return RiskAssessment("WRITE", reason, url, method, body_sample)


def assess_button(text: str, element_type: str = "button") -> RiskAssessment:
    """Assess a UI element click before execution."""
    text_s = text.strip()
    for pat in _WRITE_BUTTON_PATTERNS:
        if pat.search(text_s):
            return RiskAssessment(
                "WRITE",
                f"Button text matches high-risk pattern: '{text_s}'",
                url="",
                method="UI_CLICK",
            )
    return RiskAssessment("READ", f"Button '{text_s}' deemed safe", url="", method="UI_CLICK")


def _truncate(body: str | bytes | None, max_len: int = 200) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return body[:max_len] + ("…" if len(body) > max_len else "")
