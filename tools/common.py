"""Shared by the helper scripts: where the data lives and how to reach the running tracker.

Resolves the data folder like tracker.py does: $WORKTRACKER_HOME, else <repo>/data.
"""
import json
import os
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("WORKTRACKER_HOME", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA / "tracker.db"
CONFIG_PATH = DATA / "config.json"


def user_config():
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def api(path, body=None):
    """Call the running tracker's local API; POST when a body is given."""
    port = user_config().get("dashboard_port", 8765)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-WT": "1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")
