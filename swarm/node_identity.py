"""
NOUS Swarm — Node Identity.
Genererer og persisterer et permanent UUID for denne node.
"""
import uuid
from pathlib import Path

NODE_ID_FILE = Path("/mnt/nous-data/node_id")


def get_node_id() -> str:
    if NODE_ID_FILE.exists():
        return NODE_ID_FILE.read_text().strip()
    node_id = str(uuid.uuid4())
    NODE_ID_FILE.write_text(node_id)
    return node_id
