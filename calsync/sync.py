"""Comparison and writes for one mapping (Outlook calendar -> Google calendar).

Time windows:
  narrow window = [now - past_days, now + future_days]; only this range is looked at on the Google side;
  wide window = the narrow window widened by 1 day at each end; Outlook is queried over this range.
Only events inside the narrow window are created; only events present in the narrow window but no longer in
Outlook's wide window are deleted.
This way events at the window edge are neither deleted by mistake nor re-created repeatedly. Old events that slide
out of the window are left untouched in Google.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import render

MARGIN = timedelta(days=1)


@dataclass
class Plan:
    creates: list = field(default_factory=list)   # [body]
    updates: list = field(default_factory=list)   # [body]
    deletes: list = field(default_factory=list)   # [event_id]
    unchanged: int = 0
    existing: int = 0

    def summary(self):
        return (f"+{len(self.creates)} ~{len(self.updates)} -{len(self.deletes)} "
                f"={self.unchanged} (google had {self.existing})")


class GuardTripped(Exception):
    pass


class CapExceeded(Exception):
    """Check after confirmation on the config page, before taking effect: more than CAP_SLACK deletions beyond
    the dry run the user reviewed."""


CAP_SLACK = 10


def check_cap(caps, key, n):
    """caps: {(kind, name, target): number of deletions in the dry run}; None = no check (normal sync)."""
    if caps is None:
        return
    seen = caps.get(key, 0)
    if n > seen + CAP_SLACK:
        raise CapExceeded(f"would delete {n} events but the confirmed preview showed {seen}; nothing applied, preview again")


def windows(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    narrow = (now - timedelta(days=cfg.past_days), now + timedelta(days=cfg.future_days))
    wide = (narrow[0] - MARGIN, narrow[1] + MARGIN)
    return narrow, wide


def plan(src_events, dst, *, mid, mapping, cfg, narrow):
    """src_events: Graph wide-window result; dst: this mapping's {event_id: private_props} in the Google narrow window."""
    p = Plan(existing=len(dst))
    seen = set()
    for ev in src_events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        body = render.render(ev, mid=mid, prefix=mapping.prefix, tz=cfg.timezone,
                             reminders=mapping.reminders)
        gid = body["id"]
        if gid in dst:
            if dst[gid].get("calsync_hash") == body["extendedProperties"]["private"]["calsync_hash"]:
                p.unchanged += 1
            else:
                p.updates.append(body)
        elif render.overlaps(ev, cfg.timezone, *narrow):
            p.creates.append(body)
    src_gids = {render.event_id(mid, sid) for sid in seen}
    p.deletes = sorted(g for g in dst if g not in src_gids)
    return p


def check_guard(p, cfg):
    """Block when too much would be deleted (Outlook returned empty by mistake, wrong calendar picked, etc.);
    return the reason, or None if all is fine."""
    n = len(p.deletes)
    if n > cfg.guard_min and n > p.existing * cfg.guard_ratio:
        return f"would delete {n} of {p.existing} events (limit: >{cfg.guard_min} and >{cfg.guard_ratio:.0%})"
    return None


def apply(p, gcal, target):
    for body in p.creates:
        gcal.insert(target, body)
    for body in p.updates:
        gcal.update(target, body["id"], body)
    for gid in p.deletes:
        gcal.delete(target, gid)
