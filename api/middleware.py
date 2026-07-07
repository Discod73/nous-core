"""
NOUS Zero-Trust Scope Middleware.

Lag 2 af to-lags sikkerhedsmodellen (Lag 1 er nftables på swarm-firewall.sh).

Regel: requests fra Tailscale IP-range (100.64.0.0/10) må KUN nå
/swarm/* og /status. Alle andre endpoints returnerer HTTP 403 for
Tailscale-klienter, uanset auth-tokens.

Undtagelse: ejers egne Tailscale-enheder (OWNER_TAILSCALE_IPS) har fuld
adgang på linje med LAN — Tailscale ACL sikrer at kun godkendte enheder
overhovedet kan nå Pi'en.

Lokale klienter (127.x eller 192.168.1.x) har fuld adgang.
"""

import ipaddress
import logging
import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Ejers egne Tailscale-enheder — fuld adgang som LAN
# Sæt NOUS_OWNER_TAILSCALE_IPS til komma-separeret liste, f.eks. "100.x.y.z,100.a.b.c"
_owner_ts_env = os.environ.get("NOUS_OWNER_TAILSCALE_IPS", "")
OWNER_TAILSCALE_IPS: set[str] = set(ip.strip() for ip in _owner_ts_env.split(",") if ip.strip())

# Tailscale CGNAT-range
_TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")

# Lokale subnets der altid har fuld adgang
_LOCAL_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("192.168.1.0/24"),
    ipaddress.ip_network("172.16.0.0/12"),   # Docker/intern
    ipaddress.ip_network("10.0.0.0/8"),
]

# Endpoints Tailscale-peers MÅ nå (prefix-match)
_TAILSCALE_ALLOWED_PREFIXES = (
    "/swarm/",
    "/swarm",      # GET /swarm uden trailing slash
    "/status",
)


def _client_ip(request: Request) -> str:
    """Returner reel klient-IP (respekterer X-Forwarded-For fra lokal proxy)."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # Tag første IP i kæden — nærmeste klient
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def _is_tailscale(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return addr in _TAILSCALE_RANGE
    except ValueError:
        return False


def _is_local(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in r for r in _LOCAL_RANGES)
    except ValueError:
        return True  # ukendt format — lad den igennem, nftables håndterer det


class ScopeMiddleware(BaseHTTPMiddleware):
    """Blokerer Tailscale-IP'er fra at nå SECRET/PRIVATE-scope endpoints."""

    async def dispatch(self, request: Request, call_next: Callable):
        ip = _client_ip(request)

        if _is_tailscale(ip) and ip not in OWNER_TAILSCALE_IPS:
            path = request.url.path
            allowed = any(path.startswith(p) for p in _TAILSCALE_ALLOWED_PREFIXES)
            if not allowed:
                logger.warning(
                    f"Tailscale-IP {ip} afvist fra {path} — kun /swarm/* tilladt"
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Adgang nægtet: Tailscale-peers må kun nå /swarm/* endpoints. "
                                  "SECRET og PRIVATE scope kræver lokal forbindelse.",
                        "scope_required": "SWARM",
                        "your_ip": ip,
                    },
                )

        return await call_next(request)
