"""Failure alerts: POST {level, title, body} with a bearer key to an HTTP push endpoint ([notify] url / key_file).

Push only on state changes: once on OK -> failing, again every 24 hours while it keeps failing, and once on recovery.
State is kept in state_dir/status.json. Push messages are always in English.
"""
import json
import os
import time

import requests

from .http import UA

REPEAT = 24 * 3600


def send(cfg, level, title, body, log=print):
    if not cfg.notify_url or not cfg.notify_key_file:
        log(f"notify skipped (no url / key file): {title}")
        return False
    try:
        with open(cfg.notify_key_file) as f:
            key = f.read().strip()
        r = requests.post(cfg.notify_url, json={"level": level, "title": title, "body": body},
                          headers={"Authorization": f"Bearer {key}", "User-Agent": UA}, timeout=30)
    except (OSError, requests.RequestException) as e:
        log(f"notify failed: {e.__class__.__name__}")
        return False
    if r.status_code != 200:
        log(f"notify failed: HTTP {r.status_code}")
        return False
    return True


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"failing": False}


def report(cfg, errors, log=print, now=None):
    """errors: list of error descriptions from this run (empty = everything succeeded)."""
    now = now or time.time()
    path = os.path.join(cfg.state_dir, "status.json")
    st = _load(path)
    if errors:
        due = not st.get("failing") or now - st.get("notified", 0) >= REPEAT
        st.update(failing=True, since=st.get("since") if st.get("failing") else now, last_error=errors[0])
        if due:
            body = "\n".join(errors)
            if send(cfg, "normal", "Calendar sync failing", body, log):
                st["notified"] = now
    elif st.get("failing"):
        mins = int((now - st.get("since", now)) / 60)
        send(cfg, "normal", "Calendar sync recovered", f"Back to normal after {mins} min.", log)
        st = {"failing": False}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)
    return st
