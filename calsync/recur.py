"""Recurrence rule translation: Graph patternedRecurrence <-> iCalendar RRULE (used by Google).

Every Graph rule can be translated to RRULE; RRULE supports only a common subset (complex rules
are not synced, and raise an alert):
  FREQ = DAILY / WEEKLY / MONTHLY / YEARLY; INTERVAL; BYDAY (may carry an ordinal); BYMONTHDAY (single);
  BYMONTH (single); BYSETPOS (single, together with BYDAY); COUNT / UNTIL; WKST.
EXDATE (single-occurrence cancellation) is not handled here; the per-occurrence layer (instances) syncs it.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ICAL_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
INDEX = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
INDEX_BACK = {v: k for k, v in INDEX.items()}


class Unsupported(Exception):
    pass


def _ical_day(graph_day):
    return ICAL_DAYS[DAYS.index(graph_day.lower())]


def _graph_day(ical_day):
    return DAYS[ICAL_DAYS.index(ical_day)]


def graph_to_rrule(rec, tz="Asia/Shanghai", all_day=False, start_time="00:00"):
    """Graph recurrence -> 'RRULE:...' (single line)."""
    p, r = rec["pattern"], rec["range"]
    t = p["type"]
    parts = []
    interval = int(p.get("interval") or 1)
    days = [_ical_day(d) for d in (p.get("daysOfWeek") or [])]
    idx = INDEX.get((p.get("index") or "first").lower(), 1)
    if t == "daily":
        parts.append("FREQ=DAILY")
    elif t == "weekly":
        parts += ["FREQ=WEEKLY", "BYDAY=" + ",".join(days)]
        if p.get("firstDayOfWeek") and p["firstDayOfWeek"].lower() != "sunday":
            parts.append("WKST=" + _ical_day(p["firstDayOfWeek"]))
    elif t == "absoluteMonthly":
        parts += ["FREQ=MONTHLY", f"BYMONTHDAY={int(p['dayOfMonth'])}"]
    elif t == "relativeMonthly":
        parts.append("FREQ=MONTHLY")
        parts += [f"BYDAY={idx}{days[0]}"] if len(days) == 1 else ["BYDAY=" + ",".join(days), f"BYSETPOS={idx}"]
    elif t == "absoluteYearly":
        parts += ["FREQ=YEARLY", f"BYMONTH={int(p['month'])}", f"BYMONTHDAY={int(p['dayOfMonth'])}"]
    elif t == "relativeYearly":
        parts += ["FREQ=YEARLY", f"BYMONTH={int(p['month'])}"]
        parts += [f"BYDAY={idx}{days[0]}"] if len(days) == 1 else ["BYDAY=" + ",".join(days), f"BYSETPOS={idx}"]
    else:
        raise Unsupported(f"graph pattern {t}")
    if interval != 1:
        parts.insert(1, f"INTERVAL={interval}")

    rt = r.get("type")
    if rt == "numbered":
        parts.append(f"COUNT={int(r['numberOfOccurrences'])}")
    elif rt == "endDate":
        end = date.fromisoformat(r["endDate"][:10])
        if all_day:
            parts.append(f"UNTIL={end:%Y%m%d}")
        else:
            # last moment of the day (local) -> UTC
            local = datetime.combine(end, datetime.strptime("23:59:59", "%H:%M:%S").time(), ZoneInfo(tz))
            parts.append("UNTIL=" + local.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ"))
    return "RRULE:" + ";".join(parts)


def _parse(rrule):
    if not rrule.startswith("RRULE:"):
        raise Unsupported(rrule.split(":")[0])
    out = {}
    for kv in rrule[6:].split(";"):
        if "=" not in kv:
            raise Unsupported(kv)
        k, v = kv.split("=", 1)
        out[k.upper()] = v.upper()
    return out


def rrule_to_graph(lines, start, tz="Asia/Shanghai", windows_tz="China Standard Time"):
    """Google recurrence list -> Graph recurrence. start: local date / datetime of the series' first occurrence."""
    rules = [l for l in lines if not l.startswith(("EXDATE", "RDATE"))]
    if any(l.startswith("RDATE") for l in lines):
        raise Unsupported("RDATE")
    if len(rules) != 1:
        raise Unsupported(f"{len(rules)} rules")
    rr = _parse(rules[0])
    allowed = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "COUNT", "UNTIL", "WKST"}
    extra = set(rr) - allowed
    if extra:
        raise Unsupported(",".join(sorted(extra)))
    for k in ("BYMONTHDAY", "BYMONTH", "BYSETPOS"):
        if "," in rr.get(k, ""):
            raise Unsupported(f"multiple {k}")

    start_date = start if not isinstance(start, datetime) else start.date()
    freq = rr.get("FREQ")
    pattern = {"interval": int(rr.get("INTERVAL", "1")), "firstDayOfWeek": "sunday"}
    if "WKST" in rr:
        pattern["firstDayOfWeek"] = _graph_day(rr["WKST"])

    byday = [d for d in rr.get("BYDAY", "").split(",") if d]
    ordinals = []
    plain = []
    for d in byday:
        num, day = d[:-2], d[-2:]
        if day not in ICAL_DAYS:
            raise Unsupported(f"BYDAY {d}")
        if num:
            ordinals.append((int(num), day))
        else:
            plain.append(day)
    setpos = int(rr["BYSETPOS"]) if "BYSETPOS" in rr else None

    def relative():
        if ordinals and not plain and len({n for n, _ in ordinals}) == 1 and setpos is None:
            n = ordinals[0][0]
            days = [d for _, d in ordinals]
        elif plain and not ordinals and setpos is not None:
            n, days = setpos, plain
        else:
            raise Unsupported(f"BYDAY {rr.get('BYDAY')}")
        if n not in INDEX_BACK:
            raise Unsupported(f"position {n}")
        pattern.update(index=INDEX_BACK[n], daysOfWeek=[_graph_day(d) for d in days])

    if freq == "DAILY":
        if byday or "BYMONTHDAY" in rr or "BYMONTH" in rr:
            raise Unsupported("DAILY with BY*")
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        if ordinals or setpos or "BYMONTHDAY" in rr or "BYMONTH" in rr:
            raise Unsupported("WEEKLY with BY*")
        pattern["type"] = "weekly"
        pattern["daysOfWeek"] = [_graph_day(d) for d in plain] or [DAYS[start_date.weekday()]]
    elif freq == "MONTHLY":
        if "BYMONTH" in rr:
            raise Unsupported("MONTHLY with BYMONTH")
        if byday:
            if "BYMONTHDAY" in rr:
                raise Unsupported("BYDAY + BYMONTHDAY")
            pattern["type"] = "relativeMonthly"
            relative()
        else:
            dom = int(rr.get("BYMONTHDAY", start_date.day))
            if dom < 1:
                raise Unsupported("negative BYMONTHDAY")
            pattern.update(type="absoluteMonthly", dayOfMonth=dom)
    elif freq == "YEARLY":
        month = int(rr.get("BYMONTH", start_date.month))
        if byday:
            if "BYMONTHDAY" in rr:
                raise Unsupported("BYDAY + BYMONTHDAY")
            pattern.update(type="relativeYearly", month=month)
            relative()
        else:
            dom = int(rr.get("BYMONTHDAY", start_date.day))
            if dom < 1:
                raise Unsupported("negative BYMONTHDAY")
            pattern.update(type="absoluteYearly", month=month, dayOfMonth=dom)
    else:
        raise Unsupported(f"FREQ={freq}")

    rng = {"startDate": start_date.isoformat(), "recurrenceTimeZone": windows_tz}
    if "COUNT" in rr:
        rng.update(type="numbered", numberOfOccurrences=int(rr["COUNT"]))
    elif "UNTIL" in rr:
        u = rr["UNTIL"]
        if "T" in u:
            utc = datetime.strptime(u.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC"))
            end = utc.astimezone(ZoneInfo(tz)).date()
        else:
            end = datetime.strptime(u, "%Y%m%d").date()
        rng.update(type="endDate", endDate=end.isoformat())
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


def normalize_google(lines, start, tz="Asia/Shanghai", all_day=False):
    """Round-trip a Google rule through Graph and back to get a form comparable with the Outlook side.
    Raises Unsupported if the rule is not supported."""
    return graph_to_rrule(rrule_to_graph(lines, start, tz), tz, all_day)
