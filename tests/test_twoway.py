import itertools
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from calsync import canon, config, recur, render, twoway
from calsync.state import State

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)
CLOCK = itertools.count(1)


def stamp():
    return f"2026-10-18T09:{next(CLOCK) % 60:02d}:{next(CLOCK) % 60:02d}.1234567Z"


class RecurTest(unittest.TestCase):
    def roundtrip(self, rule, start):
        g = recur.rrule_to_graph([rule], start)
        return recur.graph_to_rrule(g)

    def test_common_rules_roundtrip(self):
        d = date(2026, 10, 20)  # Tuesday
        cases = {
            "RRULE:FREQ=DAILY": "RRULE:FREQ=DAILY",
            "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5": "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5",
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE": "RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
            "RRULE:FREQ=WEEKLY": "RRULE:FREQ=WEEKLY;BYDAY=TU",
            "RRULE:FREQ=MONTHLY": "RRULE:FREQ=MONTHLY;BYMONTHDAY=20",
            "RRULE:FREQ=MONTHLY;BYDAY=3TU": "RRULE:FREQ=MONTHLY;BYDAY=3TU",
            "RRULE:FREQ=MONTHLY;BYDAY=-1FR": "RRULE:FREQ=MONTHLY;BYDAY=-1FR",
            "RRULE:FREQ=YEARLY": "RRULE:FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=20",
            "RRULE:FREQ=YEARLY;BYMONTH=5;BYDAY=2SU": "RRULE:FREQ=YEARLY;BYMONTH=5;BYDAY=2SU",
        }
        for src, want in cases.items():
            self.assertEqual(self.roundtrip(src, d), want, src)

    def test_until(self):
        g = recur.rrule_to_graph(["RRULE:FREQ=DAILY;UNTIL=20261031T155959Z"], date(2026, 10, 20))
        self.assertEqual(g["range"], {"startDate": "2026-10-20", "recurrenceTimeZone": "China Standard Time",
                                      "type": "endDate", "endDate": "2026-10-31"})
        self.assertEqual(recur.graph_to_rrule(g), "RRULE:FREQ=DAILY;UNTIL=20261031T155959Z")
        self.assertEqual(recur.graph_to_rrule(g, all_day=True), "RRULE:FREQ=DAILY;UNTIL=20261031")

    def test_exdate_ignored_rdate_rejected(self):
        g = recur.rrule_to_graph(["EXDATE;TZID=Asia/Shanghai:20261022T090000", "RRULE:FREQ=DAILY"], date(2026, 10, 20))
        self.assertEqual(g["pattern"]["type"], "daily")
        with self.assertRaises(recur.Unsupported):
            recur.rrule_to_graph(["RRULE:FREQ=DAILY", "RDATE:20261101"], date(2026, 10, 20))

    def test_unsupported(self):
        for rule in ("RRULE:FREQ=HOURLY", "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15", "RRULE:FREQ=MONTHLY;BYMONTHDAY=-1",
                     "RRULE:FREQ=DAILY;BYHOUR=9", "RRULE:FREQ=MONTHLY;BYDAY=1MO,3WE"):
            with self.assertRaises(recur.Unsupported, msg=rule):
                recur.rrule_to_graph([rule], date(2026, 10, 20))
        with self.assertRaises(recur.Unsupported):
            recur.rrule_to_graph(["RRULE:FREQ=DAILY", "RRULE:FREQ=WEEKLY"], date(2026, 10, 20))


class CanonTest(unittest.TestCase):
    def test_google_meta_stripped_and_times_local(self):
        g = {"summary": "A", "description": "hello\n\n———\nOrganizer: x", "start": {"dateTime": "2026-10-20T01:00:00Z"},
             "end": {"dateTime": "2026-10-20T10:00:00+08:00"}}
        c = canon.from_google(g, TZ)
        self.assertEqual((c["body"], c["start"], c["end"]), ("hello", "2026-10-20T09:00", "2026-10-20T10:00"))
        self.assertEqual(canon.strip_meta("———\nOutlook: x"), "")

    def test_outlook_and_google_agree(self):
        o = {"subject": "A", "body": {"content": "hi\r\n"}, "start": {"dateTime": "2026-10-20T09:00:00.0000000"},
             "end": {"dateTime": "2026-10-20T10:00:00.0000000"}, "showAs": "busy", "sensitivity": "normal"}
        g = {"summary": "A", "description": "hi", "start": {"dateTime": "2026-10-20T09:00:00+08:00"},
             "end": {"dateTime": "2026-10-20T10:00:00+08:00"}}
        self.assertEqual(canon.from_outlook(o, TZ), canon.from_google(g, TZ))


class DecideTest(unittest.TestCase):
    def d(self, **kw):
        base = dict(have_o=True, have_g=True, o_changed=False, g_changed=False, o_newer=True, outlook_auth=False)
        base.update(kw)
        return twoway.decide(**base)

    def test_table(self):
        self.assertEqual(self.d(), "none")
        self.assertEqual(self.d(o_changed=True), "o2g")
        self.assertEqual(self.d(g_changed=True), "g2o")
        self.assertEqual(self.d(o_changed=True, g_changed=True, o_newer=False), "g2o")
        self.assertEqual(self.d(o_changed=True, g_changed=True, o_newer=True), "o2g")
        self.assertEqual(self.d(g_changed=True, outlook_auth=True), "o2g")
        self.assertEqual(self.d(o_changed=True, g_changed=True, equal=True), "none")
        self.assertEqual(self.d(have_g=False), "del_o")
        self.assertEqual(self.d(have_g=False, o_changed=True), "recreate_g")
        self.assertEqual(self.d(have_g=False, outlook_auth=True), "recreate_g")
        self.assertEqual(self.d(have_o=False), "del_g")
        self.assertEqual(self.d(have_o=False, g_changed=True), "recreate_o")
        self.assertEqual(self.d(have_o=False, have_g=False), "drop")

    def test_inst_key(self):
        self.assertEqual(twoway._inst_key("2026-10-20T01:00:00.0000000Z", False, TZ), "20261020T0100")
        self.assertEqual(twoway._inst_key({"dateTime": "2026-10-20T09:00:00+08:00"}, False, TZ), "20261020T0100")
        self.assertEqual(twoway._inst_key({"date": "2026-10-20"}, True, TZ), "20261020")
        self.assertEqual(twoway._inst_key("2026-10-19T16:00:00Z", True, TZ), "20261020")


# ---- both sides in memory ----

class FakeGraph:
    def __init__(self):
        self.ev, self.n = {}, 0

    def add(self, subject, start="2026-10-20T09:00", end="2026-10-20T10:00", **kw):
        self.n += 1
        e = {"id": f"O{self.n}", "type": "singleInstance", "subject": subject, "body": {"content": ""},
             "start": {"dateTime": start + ":00.0000000"}, "end": {"dateTime": end + ":00.0000000"},
             "isAllDay": False, "showAs": "busy", "sensitivity": "normal", "isOrganizer": True,
             "lastModifiedDateTime": stamp()}
        e.update(kw)
        self.ev[e["id"]] = e
        return e

    def calendar_view(self, cal, s, e, tz):
        return [dict(v) for v in self.ev.values()]

    def get_event(self, i, tz, cal=None):
        return dict(self.ev[i]) if i in self.ev else None

    def create_event(self, cal, body, tz):
        e = self.add(body["subject"])
        self.patch_event(e["id"], body, tz)
        return self.get_event(e["id"], tz)

    def patch_event(self, i, body, tz):
        e = self.ev[i]
        for k, v in body.items():
            if k in ("start", "end"):
                v = {"dateTime": v["dateTime"] + ".0000000"}
            e[k] = v
        e["lastModifiedDateTime"] = stamp()
        return self.get_event(i, tz)

    def delete_event(self, i, tz):
        self.ev.pop(i, None)


class FakeGCal:
    def __init__(self):
        self.ev, self.n = {}, 0

    def add(self, summary, start="2026-10-20T09:00:00+08:00", end="2026-10-20T10:00:00+08:00"):
        self.n += 1
        return self.insert("t", {"id": f"g{self.n}", "summary": summary,
                                 "start": {"dateTime": start}, "end": {"dateTime": end}})

    def list_all(self, cal, a, b):
        return [dict(v) for v in self.ev.values()]

    def get(self, cal, i):
        return dict(self.ev[i]) if i in self.ev else None

    def instances(self, *a):
        return []

    def insert(self, cal, body):
        e = dict(body)
        e.setdefault("id", f"gx{len(self.ev)}")
        e["updated"] = stamp()
        self.ev[e["id"]] = e
        return dict(e)

    def patch(self, cal, i, body):
        self.ev[i].update(body)
        self.ev[i]["updated"] = stamp()
        return dict(self.ev[i])

    def delete(self, cal, i):
        self.ev.pop(i, None)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.o, self.g = FakeGraph(), FakeGCal()
        self.state = State(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.cfg = config.parse({"outlook": {"client_id": "x"}, "google": {"key_file": "k"},
                                 "sync": [{"source": "Cal", "target": "t", "mode": "two-way"}]})
        self.m = self.cfg.mappings[0]

    def run_once(self, force=False):
        from calsync import sync
        narrow, wide = sync.windows(self.cfg, NOW)
        eng = twoway.Engine(graph=self.o, gcal=self.g, state=self.state, cfg=self.cfg, mapping=self.m,
                            mid="m1", cal_id="cal", narrow=narrow, wide=wide, log=lambda *_: None)
        eng.plan()
        guard = eng.guard()
        if guard and not force:
            return guard
        eng.execute()
        self.assertEqual(eng.errors, [])
        return eng.summary()

    def titles(self):
        return (sorted(e["subject"] for e in self.o.ev.values()), sorted(e["summary"] for e in self.g.ev.values()))

    def test_initial_both_ways_then_stable(self):
        self.o.add("from outlook")
        self.g.add("from google")
        self.assertEqual(self.run_once(), "+o=1 +g=1")
        self.assertEqual(self.titles(), (["from google", "from outlook"], ["from google", "from outlook"]))
        self.assertEqual(self.run_once(), "no changes")
        self.assertEqual(self.run_once(), "no changes")

    def test_edit_each_side(self):
        oe = self.o.add("a")
        self.run_once()
        gid = render.event_id("m1", oe["id"])
        self.g.patch("t", gid, {"summary": "edited in google"})
        self.assertEqual(self.run_once(), "~o=1")
        self.assertEqual(self.o.ev[oe["id"]]["subject"], "edited in google")
        self.assertEqual(self.run_once(), "no changes")
        self.o.patch_event(oe["id"], {"subject": "edited in outlook"}, TZ)
        self.assertEqual(self.run_once(), "~g=1")
        self.assertEqual(self.g.ev[gid]["summary"], "edited in outlook")
        self.assertEqual(self.run_once(), "no changes")

    def test_conflict_newer_wins(self):
        oe = self.o.add("a")
        self.run_once()
        gid = render.event_id("m1", oe["id"])
        self.o.patch_event(oe["id"], {"subject": "outlook older"}, TZ)
        self.g.patch("t", gid, {"summary": "google newer"})
        self.run_once()
        self.assertEqual(self.titles(), (["google newer"], ["google newer"]))

    def test_others_meeting_outlook_wins(self):
        oe = self.o.add("their meeting", isOrganizer=False)
        self.run_once()
        gid = render.event_id("m1", oe["id"])
        self.g.patch("t", gid, {"summary": "my edit"})
        self.assertEqual(self.run_once(), "~g=1")
        self.assertEqual(self.titles(), (["their meeting"], ["their meeting"]))
        self.g.delete("t", gid)
        self.assertEqual(self.run_once(), "+g=1")
        self.assertIn(gid, self.g.ev)

    def test_deletes(self):
        a, b = self.o.add("a"), self.o.add("b")
        self.run_once()
        self.g.delete("t", render.event_id("m1", a["id"]))
        self.o.delete_event(b["id"], TZ)
        self.assertEqual(self.run_once(), "-o=1 -g=1")
        self.assertEqual(self.titles(), ([], []))
        self.assertEqual(self.run_once(), "no changes")

    def test_delete_vs_edit_keeps_edit(self):
        a = self.o.add("a")
        self.run_once()
        self.g.delete("t", render.event_id("m1", a["id"]))
        self.o.patch_event(a["id"], {"subject": "a edited"}, TZ)
        self.assertEqual(self.run_once(), "+g=1")
        self.assertEqual(self.titles(), (["a edited"], ["a edited"]))

    def test_guard(self):
        for i in range(30):
            self.o.add(f"e{i}")
        self.run_once()
        self.g.ev.clear()
        self.assertIn("would delete 30 Outlook events", self.run_once())
        self.assertEqual(len(self.o.ev), 30)

    def test_state_loss_relinks_without_duplicates(self):
        self.o.add("from outlook")
        self.g.add("from google")
        self.run_once()
        self.state = State(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.run_once()
        self.assertEqual(self.titles(), (["from google", "from outlook"], ["from google", "from outlook"]))
        self.assertEqual(self.run_once(), "no changes")


class ConfigTwoWayTest(unittest.TestCase):
    def test_two_way_rules(self):
        base = {"outlook": {"client_id": "x"}, "google": {"key_file": "k"}}
        with self.assertRaises(config.ConfigError):
            config.parse({**base, "sync": [{"source": "A", "target": "t", "mode": "two-way", "prefix": "[x] "}]})
        with self.assertRaises(config.ConfigError):
            config.parse({**base, "sync": [{"source": "A", "target": "t", "mode": "two-way"},
                                           {"source": "B", "target": "t"}]})
        with self.assertRaises(config.ConfigError):
            config.parse({**base, "sync": [{"source": "A", "target": "t", "mode": "both"}]})


if __name__ == "__main__":
    unittest.main()
