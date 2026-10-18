"""Read-only source (iCloud calendar / subscription link) → same-named Outlook calendar + Google target, one-way

- Outlook: calsync creates a calendar with the same name (or outlook_name) itself and records its ID in state.db; if
  an existing calendar with that name was not created by calsync, report an error and do not touch it.
  This calendar is dedicated: all events in it are managed by calsync, and manually added ones get cleared. The
  pairing is stored in state.db table mirror_o.
- Google: same as a one-way mapping (sync.plan, with tags and fixed IDs); can be a dedicated calendar or merged into
  the main calendar.
- Source became entirely empty or could not be fetched: leave the targets alone this round and raise an alert
 .
"""
import hashlib
import requests

from . import canon, ics, render, sync
from .http import request


class EmptySource(Exception):
    pass


class _M:  # mapping fields that sync.plan needs
    prefix = ""
    reminders = "outlook"


def fetch(mir, icloud, wide, tz):
    """→ pseudo Outlook events in the wide window."""
    if mir.url_file:
        with open(mir.url_file) as f:
            url = f.read().strip()
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        text = request(requests.Session(), "GET", url).text
        if ics.count_events(text) == 0:
            raise EmptySource("subscription feed has no events")
        return ics.events(text, wide[0], wide[1], tz)
    if icloud is None:
        raise LookupError("no [icloud] credentials configured")
    cals = [c for c in icloud.calendars() if c["name"] == mir.icloud]
    if len(cals) != 1:
        raise LookupError(f"iCloud calendar {'not found' if not cals else 'ambiguous'}: {mir.icloud!r}")
    out = []
    for text in icloud.events_ics(cals[0]["url"], wide[0], wide[1]):
        out += ics.events(text, wide[0], wide[1], tz)
    return out


def map_id(mir, target):
    return render.map_id(f"mirror:{mir.name}", target)


def ensure_outlook_calendar(graph, name, state, mid, dry_run):
    """Only use calendars that calsync created itself (ID recorded in state.db). An existing calendar with that name
    not created by calsync → error; never empty and rebuild it."""
    mine = state.mirror_cal(mid)
    hits = [c for c in graph.calendars() if c["name"] == name]
    for c in hits:
        if c["id"] == mine:
            return c["id"]
    if hits:
        raise LookupError(f"Outlook already has a calendar named {name!r} that calsync did not create; "
                          f"set outlook_name to a different name")
    if dry_run:
        return None
    if mine:                      # the recorded one was deleted: rebuild, old pairings become void
        state.clear_mirror(mid)
    cal = graph.create_calendar(name)
    state.put_mirror_cal(mid, cal["id"])
    return cal["id"]


def plan_outlook(src, existing, rows, narrow, tz):
    """→ (creates[ev], updates[(o_id, ev)], deletes[(src_id, o_id)], wipe[o_id])。
    existing: {o_id: ev} in the narrow window of the Outlook calendar; rows: {src_id: (o_id, hash)}."""
    creates, updates, deletes, wipe = [], [], [], []
    if not rows and existing:     # state is gone (or someone manually added things): clear and rebuild
        wipe = list(existing)
        existing = {}
    seen = set()
    for ev in src:
        sid = ev["id"]
        if sid in seen or not render.overlaps(ev, tz, *narrow):
            continue
        seen.add(sid)
        h = canon.digest(canon.from_outlook(ev, tz))
        row = rows.get(sid)
        if row is None or row[0] not in existing:   # new, or deleted manually in Outlook: put it back
            creates.append(ev)
        elif row[1] != h:
            updates.append((row[0], ev))
    for sid, (o_id, _) in rows.items():
        if sid not in seen and o_id in existing:
            deletes.append((sid, o_id))
    return creates, updates, deletes, wipe


def run(*, mir, cfg, graph, gcal, icloud, state, narrow, wide, dry_run, force, verbose, log, rows=None, caps=None):
    """Run one mirror and return the error list. rows: one result row appended per target (config page dry-run / last
    sync); see sync.check_cap for caps."""
    errors = []
    rows = [] if rows is None else rows
    tz, otz = cfg.timezone, cfg.outlook_timezone
    targets = list(mir.google) + (["outlook"] if mir.outlook else [])
    try:
        src = fetch(mir, icloud, wide, tz)
    except EmptySource as e:
        msg = f"{e}; targets left unchanged"
        rows += [{"kind": "mirror", "name": mir.name, "target": t, "error": msg} for t in targets]
        return [f"{mir.name}: {msg}"]
    except Exception as e:
        msg = f"{e.__class__.__name__}: {str(e)[:200]}"
        rows += [{"kind": "mirror", "name": mir.name, "target": t, "error": msg} for t in targets]
        return [f"{mir.name}: {msg}"]

    # ---- Google target
    for target in mir.google:
        mid = map_id(mir, target)
        row = {"kind": "mirror", "name": mir.name, "target": target}
        rows.append(row)
        try:
            dst = gcal.list_marked(target, map_id=mid, time_min=narrow[0], time_max=narrow[1])
            if not src and dst:
                raise EmptySource(f"source returned no events but {target} has {len(dst)}")
            p = sync.plan(src, dst, mid=mid, mapping=_M, cfg=cfg, narrow=narrow)
            row.update(create=len(p.creates), update=len(p.updates), delete=len(p.deletes), existing=p.existing)
            sync.check_cap(caps, ("mirror", mir.name, target), len(p.deletes))
            reason = sync.check_guard(p, cfg)
            if reason and not force:
                raise sync.GuardTripped(reason)
            log(f"{mir.name} → {target}: {p.summary()}{' [dry-run]' if dry_run else ''}")
            if not dry_run:
                sync.apply(p, gcal, target)
        except Exception as e:
            row["error"] = f"{e.__class__.__name__}: {str(e)[:200]}"
            errors.append(f"{mir.name} → {target}: {row['error']}")

    # ---- same-named Outlook calendar
    if mir.outlook:
        mid = map_id(mir, "outlook")
        row = {"kind": "mirror", "name": mir.name, "target": "outlook"}
        rows.append(row)
        try:
            cal_id = ensure_outlook_calendar(graph, mir.outlook_name or mir.name, state, mid, dry_run)
            existing = {e["id"]: e for e in graph.calendar_view(cal_id, narrow[0], narrow[1], otz)} if cal_id else {}
            srows = state.mirror_rows(mid)
            if not src and existing:
                raise EmptySource(f"source returned no events but Outlook {mir.name!r} has {len(existing)}")
            creates, updates, deletes, wipe = plan_outlook(src, existing, srows, narrow, tz)
            n_del = len(deletes) + len(wipe)
            row.update(create=len(creates), update=len(updates), delete=n_del, existing=len(existing))
            sync.check_cap(caps, ("mirror", mir.name, "outlook"), n_del)
            if n_del > cfg.guard_min and n_del > max(len(srows), len(existing)) * cfg.guard_ratio and not force:
                raise sync.GuardTripped(f"would delete {n_del} Outlook events")
            log(f"{mir.name} → Outlook: +{len(creates)} ~{len(updates)} -{len(deletes)}"
                f"{f' (rebuild: wipe {len(wipe)})' if wipe else ''}{' [dry-run]' if dry_run else ''}")
            if verbose:
                for ev in creates:
                    log(f"    + {ev['start']['dateTime'][:16]} {ev['subject']}")
            if not dry_run:
                for o_id in wipe:
                    graph.delete_event(o_id, otz)
                if wipe:
                    state.clear_mirror(mid)
                for ev in creates:
                    body = canon.to_outlook(canon.from_outlook(ev, tz), canon.FIELDS, otz)
                    o = graph.create_event(cal_id, body, otz)
                    state.put_mirror(mid, ev["id"], o["id"], canon.digest(canon.from_outlook(ev, tz)))
                for o_id, ev in updates:
                    c = canon.from_outlook(ev, tz)
                    graph.patch_event(o_id, canon.to_outlook(c, canon.FIELDS, otz), otz)
                    state.put_mirror(mid, ev["id"], o_id, canon.digest(c))
                for sid, o_id in deletes:
                    graph.delete_event(o_id, otz)
                    state.drop_mirror(mid, sid)
        except Exception as e:
            row["error"] = f"{e.__class__.__name__}: {str(e)[:200]}"
            errors.append(f"{mir.name} → Outlook: {row['error']}")
    return errors


def url_fingerprint(path):
    """For recognizing links in logs (the link itself is not printed)."""
    with open(path) as f:
        return hashlib.sha256(f.read().strip().encode()).hexdigest()[:8]
