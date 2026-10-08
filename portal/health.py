"""
Is each server up, and is there a saved live login? For the Studio's top bar.

Checked from the computer that hosts the Studio - which is where the runs
happen, so it is the answer that matters ("Local dev" means THAT computer's
localhost). Results are kept for a minute so a room full of open tabs does not
hammer the APIs.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from config import settings
from core import auth, backend

TTL_SECONDS = 60
_lock = threading.Lock()
_last: tuple[float, dict] | None = None


def _port_open(url: str, timeout: float = 2.0) -> bool:
    parts = urlparse(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check(name: str) -> dict:
    target = settings.TARGETS[name]
    started = time.monotonic()
    if target.is_local and not _port_open(target.base_url):
        return {"state": "down", "detail": f"nothing on {target.base_url} - start "
                f"'ng serve' in the Saarthi folder", "ms": 0}
    result = backend.check(target.api_url)
    ms = round((time.monotonic() - started) * 1000)
    if result:
        return {"state": "up", "detail": f"API {result.detail}", "ms": ms}
    return {"state": "down", "detail": result.detail.splitlines()[0][:160], "ms": ms}


def servers(force: bool = False) -> dict:
    global _last
    with _lock:
        if not force and _last and time.time() - _last[0] < TTL_SECONDS:
            return _last[1]
    with ThreadPoolExecutor(max_workers=3) as pool:
        found = dict(zip(settings.TARGETS, pool.map(_check, settings.TARGETS)))
    found["live_login"] = live_login()
    found["checked"] = time.time()
    with _lock:
        _last = (time.time(), found)
    return found


def live_login() -> dict:
    """What the saved live session file says - not proof the server still
    accepts it, which only a run can tell."""
    path = auth.LIVE_SESSION_FILE
    if not path.exists():
        return {"saved": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"saved": False}
    token = next((c for c in state.get("cookies") or []
                  if c.get("name") == ".mob-token-w"), None)
    if not token:
        return {"saved": False}
    expires = token.get("expires") or -1
    return {"saved": True, "saved_at": path.stat().st_mtime,
            "expires": expires if expires and expires > 0 else None,
            "expired": bool(expires and 0 < expires < time.time())}
