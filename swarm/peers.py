"""NOUS Swarm — Peer Management."""
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

import httpx

PEERS_FILE = Path("/mnt/nous-data/swarm_peers.json")
_ACTIVE_HOURS = 24


def _load() -> dict:
    if PEERS_FILE.exists():
        try:
            return json.loads(PEERS_FILE.read_text())
        except Exception:
            pass
    return {"peers": []}


def _save(data: dict) -> None:
    PEERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PEERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add_peer(tailscale_ip: str, label: str, swarm_type: str, port: int = 8020) -> dict:
    data = _load()
    for p in data["peers"]:
        if p["tailscale_ip"] == tailscale_ip:
            return p
    peer = {
        "node_id":      str(uuid.uuid4()),
        "label":        label,
        "tailscale_ip": tailscale_ip,
        "port":         port,
        "added_at":     datetime.now(timezone.utc).isoformat(),
        "last_seen":    None,
        "trusted":      True,
        "swarm_type":   swarm_type,
    }
    data["peers"].append(peer)
    _save(data)
    return peer


def remove_peer(node_id: str) -> bool:
    data = _load()
    before = len(data["peers"])
    data["peers"] = [p for p in data["peers"] if p["node_id"] != node_id]
    if len(data["peers"]) < before:
        _save(data)
        return True
    return False


def get_all_peers() -> list[dict]:
    return _load()["peers"]


def get_active_peers() -> list[dict]:
    """Peers der er seen inden for 24 timer, eller aldrig pinget (nyligt tilføjet)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_ACTIVE_HOURS)
    result = []
    for p in _load()["peers"]:
        ls = p.get("last_seen")
        if ls is None:
            result.append(p)
            continue
        try:
            if datetime.fromisoformat(ls) >= cutoff:
                result.append(p)
        except ValueError:
            pass
    return result


def update_last_seen(node_id: str) -> None:
    data = _load()
    now = datetime.now(timezone.utc).isoformat()
    for p in data["peers"]:
        if p["node_id"] == node_id:
            p["last_seen"] = now
            break
    _save(data)


def peer_by_ip(ip: str) -> dict | None:
    return next((p for p in _load()["peers"] if p["tailscale_ip"] == ip), None)


def ping_peer(peer: dict, timeout: float = 5.0) -> bool:
    url = f"http://{peer['tailscale_ip']}:{peer['port']}/swarm/health"
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code == 200:
            update_last_seen(peer["node_id"])
            return True
    except Exception:
        pass
    return False


class PeerLoadCache:
    """In-memory cache for peer /swarm/health responses, TTL=30s.

    Caller (requester node) bruger denne — ingen disk-writes på idle peer.
    """
    TTL = 30.0

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._lock = Lock()

    def get(self, peer: dict, timeout: float = 5.0) -> dict | None:
        """Returner cachet health-data for peer, eller fetch hvis forældet."""
        node_id = peer["node_id"]
        with self._lock:
            entry = self._cache.get(node_id)
            if entry and time.monotonic() < entry["expires_at"]:
                return entry["data"]

        url = f"http://{peer['tailscale_ip']}:{peer['port']}/swarm/health"
        try:
            r = httpx.get(url, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                with self._lock:
                    self._cache[node_id] = {
                        "data":       data,
                        "expires_at": time.monotonic() + self.TTL,
                    }
                update_last_seen(node_id)
                return data
        except Exception:
            pass
        return None

    def invalidate(self, node_id: str) -> None:
        with self._lock:
            self._cache.pop(node_id, None)


peer_load_cache = PeerLoadCache()
