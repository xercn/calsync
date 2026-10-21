"""Failure alerts: POST {level, title, body} with a bearer key to an HTTP push endpoint ([notify] url / key_file).

Push only when a failure persists: a full-sync run that fails once and succeeds on the next run sends
nothing. "Calendar sync failing" goes out on the second consecutive failed run (~20 min at the 10-min interval), again
every 24 hours while it keeps failing, and "recovered" once when it recovers (only if a failing push was sent).
Only full-sync runs report here; dry runs, --only runs and poll checks without a sync do not count.
State is kept in state_dir/status.json: fails = consecutive failed runs, failing = a failing push was sent.
Push messages are always in English.
"""
import json
import os
import time

import requests

from .http import UA

REPEAT = 24 * 3600
FAILS_BEFORE_PUSH = 2


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
    """errors: list of error descriptions from this full-sync run (empty = everything succeeded)."""
    now = now or time.time()
    path = os.path.join(cfg.state_dir, "status.json")
    st = _load(path)
    if errors:
        fails = st.get("fails", 1 if st.get("failing") else 0) + 1
        st.update(fails=fails, since=st.get("since", now) if fails > 1 else now, last_error=errors[0])
        if fails >= FAILS_BEFORE_PUSH and (not st.get("failing") or now - st.get("notified", 0) >= REPEAT):
            if send(cfg, "normal", "Calendar sync failing", "\n".join(errors), log):
                st.update(failing=True, notified=now)
    else:
        if st.get("failing"):
            mins = int((now - st.get("since", now)) / 60)
            send(cfg, "normal", "Calendar sync recovered", f"Back to normal after {mins} min.", log)
        st = {"failing": False, "fails": 0}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)
    return st
