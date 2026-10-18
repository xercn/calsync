"""Config page: generation, merge, import, preview -> confirm -> apply, delete cap, leftovers, cleanup
(fake Outlook / Google / iCloud / Worker)."""
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    import cryptography  # noqa: F401
except ImportError:      # No cryptography on the Mac: gcal is only really used on the server, so stub it here
    sys.modules.setdefault("calsync.gcal", types.SimpleNamespace(GCal=None))

from calsync import cli, config, managed, sync  # noqa: E402

B = "b@gmail.com"
A_TW = "tw@group.calendar.google.com"
A_XC = "xc@group.calendar.google.com"
A_ST = "st@group.calendar.google.com"


def _dt(days, hour=9):
    d = datetime.now(timezone.utc) + timedelta(days=days)
    return d.strftime("%Y%m%d") + f"T{hour:02d}0000"


def feed(n, prefix="E"):
    ev = "".join(f"BEGIN:VEVENT\r\nUID:{prefix}{i}\r\nSUMMARY:{prefix} {i}\r\nDTSTART;TZID=Asia/Shanghai:{_dt(3 + i)}\r\n"
                 f"DTEND;TZID=Asia/Shanghai:{_dt(3 + i, 10)}\r\nEND:VEVENT\r\n" for i in range(n))
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + ev + "END:VCALENDAR\r\n"


def oev(i):
    d = (datetime.now() + timedelta(days=2 + i)).strftime("%Y-%m-%d")
    return {"id": f"AAMk{i}", "subject": f"M {i}", "start": {"dateTime": d + "T09:00:00.0000000"},
            "end": {"dateTime": d + "T10:00:00.0000000"}, "isAllDay": False}


class Graph:
    def __init__(self):
        self.cals = [{"id": "c-cal", "name": "Calendar", "canEdit": True},
                     {"id": "c-per", "name": "Personal", "canEdit": True},
                     {"id": "c-krow", "name": "Krow", "canEdit": True}]
        self.ev = {"c-cal": {e["id"]: e for e in map(oev, range(3))}, "c-per": {}, "c-krow": {"AAMk9": oev(9)}}
        self.n = 0

    def calendars(self):
        return list(self.cals)

    def calendar_view(self, cal_id, s, e, tz):
        return list(self.ev.get(cal_id, {}).values())

    def create_calendar(self, name):
        self.n += 1
        c = {"id": f"new{self.n}", "name": name, "canEdit": True}
        self.cals.append(c)
        self.ev[c["id"]] = {}
        return c

    def delete_calendar(self, cal_id):
        self.cals = [c for c in self.cals if c["id"] != cal_id]
        self.ev.pop(cal_id, None)

    def create_event(self, cal_id, body, tz):
        self.n += 1
        e = dict(body, id=f"o{self.n}")
        self.ev[cal_id][e["id"]] = e
        return e

    def patch_event(self, o_id, body, tz):
        pass

    def delete_event(self, o_id, tz):
        for evs in self.ev.values():
            evs.pop(o_id, None)


class GCal:
    email = "sa@calsync.iam.gserviceaccount.com"

    def __init__(self):
        self.cal = {}

    def list_marked(self, cal, map_id=None, time_min=None, time_max=None):
        out = {}
        for gid, b in self.cal.get(cal, {}).items():
            p = b["extendedProperties"]["private"]
            if map_id is None or p.get("calsync_map") == map_id:
                out[gid] = p
        return out

    def insert(self, cal, body):
        self.cal.setdefault(cal, {})[body["id"]] = body

    def update(self, cal, gid, body):
        self.cal[cal][gid] = body

    def delete(self, cal, gid):
        del self.cal[cal][gid]

    def check_access(self, cal):
        return None


class ICloud:
    feeds = {"携程": feed(3, "X"), "Study": feed(2, "S")}

    def calendars(self):
        return [{"name": n, "url": n} for n in self.feeds]

    def events_ics(self, url, s, e):
        return [self.feeds[url]]


class Ctx:
    def __init__(self, cfg):
        self.graph, self.gcal, self.cfg = Graph(), GCal(), cfg
        self._ic = ICloud()

    @property
    def cals(self):
        return self.graph.calendars()

    def icloud(self):
        return self._ic


class Api:
    """Worker stand-in: poll returns canned content, post records what was sent."""
    posts = []

    def __init__(self, cfg):
        pass

    def poll(self):
        raise AssertionError("tests pass poll= explicitly")

    def post(self, path, body):
        Api.posts.append((path, body))
        return {"ok": True}


CONFIG = f"""
state_dir = "{{state}}"
[icloud]
apple_id = "a@icloud.com"
password_file = "/dev/null"
[outlook]
client_id = "x"
[google]
key_file = "k.json"
[managed]
url = "https://calsync.example.com"
key_file = "/dev/null"
b_target = "{B}"
[[sync]]
name = "A-Outlook"
source = "Calendar"
target = "{A_TW}"
mode = "two-way"
[[sync]]
name = "B-Calendar"
source = "Calendar"
target = "{B}"
[[sync]]
name = "B-Personal"
source = "Personal"
target = "{B}"
[[mirror]]
name = "携程"
icloud = "携程"
google = ["{A_XC}", "{B}"]
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.toml")
        with open(self.path, "w") as f:
            f.write(CONFIG.replace("{state}", self.dir))
        self.cfg = config.load(self.path)
        self.ctx = Ctx(self.cfg)
        Api.posts = []
        p = mock.patch.object(managed, "Api", Api)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(cli, "log", lambda *a: None)
        q.start()
        self.addCleanup(q.stop)

    def cycle(self, **poll):
        poll.setdefault("subs", {})
        new, errs = managed.cycle(self.cfg, self.ctx, poll=poll, log=lambda *a: None)
        if new is not self.cfg:
            self.cfg = new
            self.ctx.cfg = new
        return errs

    def sync_round(self):
        only = [m.name for m in self.cfg.mappings if m.mode == "one-way"] + [m.name for m in self.cfg.mirrors]
        errs, rows = cli.sync_all(self.cfg, self.ctx, dry_run=False, only=only)   # two-way is not tested here
        self.assertEqual(errs, [])
        return rows

    def posted(self, path):
        return [b for p, b in Api.posts if p == path]

    def seed(self):
        cur = managed.current_body(self.cfg)
        self.cycle(confirmed={"id": 1, "body": cur, "seed": True})
        self.assertEqual(self.posted("/agent/applied")[-1], {"rev": 1, "ok": True})


class GenerateTest(Base):
    def test_current_body(self):
        self.assertEqual(managed.current_body(self.cfg), {
            "b_merge": ["Calendar", "Personal"],
            "mirrors": [{"name": "携程", "outlook": True, "outlook_name": "", "icloud": "携程", "a": A_XC, "b": True}]})

    def test_toml_round_trip_keeps_two_way_and_replaces_rest(self):
        body = {"b_merge": ["Krow", 'Odd "name"'], "mirrors": [
            {"name": "Study", "icloud": "Study", "outlook": True, "outlook_name": "Study (iCloud)", "a": A_ST, "b": False},
            {"name": "Holidays", "sub": "0123456789abcdef", "outlook": False, "a": "", "b": True}]}
        text = managed.to_toml(body, B, "/var/lib/calsync/managed/subs", 7)
        c = config.load_with_managed(self.path, text)
        self.assertEqual([(m.name, m.source, m.target, m.mode) for m in c.mappings], [
            ("A-Outlook", "Calendar", A_TW, "two-way"), ("B-Krow", "Krow", B, "one-way"),
            ("B-Odd \"name\"", 'Odd "name"', B, "one-way")])
        self.assertEqual([(m.name, m.icloud, m.url_file, m.outlook, m.outlook_name, m.google) for m in c.mirrors], [
            ("Study", "Study", "", True, "Study (iCloud)", [A_ST]),
            ("Holidays", "", "/var/lib/calsync/managed/subs/0123456789abcdef.url", False, "", [B])])

    def test_bad_sub_id_refused(self):
        with self.assertRaises(managed.ApplyError):
            managed.to_toml({"mirrors": [{"name": "x", "sub": "../../etc/x", "b": True}]}, B, "/s")

    def test_stale_after_24h(self):
        self.assertIsNone(managed.stale_error(self.cfg))            # first time: count from now
        t = os.path.getmtime(os.path.join(self.dir, "managed", "poll.json"))
        self.assertIsNone(managed.stale_error(self.cfg, now=t + 23 * 3600))
        self.assertIn("24 h", managed.stale_error(self.cfg, now=t + 24 * 3600 + 5))


class ScheduleTest(Base):
    def test_interval_and_due(self):
        self.assertEqual(managed.interval(self.cfg), 10)                  # nothing recorded: default
        self.assertEqual(managed.interval(self.cfg, {"interval": 5}), 5)
        self.assertEqual(managed.interval(self.cfg, None), 5)             # page unreachable: use the last one
        self.assertEqual(managed.interval(self.cfg, {"interval": 7}), 5)  # unknown values are rejected
        self.assertTrue(managed.due(self.cfg, 10))                        # never ran
        managed.record_run(self.cfg, now=1000)
        self.assertFalse(managed.due(self.cfg, 10, now=1000 + 500))
        self.assertTrue(managed.due(self.cfg, 10, now=1000 + 600 - 45))
        self.assertTrue(managed.due(self.cfg, 5, now=1000 + 300))

    def _poll(self, p):
        """Run cli.cmd_poll with Worker / connections / alerts replaced by stand-ins; return whether a full sync ran."""
        ran = []
        Api.poll = lambda self_: p
        with mock.patch.object(cli, "_setup", lambda cfg, dry: (self.ctx, None)), \
             mock.patch.object(cli, "_full_run", lambda cfg, ctx, errors: ran.append(1) or managed.record_run(cfg) or []), \
             mock.patch.object(cli.notify, "report", lambda *a: None):
            cli.cmd_poll(self.cfg, None)
        return bool(ran)

    def test_poll_runs_only_when_due_or_asked(self):
        idle = {"interval": 10, "sync_now": None, "subs": {}}
        self.assertTrue(self._poll(idle))                 # never ran -> run
        self.assertFalse(self._poll(idle))                # just ran -> skip
        Api.posts = []
        self.assertTrue(self._poll(dict(idle, sync_now=1234)))   # sync now
        self.assertEqual(self.posted("/agent/ack-sync"), [{"at": 1234}])
        managed.record_run(self.cfg, now=time.time() - 6 * 60)
        self.assertFalse(self._poll(idle))                # ran 6 minutes ago, interval 10
        self.assertTrue(self._poll(dict(idle, interval=5)))       # changed to 5 minutes -> due


class FlowTest(Base):
    def test_seed_takes_over_without_changes(self):
        self.sync_round()
        before = {k: dict(v) for k, v in self.ctx.gcal.cal.items()}
        self.seed()
        self.assertTrue(self.cfg.managed_active)
        self.assertEqual(managed.normalize(managed.current_body(self.cfg)),
                         managed.normalize(managed.local(self.cfg)["body"]))
        rows = self.sync_round()
        self.assertTrue(all(not r.get("create") and not r.get("delete") for r in rows if r["kind"] != "two-way"), rows)
        self.assertEqual(self.ctx.gcal.cal, before)

    def test_seed_mismatch_refused(self):
        self.cycle(confirmed={"id": 1, "body": {"b_merge": ["Krow"], "mirrors": []}, "seed": True})
        r = self.posted("/agent/applied")[-1]
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "managed", "managed.toml")))

    def test_preview_confirm_apply_then_remove_and_cleanup(self):
        self.sync_round()
        self.seed()
        cur = managed.local(self.cfg)["body"]
        draft = {"b_merge": ["Calendar", "Krow"],           # drop Personal, add Krow
                 "mirrors": cur["mirrors"] + [
                     {"name": "Study", "icloud": "Study", "outlook": True, "outlook_name": "", "a": A_ST, "b": True},
                     {"name": "Holidays", "sub": "0123456789abcdef", "outlook": False, "outlook_name": "", "a": "", "b": True}]}
        subs = {"0123456789abcdef": "webcal://example.com/h.ics"}
        with mock.patch("calsync.mirror.request") as req:
            req.return_value.text = feed(4, "H")
            self.cycle(draft={"id": 2, "body": draft}, subs=subs)
            prev = self.posted("/agent/preview")[-1]
            self.assertEqual(prev["errors"], [])
            got = {(r["name"], r["target"]): (r["state"], r.get("create"), r.get("delete")) for r in prev["rows"]}
            self.assertEqual(got[("B-Krow", B)], ("new", 1, 0))
            self.assertEqual(got[("Study", "outlook")], ("new", 2, 0))
            self.assertEqual(got[("Study", A_ST)], ("new", 2, 0))
            self.assertEqual(got[("Holidays", B)], ("new", 4, 0))
            self.assertEqual(got[("携程", A_XC)], ("same", 0, 0))
            self.assertEqual(got[("B-Personal", B)][0], "stop")
            self.assertFalse(os.path.exists(os.path.join(self.dir, "managed", "preview-subs")))
            # the preview wrote nothing
            self.assertNotIn(A_ST, self.ctx.gcal.cal)

            self.cycle(confirmed={"id": 2, "body": draft, "preview": prev}, subs=subs)
            self.assertEqual(self.posted("/agent/applied")[-1], {"rev": 2, "ok": True})
            url_file = os.path.join(self.dir, "managed", "subs", "0123456789abcdef.url")
            self.assertEqual(oct(os.stat(url_file).st_mode & 0o777), "0o600")
            self.sync_round()
        self.assertEqual(len(self.ctx.gcal.cal[A_ST]), 2)
        self.assertTrue(any(c["name"] == "Study" for c in self.ctx.graph.cals))
        self.assertEqual([l["name"] for l in managed.local(self.cfg)["leftovers"]], ["B-Personal"])
        managed.upload_inventory(self.cfg, self.ctx, log=lambda *a: None)
        self.assertEqual(managed.local(self.cfg)["leftovers"], [])   # Personal has no events: nothing to clean, dropped from the uploaded list

        # drop Study and Holidays: events are kept, recorded as leftover events
        body3 = {"b_merge": ["Calendar", "Krow"], "mirrors": cur["mirrors"]}
        self.cycle(confirmed={"id": 3, "body": body3, "preview": {"rows": []}})
        self.assertEqual(self.posted("/agent/applied")[-1], {"rev": 3, "ok": True})
        self.assertFalse(os.listdir(os.path.join(self.dir, "managed", "subs")))   # link file deleted
        self.sync_round()
        self.assertEqual(len(self.ctx.gcal.cal[A_ST]), 2)                     # not deleted
        managed.upload_inventory(self.cfg, self.ctx, log=lambda *a: None)
        left = {(l["name"], l["target"]): l["count"] for l in self.posted("/agent/inventory")[-1]["leftovers"]}
        self.assertEqual(left, {("Study", "outlook"): 2, ("Study", A_ST): 2, ("Study", B): 2, ("Holidays", B): 4})

        # cleanup: the page shows 2 items
        self.cycle(cleanups=[{"id": 1, "kind": "mirror", "name": "Study", "target": A_ST, "count": 2},
                             {"id": 2, "kind": "mirror", "name": "Study", "target": "outlook", "count": 2}])
        self.assertEqual([c["ok"] for c in self.posted("/agent/cleanup")], [True, True])
        self.assertEqual(self.ctx.gcal.cal[A_ST], {})
        self.assertFalse(any(c["name"] == "Study" for c in self.ctx.graph.cals))
        self.assertEqual(len(self.ctx.gcal.list_marked(B)), 3 + 3 + 1 + 2 + 4)   # everything else in B untouched

    def test_apply_refused_when_more_deletes_than_preview(self):
        self.sync_round()
        self.seed()
        # At preview time Calendar still exists; before confirming, 12 events vanished from Outlook...
        # set up here: B-Calendar has 13 events, source becomes 0 after confirming
        g = self.ctx.graph
        g.ev["c-cal"] = {e["id"]: e for e in map(oev, range(13))}
        self.sync_round()
        body = managed.local(self.cfg)["body"]
        prev = {"rows": [{"kind": "sync", "name": "B-Calendar", "target": B, "delete": 0, "state": "same"}]}
        g.ev["c-cal"] = {}
        self.cycle(confirmed={"id": 2, "body": dict(body, b_merge=["Calendar"]), "preview": prev})
        r = self.posted("/agent/applied")[-1]
        self.assertFalse(r["ok"])
        self.assertIn("confirmed preview showed 0", " ".join(r["errors"]))
        self.assertEqual(managed.local(self.cfg)["rev"], 1)       # not applied

    def test_cap(self):
        sync.check_cap(None, ("sync", "x", "t"), 999)
        sync.check_cap({("sync", "x", "t"): 5}, ("sync", "x", "t"), 15)
        with self.assertRaises(sync.CapExceeded):
            sync.check_cap({("sync", "x", "t"): 5}, ("sync", "x", "t"), 16)

    def test_out_of_sync_reported(self):
        self.seed()
        errs = self.cycle(applied={"id": 5, "body": {}})
        self.assertIn("#5", errs[0])


if __name__ == "__main__":
    unittest.main()
