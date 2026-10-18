"""iCalendar (.ics) parsing: shared by subscription links and iCloud CalDAV.

Outputs "pseudo-Outlook events" shaped like Graph calendarView (times local to cfg.timezone),
so one-way sync (sync.plan / render) and writing to Outlook can use them directly.
Recurring events are expanded inside the time window (python3-dateutil, usually installed with the distro); EXDATE and
RECURRENCE-ID single-occurrence changes are supported; STATUS:CANCELLED is skipped.
"""
import hashlib
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")
WINDOWS_TZ = {  # ics exported by Outlook / Exchange sometimes uses Windows timezone names
    "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "Singapore Standard Time": "Asia/Singapore", "UTC": "UTC", "GMT Standard Time": "Europe/London",
    "Pacific Standard Time": "America/Los_Angeles", "Eastern Standard Time": "America/New_York",
}


class Unsupported(Exception):
    pass


def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n")).split("\n")


def parse_line(line):
    """'DTSTART;TZID=Asia/Shanghai:20261020T090000' -> ('DTSTART', {'TZID': 'Asia/Shanghai'}, '2026...')."""
    quoted, cut = False, None
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    if cut is None:
        return None
    head, value = line[:cut], line[cut + 1:]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(s):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def components(text):
    """VEVENTs below the top level (properties inside VTIMEZONE / VALARM are ignored).
    Returns [{NAME: [(params, value), ...]}]."""
    events, stack, cur = [], [], None
    for line in unfold(text):
        if not line:
            continue
        if line.startswith("BEGIN:"):
            stack.append(line[6:].strip().upper())
            if stack[-1] == "VEVENT" and len(stack) >= 1:
                cur = {}
            continue
        if line.startswith("END:"):
            name = stack.pop() if stack else ""
            if name == "VEVENT" and cur is not None:
                events.append(cur)
                cur = None
            continue
        if cur is not None and stack and stack[-1] == "VEVENT":
            p = parse_line(line)
            if p:
                cur.setdefault(p[0], []).append((p[1], p[2]))
    return events


def _zone(tzid, default_tz):
    if not tzid:
        return ZoneInfo(default_tz)
    tzid = WINDOWS_TZ.get(tzid, tzid)
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(default_tz)


def parse_dt(params, value, default_tz):
    """-> (aware datetime or date, is_date)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return dt.replace(tzinfo=UTC), False
    return dt.replace(tzinfo=_zone(params.get("TZID"), default_tz)), False


def parse_duration(s):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s.strip())
    if not m:
        raise Unsupported(f"DURATION {s}")
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, se = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=se)


def _first(ev, name):
    v = ev.get(name)
    return v[0] if v else (None, None)


def _text(ev, name):
    return unescape(_first(ev, name)[1] or "").strip()


def _key(start):
    """Pairing key of a single occurrence: YYYYMMDD for all-day, otherwise UTC YYYYMMDDTHHMM."""
    if isinstance(start, datetime):
        return start.astimezone(UTC).strftime("%Y%m%dT%H%M")
    return start.strftime("%Y%m%d")


def _pseudo(ev, uid, start, end, is_date, tz, suffix=""):
    """-> Graph-shaped event (times local to tz)."""
    if is_date:
        s, e = f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T00:00:00"
    else:
        z = ZoneInfo(tz)
        s = start.astimezone(z).strftime("%Y-%m-%dT%H:%M:%S")
        e = end.astimezone(z).strftime("%Y-%m-%dT%H:%M:%S")
    url = _text(ev, "URL")
    desc = _text(ev, "DESCRIPTION")
    if url and url not in desc:
        desc = f"{desc}\n\n{url}".strip()
    src_id = uid + (f"|{suffix}" if suffix else "")
    return {
        "id": hashlib.sha1(src_id.encode()).hexdigest(),   # fixed length, avoids overlong UIDs
        "type": "singleInstance",
        "subject": _text(ev, "SUMMARY"),
        "body": {"content": desc},
        "location": {"displayName": _text(ev, "LOCATION")},
        "start": {"dateTime": s + ".0000000"},
        "end": {"dateTime": e + ".0000000"},
        "isAllDay": is_date,
        "showAs": "free" if _text(ev, "TRANSP").upper() == "TRANSPARENT" else "busy",
        "sensitivity": "private" if _text(ev, "CLASS").upper() in ("PRIVATE", "CONFIDENTIAL") else "normal",
        "isOrganizer": True,
        "isReminderOn": False,
    }


def _span(ev, tz):
    sp, sv = _first(ev, "DTSTART")
    if sv is None:
        return None
    start, is_date = parse_dt(sp, sv, tz)
    ep, evv = _first(ev, "DTEND")
    if evv is not None:
        end, _ = parse_dt(ep, evv, tz)
    elif _first(ev, "DURATION")[1]:
        end = start + parse_duration(_first(ev, "DURATION")[1])
    else:
        end = start + timedelta(days=1) if is_date else start
    if is_date and isinstance(end, datetime):
        end = end.date()
    return start, end, is_date


def _overlaps(start, end, is_date, w0, w1, tz):
    if is_date:
        z = ZoneInfo(tz)
        start = datetime.combine(start, time(), z)
        end = datetime.combine(end, time(), z)
    return start < w1 and (end > w0 or start >= w0)


def events(text, w0, w1, tz="Asia/Shanghai"):
    """ics text -> list of pseudo-Outlook events inside the time window [w0, w1)."""
    comps = components(text)
    overrides = {}
    masters = []
    for ev in comps:
        uid = _text(ev, "UID") or hashlib.sha1(repr(sorted(ev.items())).encode()).hexdigest()
        rp, rv = _first(ev, "RECURRENCE-ID")
        if rv is not None:
            orig, _ = parse_dt(rp, rv, tz)
            overrides[(uid, _key(orig))] = ev
        else:
            masters.append((uid, ev))

    out = []
    for uid, ev in masters:
        if _text(ev, "STATUS").upper() == "CANCELLED":
            continue
        sp = _span(ev, tz)
        if sp is None:
            continue
        start, end, is_date = sp
        rrules = [v for _, v in ev.get("RRULE", [])]
        if not rrules:
            if _overlaps(start, end, is_date, w0, w1, tz):
                out.append(_pseudo(ev, uid, start, end, is_date, tz))
            continue
        out += _expand(uid, ev, start, end, is_date, rrules, overrides, w0, w1, tz)
    return out


def _expand(uid, ev, start, end, is_date, rrules, overrides, w0, w1, tz):
    from dateutil.rrule import rruleset, rrulestr

    dur = end - start
    base = datetime.combine(start, time()) if is_date else start
    rs = rruleset()
    for r in rrules:
        if is_date:
            r = re.sub(r"UNTIL=(\d{8})(?!T)", r"UNTIL=\1T000000", r)
        else:
            r = re.sub(r"UNTIL=(\d{8})(?!T)", r"UNTIL=\1T235959Z", r)
        rs.rrule(rrulestr(r, dtstart=base, ignoretz=is_date))
    for params, value in ev.get("EXDATE", []):
        for v in value.split(","):
            x, x_is_date = parse_dt(params, v, tz)
            rs.exdate(datetime.combine(x, time()) if x_is_date else (x if not is_date else x.replace(tzinfo=None)))
    z = ZoneInfo(tz)
    lo = (w0 - dur - timedelta(days=1)).astimezone(z)
    hi = w1.astimezone(z)
    if is_date:
        lo, hi = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
    out = []
    for occ in rs.between(lo, hi, inc=True):
        s = occ.date() if is_date else occ
        e = s + dur
        k = _key(s)
        ov = overrides.get((uid, k))
        if ov is not None:
            if _text(ov, "STATUS").upper() == "CANCELLED":
                continue
            sp = _span(ov, tz)
            if sp is None:
                continue
            os_, oe, od = sp
            if _overlaps(os_, oe, od, w0, w1, tz):
                out.append(_pseudo(ov, uid, os_, oe, od, tz, suffix=k))
            continue
        if _overlaps(s, e, is_date, w0, w1, tz):
            out.append(_pseudo(ev, uid, s, e, is_date, tz, suffix=k))
    return out


def count_events(text):
    return len(components(text))
