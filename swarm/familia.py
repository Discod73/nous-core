"""NOUS Swarm — Kin: krypteret privat swarm-gruppe med pre-shared keys."""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

FAMILIA_CONFIG = Path("/mnt/nous-data/familia_config.json")
_lock = threading.Lock()


def _load() -> dict:
    if FAMILIA_CONFIG.exists():
        try:
            return json.loads(FAMILIA_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"groups": []}


def _save(data: dict) -> None:
    FAMILIA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAMILIA_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FAMILIA_CONFIG)


def generate_group_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_fact(fact_text: str, psk: str) -> str:
    return Fernet(psk.encode()).encrypt(fact_text.encode()).decode()


def decrypt_fact(encrypted_text: str, psk: str) -> str:
    return Fernet(psk.encode()).decrypt(encrypted_text.encode()).decode()


def create_group(name: str, group_type: str, allowed_wings: list[str]) -> dict:
    with _lock:
        data = _load()
        group = {
            "group_id":     str(uuid.uuid4()),
            "name":         name,
            "type":         group_type,
            "psk":          generate_group_key(),
            "members":      [],
            "allowed_wings": allowed_wings,
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }
        data["groups"].append(group)
        _save(data)
        return group


def add_member_to_group(group_id: str, node_id: str, label: str) -> dict | None:
    with _lock:
        data = _load()
        for group in data["groups"]:
            if group["group_id"] == group_id:
                if not any(m["node_id"] == node_id for m in group["members"]):
                    group["members"].append({"node_id": node_id, "label": label})
                    _save(data)
                return group
        return None


def delete_group(group_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["groups"])
        data["groups"] = [g for g in data["groups"] if g["group_id"] != group_id]
        if len(data["groups"]) < before:
            _save(data)
            return True
        return False


def get_group_by_id(group_id: str) -> dict | None:
    return next((g for g in _load()["groups"] if g["group_id"] == group_id), None)


def get_group_for_peer(node_id: str) -> dict | None:
    for group in _load()["groups"]:
        if any(m["node_id"] == node_id for m in group["members"]):
            return group
    return None


def get_all_groups() -> list[dict]:
    return _load()["groups"]


def try_decrypt(encrypted_text: str, psk: str) -> str | None:
    try:
        return decrypt_fact(encrypted_text, psk)
    except (InvalidToken, Exception):
        return None
