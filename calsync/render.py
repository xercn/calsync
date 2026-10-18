"""Outlook (Graph) event -> Google event body. Pure functions, no network access."""
import base64
import hashlib
import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# bump by 1 whenever the rendering rules change: every event hash changes with it, so the next run rewrites everything
RENDER_VERSION = "1"
DESC_LIMIT = 8000

RESPONSE = {
    "accepted": "accepted", "tentativelyAccepted": "tentative", "declined": "declined",
    "organizer": "organizer", "notResponded": "no response", "none": "no response",
}


def map_id(source_calendar_id, target):
    return hashlib.sha1(f"{source_calendar_id}|{target}".encode()).hexdigest()[:16]


def event_id(mid, src_id):
    """Google event ID: lowercase base32hex (a-v, 0-9); same mapping + same Outlook event always give the same ID."""
    digest = hashlib.sha1(f"{mid}|{src_id}".encode()).digest()
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def _trim(dt_str):
    """Graph '2026-10-20T09:00:00.0000000' -> '2026-10-20T09:00:00'."""
    return dt_str.split(".")[0]


def span(ev, tz):
    """Start and end of an event (aware datetimes), used to decide whether it falls inside the time window."""
    zone = ZoneInfo(tz)
    if ev.get("isAllDay"):
        s = date.fromisoformat(ev["start"]["dateTime"][:10])
        e = date.fromisoformat(ev["end"]["dateTime"][:10])
        return datetime.combine(s, time(), zone), datetime.combine(e, time(), zone)
    s = datetime.fromisoformat(_trim(ev["start"]["dateTime"])).replace(tzinfo=zone)
    e = datetime.fromisoformat(_trim(ev["end"]["dateTime"])).replace(tzinfo=zone)
    return s, e


def overlaps(ev, tz, win_start, win_end):
    s, e = span(ev, tz)
    return s < win_end and (e > win_start or s >= win_start)


def _person(p):
    addr = (p or {}).get("emailAddress") or {}
    name, email = addr.get("name", ""), addr.get("address", "")
    if name and email and name != email:
        return f"{name} <{email}>"
    return name or email


def description(ev):
    parts = []
    text = ((ev.get("body") or {}).get("content") or "").strip()
    if text:
        parts.append(text)

    meta = []
    org = _person(ev.get("organizer"))
    if org:
        meta.append(f"Organizer: {org}")
    attendees = ev.get("attendees") or []
    if attendees:
        meta.append("Attendees:")
        for a in attendees:
            status = RESPONSE.get(((a.get("status") or {}).get("response")) or "none", "no response")
            kind = a.get("type", "required")
            extra = "" if kind == "required" else f", {kind}"
            meta.append(f"  - {_person(a)} ({status}{extra})")
    join = ((ev.get("onlineMeeting") or {}).get("joinUrl")) if ev.get("isOnlineMeeting") else None
    if join:
        meta.append(f"Online meeting: {join}")
    if ev.get("webLink"):
        meta.append(f"Outlook: {ev['webLink']}")
    if meta:
        parts.append("———\n" + "\n".join(meta))

    desc = "\n\n".join(parts)
    if len(desc) > DESC_LIMIT:
        desc = desc[:DESC_LIMIT - 12] + "\n[truncated]"
    return desc


def _times(ev, tz):
    if ev.get("isAllDay"):
        return ({"date": ev["start"]["dateTime"][:10]}, {"date": ev["end"]["dateTime"][:10]})
    return ({"dateTime": _trim(ev["start"]["dateTime"]), "timeZone": tz},
            {"dateTime": _trim(ev["end"]["dateTime"]), "timeZone": tz})


def _reminders(ev, mode):
    if mode == "default":
        return {"useDefault": True}
    if mode == "outlook" and ev.get("isReminderOn"):
        minutes = max(0, min(int(ev.get("reminderMinutesBeforeStart") or 0), 40320))
        return {"useDefault": False, "overrides": [{"method": "popup", "minutes": minutes}]}
    return {"useDefault": False, "overrides": []}


def content_hash(body):
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{RENDER_VERSION}|{blob}".encode()).hexdigest()[:20]


def render(ev, *, mid, prefix="", tz="Asia/Shanghai", reminders="outlook"):
    """Return the Google event body (including the fixed ID and the calsync marker).

    Attendees are written only into the description, not into Google's attendees field -- doing so would make Google
send them invitations.
    """
    start, end = _times(ev, tz)
    body = {
        "summary": prefix + (ev.get("subject") or "(no title)"),
        "location": ((ev.get("location") or {}).get("displayName")) or "",
        "description": description(ev),
        "start": start,
        "end": end,
        "status": "confirmed",
        "transparency": "transparent" if ev.get("showAs") in ("free", "workingElsewhere") or ev.get("isCancelled") else "opaque",
        "visibility": "private" if ev.get("sensitivity") in ("private", "confidential") else "default",
        "reminders": _reminders(ev, reminders),
    }
    if ev.get("webLink", "").startswith("https://"):
        body["source"] = {"title": "Outlook", "url": ev["webLink"]}
    h = content_hash(body)
    body["id"] = event_id(mid, ev["id"])
    body["extendedProperties"] = {"private": {
        "calsync": "1", "calsync_map": mid, "calsync_src": ev["id"][:1000], "calsync_hash": h,
    }}
    return body
