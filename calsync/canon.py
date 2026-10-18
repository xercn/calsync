"""The "canonical form" used by two-way sync: turns Outlook / Google events into one common set of fields,
used to tell which side changed and what changed.

Fields: title, location, body, all_day, start, end, busy, private; a series master also has rule.
start / end: 'YYYY-MM-DD' for all-day events, otherwise 'YYYY-MM-DDTHH:MM' in the local timezone (cfg.timezone).
Each side is compared only with the hash it stored last time (never across sides), so small formatting differences
between the two sides do not cause back-and-forth edits.
"""
import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import recur

META_MARK = "———\n"   # separator in render.description between body and metadata (organizer, attendees, links)
FIELDS = ("title", "location", "body", "all_day", "start", "end", "busy", "private")


def clean_text(s):
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def strip_meta(desc):
    """Strip the metadata calsync added from a Google description, keeping only the body."""
    desc = desc or ""
    if desc.startswith(META_MARK):
        return ""
    i = desc.find("\n\n" + META_MARK)
    return desc[:i] if i >= 0 else desc


def _local(dt_str, tz):
    """Google dateTime with an offset -> local 'YYYY-MM-DDTHH:MM'."""
    return datetime.fromisoformat(dt_str).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def from_outlook(ev, tz):
    """ev is a Graph event fetched with Prefer outlook.timezone = the local timezone."""
    all_day = bool(ev.get("isAllDay"))
    s, e = ev["start"]["dateTime"], ev["end"]["dateTime"]
    c = {
        "title": ev.get("subject") or "",
        "location": ((ev.get("location") or {}).get("displayName")) or "",
        "body": clean_text((ev.get("body") or {}).get("content")),
        "all_day": all_day,
        "start": s[:10] if all_day else s[:16],
        "end": e[:10] if all_day else e[:16],
        "busy": ev.get("showAs") not in ("free", "workingElsewhere"),
        "private": ev.get("sensitivity") in ("private", "confidential"),
    }
    if ev.get("type") == "seriesMaster" and ev.get("recurrence"):
        c["rule"] = recur.graph_to_rrule(ev["recurrence"], tz, all_day, c["start"][11:])
    return c


def from_google(ev, tz):
    """An unsupported recurrence rule raises recur.Unsupported."""
    all_day = "date" in ev["start"]
    c = {
        "title": ev.get("summary") or "",
        "location": ev.get("location") or "",
        "body": clean_text(strip_meta(ev.get("description"))),
        "all_day": all_day,
        "start": ev["start"]["date"] if all_day else _local(ev["start"]["dateTime"], tz),
        "end": ev["end"]["date"] if all_day else _local(ev["end"]["dateTime"], tz),
        "busy": ev.get("transparency") != "transparent",
        "private": ev.get("visibility") in ("private", "confidential"),
    }
    if ev.get("recurrence"):
        first = datetime.fromisoformat(c["start"]) if not all_day else datetime.fromisoformat(c["start"]).date()
        c["rule"] = recur.normalize_google(ev["recurrence"], first, tz, all_day)
    return c


def digest(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def diff(a, b):
    """Names of the fields whose values differ between a and b."""
    return [k for k in (*FIELDS, "rule") if a.get(k) != b.get(k)]


# ---- canonical form -> write body ----

def to_outlook(c, fields, outlook_tz, google_lines=None, tz="Asia/Shanghai"):
    """Generate only the fields listed in fields (for PATCH); when creating, fields = all of them."""
    body = {}
    if "title" in fields:
        body["subject"] = c["title"]
    if "location" in fields:
        body["location"] = {"displayName": c["location"]}
    if "body" in fields:
        body["body"] = {"contentType": "text", "content": c["body"]}
    if {"start", "end", "all_day"} & set(fields):
        if c["all_day"]:
            body["isAllDay"] = True
            body["start"] = {"dateTime": c["start"] + "T00:00:00", "timeZone": outlook_tz}
            body["end"] = {"dateTime": c["end"] + "T00:00:00", "timeZone": outlook_tz}
        else:
            body["isAllDay"] = False
            body["start"] = {"dateTime": c["start"] + ":00", "timeZone": outlook_tz}
            body["end"] = {"dateTime": c["end"] + ":00", "timeZone": outlook_tz}
    if "busy" in fields:
        body["showAs"] = "busy" if c["busy"] else "free"
    if "private" in fields:
        body["sensitivity"] = "private" if c["private"] else "normal"
    if "rule" in fields and google_lines is not None:
        start = datetime.fromisoformat(c["start"]).date()
        body["recurrence"] = recur.rrule_to_graph(google_lines, start, tz, outlook_tz)
    return body
