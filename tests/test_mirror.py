import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from calsync import caldav, config, ics, mirror

try:
    import dateutil  # noqa: F401
    HAVE_DATEUTIL = True
except ImportError:
    HAVE_DATEUTIL = False

TZ = "Asia/Shanghai"
W0 = datetime(2026, 9, 17, tzinfo=timezone.utc)
W1 = datetime(2027, 10, 19, tzinfo=timezone.utc)

FEED = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:携程\r
BEGIN:VTIMEZONE\r
TZID:Asia/Shanghai\r
BEGIN:STANDARD\r
DTSTART:19910915T000000\r
RRULE:FREQ=YEARLY;UNTIL=19910914T150000Z;BYMONTH=9;BYDAY=3SU\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:flight-1\r
SUMMARY:MU5101 上海虹桥 → 北京首都\r
LOCATION:虹桥 T2\\, 上海\r
DESCRIPTION:座位 32A\\n登机口 C12\r
DTSTART;TZID=Asia/Shanghai:20261020T090000\r
DTEND;TZID=Asia/Tokyo:20261020T120000\r
URL:https://example.com/order/1\r
BEGIN:VALARM\r
TRIGGER:-PT1H\r
SUMMARY:should be ignored\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:hotel-1\r
SUMMARY:Hotel\r
DTSTART;VALUE=DATE:20261021\r
DTEND;VALUE=DATE:20261023\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:utc-1\r
SUMMARY:A long title that is folded across\r
  two lines\r
DTSTART:20261101T010000Z\r
DURATION:PT1H30M\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled-1\r
STATUS:CANCELLED\r
SUMMARY:gone\r
DTSTART:20261102T010000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old-1\r
SUMMARY:old trip\r
DTSTART;TZID=Asia/Shanghai:20250319T110000\r
DTEND;TZID=Asia/Shanghai:20250319T120000\r
END:VEVENT\r
END:VCALENDAR\r
"""


class IcsTest(unittest.TestCase):
    def setUp(self):
        self.evs = {e["subject"]: e for e in ics.events(FEED, W0, W1, TZ)}

    def test_window_and_cancelled(self):
        self.assertEqual(set(self.evs), {"MU5101 上海虹桥 → 北京首都", "Hotel",
                                         "A long title that is folded across two lines"})
        self.assertEqual(ics.count_events(FEED), 5)

    def test_timezones_converted_to_local(self):
        e = self.evs["MU5101 上海虹桥 → 北京首都"]
        self.assertEqual(e["start"]["dateTime"][:16], "2026-10-20T09:00")
        self.assertEqual(e["end"]["dateTime"][:16], "2026-10-20T11:00")   # Tokyo 12:00 = Shanghai 11:00
        u = self.evs["A long title that is folded across two lines"]
        self.assertEqual((u["start"]["dateTime"][:16], u["end"]["dateTime"][:16]), ("2026-11-01T09:00", "2026-11-01T10:30"))

    def test_text_unescaped_url_appended(self):
        e = self.evs["MU5101 上海虹桥 → 北京首都"]
        self.assertEqual(e["location"]["displayName"], "虹桥 T2, 上海")
        self.assertEqual(e["body"]["content"], "座位 32A\n登机口 C12\n\nhttps://example.com/order/1")

    def test_all_day(self):
        h = self.evs["Hotel"]
        self.assertTrue(h["isAllDay"])
        self.assertEqual((h["start"]["dateTime"][:10], h["end"]["dateTime"][:10]), ("2026-10-21", "2026-10-23"))

    def test_ids_stable_and_distinct(self):
        again = {e["subject"]: e["id"] for e in ics.events(FEED, W0, W1, TZ)}
        self.assertEqual(again, {k: v["id"] for k, v in self.evs.items()})
        self.assertEqual(len(set(again.values())), 3)

    @unittest.skipUnless(HAVE_DATEUTIL, "dateutil not installed (it is on the server)")
    def test_rrule_exdate_override(self):
        feed = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:weekly
SUMMARY:Weekly
DTSTART;TZID=Asia/Shanghai:20261019T100000
DTEND;TZID=Asia/Shanghai:20261019T110000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Asia/Shanghai:20261026T100000
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=Asia/Shanghai:20261102T100000
SUMMARY:Weekly moved
DTSTART;TZID=Asia/Shanghai:20261102T150000
DTEND;TZID=Asia/Shanghai:20261102T160000
END:VEVENT
END:VCALENDAR
"""
        got = sorted((e["start"]["dateTime"][:16], e["subject"]) for e in ics.events(feed, W0, W1, TZ))
        self.assertEqual(got, [("2026-10-19T10:00", "Weekly"), ("2026-11-02T15:00", "Weekly moved"),
                               ("2026-11-09T10:00", "Weekly")])


class CaldavTest(unittest.TestCase):
    def test_parse_calendars(self):
        xml = """<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/123/calendars/home/</d:href><d:propstat><d:prop><d:displayname>携程</d:displayname>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
<d:response><d:href>/123/calendars/tasks/</d:href><d:propstat><d:prop><d:displayname>Reminders</d:displayname>
<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>
</d:multistatus>"""
        cals = caldav.parse_calendars(ET.fromstring(xml), "https://p1-caldav.icloud.com:443/123/calendars/")
        self.assertEqual(cals, [{"name": "携程", "url": "https://p1-caldav.icloud.com:443/123/calendars/home/"}])


class PlanOutlookTest(unittest.TestCase):
    def setUp(self):
        self.src = ics.events(FEED, W0, W1, TZ)
        self.narrow = (W0, W1)

    def test_first_run_creates_all(self):
        c, u, d, w = mirror.plan_outlook(self.src, {}, {}, self.narrow, TZ)
        self.assertEqual((len(c), u, d, w), (3, [], [], []))

    def test_steady_update_delete_recreate(self):
        from calsync import canon
        rows = {e["id"]: (f"O-{e['id']}", canon.digest(canon.from_outlook(e, TZ))) for e in self.src}
        existing = {f"O-{e['id']}": {} for e in self.src}
        self.assertEqual(mirror.plan_outlook(self.src, existing, rows, self.narrow, TZ), ([], [], [], []))
        changed = [dict(e, subject="new") if e["subject"] == "Hotel" else e for e in self.src]
        dropped = [e for e in changed if not e["subject"].startswith("MU")]
        del existing[f"O-{dropped[0]['id']}"]          # the user manually deleted one in Outlook
        c, u, d, w = mirror.plan_outlook(dropped, existing, rows, self.narrow, TZ)
        self.assertEqual(len(c), 1)                     # restored
        self.assertEqual([ev["subject"] for _, ev in u], ["new"] if dropped[0]["subject"] != "new" else [])
        self.assertEqual(len(d), 1)                     # the flight is gone from the source

    def test_lost_state_wipes_and_rebuilds(self):
        c, u, d, w = mirror.plan_outlook(self.src, {"X1": {}, "X2": {}}, {}, self.narrow, TZ)
        self.assertEqual((len(c), sorted(w)), (3, ["X1", "X2"]))


class EnsureCalendarTest(unittest.TestCase):
    class G:
        def __init__(self, cals):
            self.cals, self.created = cals, []

        def calendars(self):
            return self.cals

        def create_calendar(self, name):
            c = {"id": f"new-{name}", "name": name, "canEdit": True}
            self.cals.append(c)
            self.created.append(name)
            return c

    def setUp(self):
        import os, tempfile
        from calsync.state import State
        self.state = State(os.path.join(tempfile.mkdtemp(), "s.db"))

    def test_never_adopts_existing_calendar(self):
        g = self.G([{"id": "user-personal", "name": "Personal", "canEdit": True}])
        with self.assertRaises(LookupError):
            mirror.ensure_outlook_calendar(g, "Personal", self.state, "m", False)
        self.assertEqual(g.created, [])

    def test_creates_once_then_reuses(self):
        g = self.G([])
        a = mirror.ensure_outlook_calendar(g, "携程", self.state, "m", False)
        b = mirror.ensure_outlook_calendar(g, "携程", self.state, "m", False)
        self.assertEqual((a, b, g.created), ("new-携程", "new-携程", ["携程"]))

    def test_dry_run_creates_nothing(self):
        g = self.G([])
        self.assertIsNone(mirror.ensure_outlook_calendar(g, "携程", self.state, "m", True))
        self.assertEqual(g.created, [])


class MirrorConfigTest(unittest.TestCase):
    base = {"outlook": {"client_id": "x"}, "google": {"key_file": "k"}}

    def test_parse(self):
        c = config.parse({**self.base, "icloud": {"apple_id": "a@b", "password_file": "/p"},
                          "mirror": [{"name": "携程", "icloud": "携程", "google": ["x@group", "b@gmail.com"]}]})
        self.assertEqual((c.mirrors[0].outlook, c.mirrors[0].google, c.icloud_apple_id), (True, ["x@group", "b@gmail.com"], "a@b"))

    def test_errors(self):
        with self.assertRaises(config.ConfigError):
            config.parse({**self.base, "mirror": [{"name": "x"}]})
        with self.assertRaises(config.ConfigError):
            config.parse({**self.base, "mirror": [{"name": "x", "icloud": "a", "url_file": "/f"}]})
        with self.assertRaises(config.ConfigError):   # same-named Outlook calendar is also a [[sync]] source -> loop
            config.parse({**self.base, "sync": [{"source": "携程", "target": "t"}],
                          "mirror": [{"name": "携程", "icloud": "携程"}]})
        with self.assertRaises(config.ConfigError):   # a two-way target cannot also be a mirror target
            config.parse({**self.base, "sync": [{"source": "A", "target": "t", "mode": "two-way"}],
                          "mirror": [{"name": "携程", "icloud": "携程", "google": ["t"]}]})


if __name__ == "__main__":
    unittest.main()
