"""End-to-end test (run on the server as the calsync user): touches only the calsync-test calendar pair in the test config.

  cd /opt/calsync && runuser -u calsync -- /usr/bin/python3 tools/e2e_test.py /etc/calsync/config-test.toml [--cleanup]

Create a batch of events on each side (single, all-day, series + single-instance changes / cancellations,
unsupported rules) -> several sync rounds -> verify -> change some on each side -> sync again -> verify.
--cleanup: only delete all events in the two test calendars and the test state; do not run the test.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from calsync import config  # noqa: E402
from calsync.gcal import GCal  # noqa: E402
from calsync.msgraph import GRAPH, Graph  # noqa: E402

CFG_PATH = sys.argv[1]
cfg = config.load(CFG_PATH)
m = cfg.mappings[0]
assert m.mode == "two-way" and "calsync-test" in m.source, "only for the calsync-test mapping"
graph = Graph(cfg.outlook_client_id, cfg.token_file)
gcal = GCal(cfg.google_key_file)
CAL = next(c["id"] for c in graph.calendars() if c["name"] == m.source)
TGT, OTZ, TZ = m.target, cfg.outlook_timezone, cfg.timezone
W0 = datetime(2026, 10, 25, tzinfo=timezone.utc)
W1 = datetime(2027, 3, 1, tzinfo=timezone.utc)
fails = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails.append(what)


def sync(tag):
    r = subprocess.run([sys.executable, "-m", "calsync", "--config", CFG_PATH, "run", "-v"],
                       capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."))
    out = (r.stdout + r.stderr).strip()
    print(f"--- sync {tag} (exit {r.returncode})\n{out}", flush=True)
    return out


def o_view():
    return graph.calendar_view(CAL, W0, W1, OTZ)


def g_view():
    params = {"singleEvents": "true", "timeMin": W0.isoformat(), "timeMax": W1.isoformat(), "maxResults": "2500"}
    return gcal._req("GET", gcal._cal(TGT), params=params).json().get("items", [])


def o_titles():
    return sorted((e["start"]["dateTime"][:16], e["subject"]) for e in o_view())


def g_titles():
    out = []
    for e in g_view():
        s = e["start"].get("date") or datetime.fromisoformat(e["start"]["dateTime"]).astimezone(
            __import__("zoneinfo").ZoneInfo(TZ)).strftime("%Y-%m-%dT%H:%M")
        out.append((s, e["summary"]))
    return sorted(out)


def cleanup():
    ids = {e.get("seriesMasterId") or e["id"] for e in o_view()}
    for i in ids:
        graph.delete_event(i, OTZ)
    params = {"singleEvents": "false", "maxResults": "2500"}
    for e in gcal._req("GET", gcal._cal(TGT), params=params).json().get("items", []):
        if e.get("status") != "cancelled" and not e.get("recurringEventId"):
            gcal.delete(TGT, e["id"])
    db = os.path.join(cfg.state_dir, "state.db")
    if os.path.exists(db):
        os.remove(db)
    print("cleaned: both test calendars empty, test state removed")


if "--cleanup" in sys.argv:
    cleanup()
    sys.exit(0)

cleanup()
o_body = lambda s, e, **kw: {"start": {"dateTime": s, "timeZone": OTZ}, "end": {"dateTime": e, "timeZone": OTZ}, **kw}

# ---- create data: Outlook ----
t1 = graph.create_event(CAL, o_body("2026-11-03T10:00:00", "2026-11-03T11:00:00", subject="T1 outlook single",
                                    body={"contentType": "text", "content": "body one"}), OTZ)
graph.create_event(CAL, o_body("2026-11-04T00:00:00", "2026-11-05T00:00:00", subject="T2 outlook allday", isAllDay=True), OTZ)
t3 = graph.create_event(CAL, o_body("2026-11-02T14:00:00", "2026-11-02T15:00:00", subject="T3 outlook weekly", recurrence={
    "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "wednesday"], "firstDayOfWeek": "sunday"},
    "range": {"type": "numbered", "startDate": "2026-11-02", "numberOfOccurrences": 6, "recurrenceTimeZone": OTZ}}), OTZ)
occ = {e["start"]["dateTime"][:10]: e for e in o_view() if e.get("seriesMasterId") == t3["id"]}
graph.patch_event(occ["2026-11-04"]["id"], {"subject": "T3 exception"}, OTZ)
graph.delete_event(occ["2026-11-09"]["id"], OTZ)

# ---- create data: Google ----
g_tz = lambda s, e: {"start": {"dateTime": s, "timeZone": TZ}, "end": {"dateTime": e, "timeZone": TZ}}
gcal.insert(TGT, {"summary": "T4 google single", **g_tz("2026-11-05T09:00:00", "2026-11-05T09:30:00")})
t5 = gcal.insert(TGT, {"summary": "T5 google monthly", "recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=3TU;COUNT=3"],
                       **g_tz("2026-11-17T16:00:00", "2026-11-17T17:00:00")})
inst = {i["originalStartTime"]["dateTime"][:10]: i for i in gcal.instances(TGT, t5["id"], W0, W1)}
gcal.patch(TGT, inst["2026-12-15"]["id"], {"summary": "T5 exception"})
gcal.delete(TGT, inst["2027-01-19"]["id"])
gcal.insert(TGT, {"summary": "T6 google unsupported", "recurrence": ["RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15;COUNT=4"],
                  **g_tz("2026-11-15T10:00:00", "2026-11-15T11:00:00")})

# ---- rounds 1-3: initial sync, single-instance layer, stable ----
out1 = sync(1)
check("+o=2 +g=3" in out1, "run 1 creates 3 in Google, 2 in Outlook")
check("T6 google unsupported" in out1 and "unsupported" in out1, "run 1 reports T6 as unsupported")
out2 = sync(2)
check(all(k in out2 for k in ("~oi=", "-oi=", "~gi=", "-gi=")), "run 2 syncs single-occurrence edits / cancels both ways")
out3 = sync(3)
check("no changes" in out3, "run 3 is stable (no ping-pong)")

O, G = o_titles(), g_titles()
want_o = [("2026-11-05T09:00", "T4 google single"), ("2026-11-17T16:00", "T5 google monthly"),
          ("2026-12-15T16:00", "T5 exception")]
for w in want_o:
    check(w in O, f"Outlook has {w}")
check(not any(s.startswith("2027-01-19") for s, _ in O), "Outlook: T5 2027-01-19 cancelled")
check(not any("T6" in t for _, t in O), "Outlook: T6 not synced")
want_g = [("2026-11-03T10:00", "T1 outlook single"), ("2026-11-04", "T2 outlook allday"),
          ("2026-11-02T14:00", "T3 outlook weekly"), ("2026-11-04T14:00", "T3 exception"),
          ("2026-11-11T14:00", "T3 outlook weekly"), ("2026-11-16T14:00", "T3 outlook weekly"),
          ("2026-11-18T14:00", "T3 outlook weekly")]
for w in want_g:
    check(w in G, f"Google has {w}")
check(not any(s.startswith("2026-11-09") for s, _ in G), "Google: T3 2026-11-09 cancelled")
g1 = next(e for e in g_view() if e["summary"] == "T1 outlook single")
check((g1.get("description") or "").startswith("body one"), "Google T1 description has the Outlook body")
check(not g1.get("attendees"), "Google T1 has no attendees (no invitations)")

# ---- rounds 4-6: change some on each side ----
gcal.patch(TGT, g1["id"], {"summary": "T1 edited in google"})
t4o = next(e for e in o_view() if e["subject"] == "T4 google single")
graph.patch_event(t4o["id"], o_body("2026-11-05T10:00:00", "2026-11-05T10:30:00"), OTZ)
g2 = next(e for e in g_view() if e["summary"] == "T2 outlook allday")
gcal.delete(TGT, g2["id"])
graph.patch_event(t3["id"], {"subject": "T3 renamed"}, OTZ)
t5o = next(e for e in o_view() if e["subject"] == "T5 google monthly")
graph.delete_event(t5o["seriesMasterId"], OTZ)
out4 = sync(4)
check(all(k in out4 for k in ("~o=1", "~g=2", "-o=1", "-g=1")), "run 4 applies edits both ways and deletes both ways")
out5 = sync(5)
check("~gi=1" in out5, "run 5 restores the T3 exception in Google after the series rename")
out6 = sync(6)
check("no changes" in out6, "run 6 is stable")
O, G = o_titles(), g_titles()
check(("2026-11-03T10:00", "T1 edited in google") in O, "Outlook T1 renamed from Google")
check(("2026-11-05T10:00", "T4 google single") in G, "Google T4 moved from Outlook")
check(not any("T2" in t for _, t in O), "Outlook T2 deleted after Google delete")
check(not any("T5" in t for _, t in G), "Google T5 series deleted after Outlook delete")
check(("2026-11-02T14:00", "T3 renamed") in G and ("2026-11-04T14:00", "T3 exception") in G,
      "Google T3 series renamed, exception kept")

print(f"\n{len(fails)} failed" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
