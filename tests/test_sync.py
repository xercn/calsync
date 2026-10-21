import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

from calsync import config, notify, render, sync
from calsync.http import HttpError, request

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def ev(i, start="2026-10-20T09:00:00.0000000", end="2026-10-20T10:00:00.0000000", **kw):
    e = {"id": f"AAMk{i}", "subject": f"Meeting {i}", "start": {"dateTime": start},
         "end": {"dateTime": end}, "isAllDay": False}
    e.update(kw)
    return e


def cfg(**kw):
    raw = {"outlook": {"client_id": "x"}, "google": {"key_file": "k.json"},
           "sync": [{"source": "Calendar", "target": "a@group.calendar.google.com"}]}
    raw.update(kw)
    return config.parse(raw)


class RenderTest(unittest.TestCase):
    def test_event_id_is_valid_google_id_and_stable(self):
        a = render.event_id("m1", "AAMk1")
        self.assertRegex(a, r"^[a-v0-9]{5,1024}$")
        self.assertEqual(a, render.event_id("m1", "AAMk1"))
        self.assertNotEqual(a, render.event_id("m2", "AAMk1"))

    def test_timed(self):
        b = render.render(ev(1), mid="m", tz=TZ)
        self.assertEqual(b["start"], {"dateTime": "2026-10-20T09:00:00", "timeZone": TZ})
        self.assertEqual(b["end"]["dateTime"], "2026-10-20T10:00:00")
        self.assertEqual(b["transparency"], "opaque")
        self.assertEqual(b["extendedProperties"]["private"]["calsync_map"], "m")

    def test_all_day_uses_dates(self):
        b = render.render(ev(1, "2026-10-20T00:00:00.0000000", "2026-10-22T00:00:00.0000000", isAllDay=True), mid="m")
        self.assertEqual(b["start"], {"date": "2026-10-20"})
        self.assertEqual(b["end"], {"date": "2026-10-22"})

    def test_details_go_to_description_not_attendees(self):
        e = ev(1, body={"content": "Agenda\n1. foo"},
               organizer={"emailAddress": {"name": "Alice", "address": "alice@x.com"}},
               attendees=[{"emailAddress": {"name": "Bob", "address": "bob@x.com"},
                           "status": {"response": "accepted"}, "type": "required"},
                          {"emailAddress": {"address": "room@x.com"}, "status": {"response": "none"}, "type": "resource"}],
               isOnlineMeeting=True, onlineMeeting={"joinUrl": "https://teams.example/j/1"},
               webLink="https://outlook.live.com/owa/?itemid=1", location={"displayName": "Room 1"})
        b = render.render(e, mid="m")
        self.assertNotIn("attendees", b)
        d = b["description"]
        self.assertTrue(d.startswith("Agenda\n1. foo"))
        self.assertIn("Organizer: Alice <alice@x.com>", d)
        self.assertIn("  - Bob <bob@x.com> (accepted)", d)
        self.assertIn("  - room@x.com (no response, resource)", d)
        self.assertIn("Online meeting: https://teams.example/j/1", d)
        self.assertEqual(b["location"], "Room 1")
        self.assertEqual(b["source"]["url"], "https://outlook.live.com/owa/?itemid=1")

    def test_prefix_private_free(self):
        b = render.render(ev(1, sensitivity="private", showAs="free"), mid="m", prefix="[O] ")
        self.assertEqual(b["summary"], "[O] Meeting 1")
        self.assertEqual(b["visibility"], "private")
        self.assertEqual(b["transparency"], "transparent")

    def test_reminders(self):
        e = ev(1, isReminderOn=True, reminderMinutesBeforeStart=15)
        self.assertEqual(render.render(e, mid="m")["reminders"]["overrides"], [{"method": "popup", "minutes": 15}])
        self.assertEqual(render.render(e, mid="m", reminders="none")["reminders"], {"useDefault": False, "overrides": []})
        self.assertEqual(render.render(e, mid="m", reminders="default")["reminders"], {"useDefault": True})
        self.assertEqual(render.render(ev(1, isReminderOn=False), mid="m")["reminders"]["overrides"], [])

    def test_hash_changes_with_content_only(self):
        h = lambda e: render.render(e, mid="m")["extendedProperties"]["private"]["calsync_hash"]
        self.assertEqual(h(ev(1)), h(ev(1)))
        self.assertNotEqual(h(ev(1)), h(ev(1, subject="Renamed")))

    def test_long_description_truncated(self):
        b = render.render(ev(1, body={"content": "x" * 20000}), mid="m")
        self.assertLessEqual(len(b["description"]), render.DESC_LIMIT)
        self.assertTrue(b["description"].endswith("[truncated]"))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()
        self.m = self.cfg.mappings[0]
        self.narrow, self.wide = sync.windows(self.cfg, NOW)

    def _dst(self, *events):
        return {b["id"]: b["extendedProperties"]["private"]
                for b in (render.render(e, mid="m", tz=TZ) for e in events)}

    def plan(self, src, dst):
        return sync.plan(src, dst, mid="m", mapping=self.m, cfg=self.cfg, narrow=self.narrow)

    def test_create_update_delete_unchanged(self):
        dst = self._dst(ev(1), ev(2), ev(3))
        p = self.plan([ev(1), ev(2, subject="changed"), ev(4)], dst)
        self.assertEqual([b["id"] for b in p.creates], [render.event_id("m", "AAMk4")])
        self.assertEqual([b["id"] for b in p.updates], [render.event_id("m", "AAMk2")])
        self.assertEqual(p.deletes, [render.event_id("m", "AAMk3")])
        self.assertEqual(p.unchanged, 1)

    def test_margin_event_not_created_but_not_deleted(self):
        # Events only in the wide-window margin (outside the narrow window): not created;
        # ones already in Google are updated, not deleted
        past = ev(9, "2026-09-17T12:00:00.0000000", "2026-09-17T13:00:00.0000000")  # ~1 day before the narrow window start
        self.assertFalse(render.overlaps(past, TZ, *self.narrow))
        self.assertEqual(self.plan([past], {}).creates, [])
        dst = self._dst(ev(9, "2026-09-19T12:00:00.0000000", "2026-09-19T13:00:00.0000000"))
        p = self.plan([past], dst)
        self.assertEqual(p.deletes, [])
        self.assertEqual(len(p.updates), 1)

    def test_duplicate_source_ids_ignored(self):
        p = self.plan([ev(1), ev(1)], {})
        self.assertEqual(len(p.creates), 1)

    def test_guard(self):
        dst = self._dst(*[ev(i) for i in range(30)])
        p = self.plan([], dst)
        self.assertIn("would delete 30 of 30", sync.check_guard(p, self.cfg))
        p = self.plan([ev(i) for i in range(20)], dst)   # 10 deletes: does not exceed guard_min
        self.assertIsNone(sync.check_guard(p, self.cfg))
        small = self._dst(*[ev(i) for i in range(5)])
        self.assertIsNone(sync.check_guard(self.plan([], small), self.cfg))  # deleting all of a small set is allowed


class ConfigTest(unittest.TestCase):
    def test_defaults_and_names(self):
        c = cfg(sync=[{"source": "Calendar", "target": "a"},
                      {"source": "Work", "target": "b@gmail.com", "prefix": "[W] ", "name": "work"}])
        self.assertEqual(c.past_days, 30)
        self.assertEqual(c.mappings[0].name, "Calendar → a")
        self.assertEqual(c.mappings[1].prefix, "[W] ")

    def test_errors(self):
        with self.assertRaises(config.ConfigError):
            cfg(sync=[])
        with self.assertRaises(config.ConfigError):
            cfg(sync=[{"source": "A", "target": "t"}, {"source": "A", "target": "t", "name": "other"}])
        with self.assertRaises(config.ConfigError):
            cfg(sync=[{"source": "A", "target": "t", "reminders": "loud"}])


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = cfg()
        self.cfg.state_dir = self.dir
        self.sent = []
        p = mock.patch.object(notify, "send", lambda c, lvl, t, b, log=print: self.sent.append(t) or True)
        p.start()
        self.addCleanup(p.stop)

    def test_transitions(self):
        notify.report(self.cfg, [], now=0)
        notify.report(self.cfg, ["boom"], now=100)              # 1st failed run: nothing yet
        self.assertEqual(self.sent, [])
        notify.report(self.cfg, ["boom"], now=700)              # 2nd in a row: push
        notify.report(self.cfg, ["boom"], now=1300)             # persistent failure: no repeat push
        notify.report(self.cfg, ["boom"], now=700 + 24 * 3600)  # push again after 24 hours
        notify.report(self.cfg, [], now=700 + 25 * 3600)
        notify.report(self.cfg, [], now=700 + 26 * 3600)
        self.assertEqual(self.sent, ["Calendar sync failing", "Calendar sync failing", "Calendar sync recovered"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "status.json")))

    def test_single_blip_is_silent(self):
        notify.report(self.cfg, ["network unreachable"], now=100)
        st = notify.report(self.cfg, [], now=700)
        self.assertEqual(self.sent, [])                         # neither failing nor recovered
        self.assertEqual(st, {"failing": False, "fails": 0})
        notify.report(self.cfg, ["boom"], now=1300)             # the count restarted
        self.assertEqual(self.sent, [])

    def test_failed_push_is_retried_next_run(self):
        notify.send = lambda *a, **k: False                     # endpoint down on the 2nd run
        notify.report(self.cfg, ["boom"], now=100)
        notify.report(self.cfg, ["boom"], now=700)
        notify.send = lambda c, lvl, t, b, log=print: self.sent.append(t) or True
        notify.report(self.cfg, ["boom"], now=1300)
        notify.report(self.cfg, [], now=1900)
        self.assertEqual(self.sent, ["Calendar sync failing", "Calendar sync recovered"])

    def test_old_status_file(self):
        with open(os.path.join(self.dir, "status.json"), "w") as f:   # written by the old version: alert already sent
            f.write('{"failing": true, "since": 0, "notified": 50}')
        notify.report(self.cfg, ["boom"], now=100)
        notify.report(self.cfg, [], now=200)
        self.assertEqual(self.sent, ["Calendar sync recovered"])


class TokenRefreshTest(unittest.TestCase):
    def setUp(self):
        from calsync import msgraph
        self.msgraph = msgraph
        self.path = os.path.join(tempfile.mkdtemp(), "tok.json")
        with open(self.path, "w") as f:
            f.write('{"access_token": "old", "refresh_token": "r1", "expires_at": 0}')
        no_sleep = lambda *a, **kw: request(*a, sleep=lambda s: None, **kw)   # request's sleep is a default argument
        p = mock.patch.object(msgraph, "request", no_sleep)
        p.start()
        self.addCleanup(p.stop)

    def _resp(self, status, body):
        r = mock.Mock(status_code=status, headers={"content-type": "application/json"}, text="t")
        r.json.return_value = body
        return r

    def test_network_blip_is_retried(self):
        s = mock.Mock()
        s.request.side_effect = [requests.ConnectionError("[Errno 101] Network is unreachable"),
                                 self._resp(503, {}),
                                 self._resp(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 3600})]
        g = self.msgraph.Graph("cid", self.path, session=s)
        self.assertEqual(g._access_token(), "new")
        self.assertEqual(s.request.call_count, 3)

    def test_invalid_grant_is_not_retried(self):
        s = mock.Mock()
        s.request.side_effect = [self._resp(400, {"error": "invalid_grant"})]
        g = self.msgraph.Graph("cid", self.path, session=s)
        with self.assertRaises(self.msgraph.LoginRequired):
            g._access_token()
        self.assertEqual(s.request.call_count, 1)


class HttpTest(unittest.TestCase):
    def _resp(self, status, body=None, headers=None):
        r = mock.Mock(status_code=status, headers=headers or {}, text="t")
        r.json.return_value = body or {}
        return r

    def test_retries_then_ok(self):
        s = mock.Mock()
        s.request.side_effect = [self._resp(503), self._resp(429, headers={"Retry-After": "1"}), self._resp(200)]
        slept = []
        self.assertEqual(request(s, "GET", "https://x", sleep=slept.append).status_code, 200)
        self.assertEqual(slept, [2, 1])

    def test_google_rate_limit_403_retried_other_403_not(self):
        s = mock.Mock()
        rl = self._resp(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        s.request.side_effect = [rl, self._resp(200)]
        self.assertEqual(request(s, "GET", "https://x", sleep=lambda _: None).status_code, 200)
        s.request.side_effect = [self._resp(403, {"error": {"errors": [{"reason": "forbidden"}]}})]
        with self.assertRaises(HttpError):
            request(s, "GET", "https://x", sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
