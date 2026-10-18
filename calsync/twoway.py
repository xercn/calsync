"""Account A: one Outlook calendar ↔ one dedicated Google calendar, two-way sync.

Rules:
- If only one side changed → update the other side; if both changed → the last modified wins (Outlook
  lastModifiedDateTime vs Google updated).
- Meetings organized by someone else (you are not the organizer in Outlook) and cancelled meetings: Outlook wins;
  changes / deletions in Google are reverted next round.
- Deleted on one side: if the other side was not changed → delete it too (subject to the delete guard); if the other
  side was changed → do not delete, recreate the deleted side instead (no changes are lost).
- Recurring events that are series on both sides: the master (rule, title, etc.) syncs by the rules above;
  single-instance changes / cancellations are paired and synced by original start time. For a series whose master
  changed this round, the instance layer is synced next round (when a series' times change as a whole, instance
  changes on both sides are reset, just as Outlook itself does).
- Recurring events created in Google only support common rules (see recur.py); unsupported ones are not synced and
  raise an alert.

Each side is compared only against the canonical hash it stored last time (state.py); after writing one side, both new
hashes are stored immediately, so it never flip-flops.
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import canon, recur, render
from .state import Pair

UTC = ZoneInfo("UTC")


def decide(*, have_o, have_g, o_changed, g_changed, o_newer, outlook_auth, equal=False):
    """What to do with a paired event this round. Returns:
    none / o2g (Outlook overwrites Google) / g2o / del_o / del_g / recreate_g / recreate_o / drop (gone on both
    sides)."""
    if have_o and have_g:
        if equal:
            return "none"
        if outlook_auth:
            return "o2g" if (o_changed or g_changed) else "none"
        if o_changed and g_changed:
            return "o2g" if o_newer else "g2o"
        if o_changed:
            return "o2g"
        if g_changed:
            return "g2o"
        return "none"
    if have_o:   # Google side is gone
        if outlook_auth or o_changed:
            return "recreate_g"
        return "del_o"
    if have_g:   # Outlook side is gone
        return "recreate_o" if g_changed else "del_g"
    return "drop"


def _aware(c, tz):
    if c["all_day"]:
        return datetime.combine(date.fromisoformat(c["start"]), datetime.min.time(), ZoneInfo(tz))
    return datetime.fromisoformat(c["start"]).replace(tzinfo=ZoneInfo(tz))


def _parse_dt(s):
    """Graph times carry 7 fractional digits and a Z; strip the fraction and add the timezone."""
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _inst_key(raw, all_day, tz):
    """Original start time → pairing key. raw: Outlook originalStart (string) or Google originalStartTime (dict)."""
    if isinstance(raw, dict):
        if "date" in raw:
            return raw["date"].replace("-", "")
        raw = raw["dateTime"]
    dt = _parse_dt(raw)
    if all_day:
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y%m%d")
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M")


def _outlook_auth(o):
    return o is not None and (o.get("isOrganizer") is False or bool(o.get("isCancelled")))


def _o_mod(o):
    return _parse_dt(o.get("lastModifiedDateTime") or "1970-01-01T00:00:00Z")


def _g_mod(g):
    return _parse_dt(g.get("updated") or "1970-01-01T00:00:00Z")


class Engine:
    def __init__(self, *, graph, gcal, state, cfg, mapping, mid, cal_id, narrow, wide, log=print, dry_run=False):
        self.graph, self.gcal, self.state, self.cfg = graph, gcal, state, cfg
        self.m, self.mid, self.cal_id, self.target = mapping, mid, cal_id, mapping.target
        self.narrow, self.wide, self.log, self.dry = narrow, wide, log, dry_run
        self.tz, self.otz = cfg.timezone, cfg.outlook_timezone
        self.errors = []
        self.actions = []       # (description, count category, callable)
        self.counts = {}

    # ---- canonical form

    def oc(self, o):
        return canon.from_outlook(o, self.tz)

    def gc(self, g):
        return canon.from_google(g, self.tz)

    def _label(self, c):
        return f"{c.get('title', '')!r} @ {c.get('start', '')}"

    def _err(self, msg):
        self.errors.append(f"{self.m.name}: {msg}")

    def _add(self, kind, fn, label=""):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.actions.append((kind, label, fn))

    # ---- write to Google (from Outlook)

    def _g_body(self, o, g_id):
        body = render.render(o, mid=self.mid, tz=self.tz, reminders=self.m.reminders)
        body["id"] = g_id
        c = self.oc(o)
        if c.get("rule"):
            body["recurrence"] = [c["rule"]]
        return body

    def _g_patch_body(self, o):
        full = self._g_body(o, "x")
        keep = ("summary", "location", "description", "start", "end", "transparency", "visibility",
                "extendedProperties", "source", "recurrence")
        return {k: full[k] for k in keep if k in full}

    def _store(self, o, g, kind):
        oc, gc = self.oc(o), self.gc(g)
        self.state.put_pair(self.mid, Pair(o["id"], g["id"], kind, canon.digest(oc), canon.digest(gc), oc["start"]))
        return oc, gc

    def create_g(self, o, kind):
        g = self.gcal.insert(self.target, self._g_body(o, render.event_id(self.mid, o["id"])))
        self._store(o, g, kind)

    def o2g(self, o, g, kind):
        g = self.gcal.patch(self.target, g["id"], self._g_patch_body(o))
        self._store(o, g, kind)
        if kind == "series":
            self._reset_instances(o["id"])

    # ---- write to Outlook (from Google)

    def _tag_google(self, g, o):
        """Tag events that came from Google, so they can be re-paired even if the state db is lost."""
        return self.gcal.patch(self.target, g["id"], {"extendedProperties": {"private": {
            "calsync": "1", "calsync_map": self.mid, "calsync_src": o["id"][:1000]}}})

    def create_o(self, g, kind):
        c = self.gc(g)
        body = canon.to_outlook(c, (*canon.FIELDS, "rule"), self.otz, g.get("recurrence"), self.tz)
        o = self.graph.create_event(self.cal_id, body, self.otz)
        g = self._tag_google(g, o)
        self._store(o, g, kind)

    def g2o(self, o, g, kind):
        gc, oc = self.gc(g), self.oc(o)
        fields = canon.diff(gc, oc)
        if kind == "series" and {"start", "end", "all_day", "rule"} & set(fields):
            fields.append("rule")      # series time change: send the rule too (range.startDate follows)
        body = canon.to_outlook(gc, fields, self.otz, g.get("recurrence"), self.tz)
        if body:
            o = self.graph.patch_event(o["id"], body, self.otz)
        self._store(o, g, kind)
        if kind == "series":
            self._reset_instances(o["id"])

    def _reset_instances(self, o_master):
        """The master was changed: changing the master in Google overwrites instance changes, Outlook does not.
        Clear the instance-layer state, so next round, instances that differ between the two sides follow Outlook's
        instance changes, and the instance changes are preserved."""
        self.state.drop_inst(self.mid, o_master)

    # ---- plan

    def plan(self):
        o_all = self.graph.calendar_view(self.cal_id, self.wide[0], self.wide[1], self.otz)
        o_items, o_inst = {}, {}
        for ev in o_all:
            if ev.get("type") in ("occurrence", "exception"):
                o_inst.setdefault(ev["seriesMasterId"], []).append(ev)
            else:
                o_items[ev["id"]] = ev
        for mid_ in o_inst:
            if mid_ not in o_items:
                master = self.graph.get_event(mid_, self.otz, self.cal_id)
                if master:
                    o_items[mid_] = master
        self.o_inst = o_inst
        g_items = {g["id"]: g for g in self.gcal.list_all(self.target, self.wide[0], self.wide[1])}

        pairs = self.state.pairs(self.mid)
        by_o = {p.o_id: p for p in pairs}
        by_g = {p.g_id: p for p in pairs}
        self.pair_count = len(pairs)

        for p in pairs:
            o, g = o_items.get(p.o_id), g_items.get(p.g_id)
            if p.kind == "single" and o is None and g is None:
                start = _aware({"all_day": len(p.start) == 10, "start": p.start}, self.tz)
                if not (self.narrow[0] <= start <= self.narrow[1]):
                    continue    # already outside the window, ignore
            if o is None:
                o = self.graph.get_event(p.o_id, self.otz, self.cal_id)
            if g is None:
                g = self.gcal.get(self.target, p.g_id)
            try:
                oc = self.oc(o) if o else None
                gc = self.gc(g) if g else None
            except recur.Unsupported as e:
                self._err(f"unsupported Google recurrence ({e}): {self._label(self.oc(o)) if o else p.g_id}")
                continue
            action = decide(
                have_o=o is not None, have_g=g is not None,
                o_changed=oc is not None and canon.digest(oc) != p.o_hash,
                g_changed=gc is not None and canon.digest(gc) != p.g_hash,
                o_newer=o is not None and g is not None and _o_mod(o) >= _g_mod(g),
                outlook_auth=_outlook_auth(o),
                equal=oc is not None and oc == gc,
            )
            label = self._label(oc or gc or {})
            if action == "none":
                if o and g and oc == gc and not self.dry and (canon.digest(oc), canon.digest(gc)) != (p.o_hash, p.g_hash):
                    self._store(o, g, p.kind)
                if p.kind == "series" and o and g:
                    self._plan_instances(p, o, g)
                continue
            if action == "o2g":
                self._add("~g", lambda o=o, g=g, k=p.kind: self.o2g(o, g, k), label)
            elif action == "g2o":
                self._add("~o", lambda o=o, g=g, k=p.kind: self.g2o(o, g, k), label)
            elif action == "del_o":
                self._add("-o", lambda p=p: (self.graph.delete_event(p.o_id, self.otz),
                                             self.state.drop_pair(self.mid, p.o_id)), label)
            elif action == "del_g":
                self._add("-g", lambda p=p: (self.gcal.delete(self.target, p.g_id),
                                             self.state.drop_pair(self.mid, p.o_id)), label)
            elif action == "recreate_g":
                self._add("+g", lambda o=o, k=p.kind: self.create_g(o, k), label)
            elif action == "recreate_o":
                self._add("+o", lambda p=p, g=g: (self.state.drop_pair(self.mid, p.o_id),
                                                  self.create_o(g, p.kind)), label)
            elif action == "drop" and not self.dry:
                self.state.drop_pair(self.mid, p.o_id)

        # events in Google carrying this mapping's tag but missing from the state db (state db lost): recover via the
        # Outlook ID in the tag
        tagged = {}
        for g_id, g in g_items.items():
            props = (g.get("extendedProperties") or {}).get("private") or {}
            if g_id not in by_g and props.get("calsync_map") == self.mid and props.get("calsync_src"):
                tagged[props["calsync_src"]] = g_id

        # newly appeared Outlook events
        for o_id, o in o_items.items():
            if o_id in by_o:
                continue
            kind = "series" if o.get("type") == "seriesMaster" else "single"
            g_id = tagged.pop(o_id, None)
            if g_id:     # synced before, state db lost: re-pair, Outlook wins
                g = g_items.pop(g_id)
                self._add("~g", lambda o=o, g=g, k=kind: self.o2g(o, g, k), self._label(self.oc(o)))
                continue
            if kind == "single" and not render.overlaps(o, self.tz, *self.narrow):
                continue
            self._add("+g", lambda o=o, k=kind: self.create_g(o, k), self._label(self.oc(o)))

        # newly appeared Google events
        for g_id, g in g_items.items():
            if g_id in by_g:
                continue
            props = (g.get("extendedProperties") or {}).get("private") or {}
            kind = "series" if g.get("recurrence") else "single"
            if props.get("calsync_map") == self.mid:
                src = props.get("calsync_src")
                if src and src not in o_items and src not in by_o and self.graph.get_event(src, self.otz, self.cal_id) is None:
                    # state db lost, and the Outlook event is also deleted
                    self._add("-g", lambda g=g: self.gcal.delete(self.target, g["id"]), g.get("summary", ""))
                continue
            try:
                c = self.gc(g)
            except recur.Unsupported as e:
                self._err(f"unsupported Google recurrence ({e}): {g.get('summary', '')!r} — not synced to Outlook")
                continue
            if kind == "single" and not (self.narrow[0] <= _aware(c, self.tz) <= self.narrow[1]):
                continue
            self._add("+o", lambda g=g, k=kind: self.create_o(g, k), self._label(c))

    # ---- single instances within series

    def _plan_instances(self, p, o_master, g_master):
        all_day = bool(o_master.get("isAllDay"))
        O = {_inst_key(e["originalStart"], all_day, self.tz): e for e in self.o_inst.get(p.o_id, [])
             if e.get("originalStart")}
        G = {_inst_key(e["originalStartTime"], all_day, self.tz): e
             for e in self.gcal.instances(self.target, p.g_id, self.wide[0], self.wide[1])}
        stored = self.state.insts(self.mid, p.o_id)
        auth = _outlook_auth(o_master)
        for key in set(O) | set(G):
            o, g = O.get(key), G.get(key)
            oc = self.oc(o) if o else None
            gc = self.gc(g) if g else None
            ref = oc or gc
            if not (self.narrow[0] <= _aware(ref, self.tz) <= self.narrow[1]):
                continue
            label = self._label(ref)
            if o and g:
                oh, gh = canon.digest(oc), canon.digest(gc)
                if oc == gc:
                    if stored.get(key) != (oh, gh) and not self.dry:
                        self.state.put_inst(self.mid, p.o_id, key, oh, gh)
                    continue
                st = stored.get(key)
                if st is None:
                    o_wins = o.get("type") == "exception"
                else:
                    o_ch, g_ch = oh != st[0], gh != st[1]
                    if not (o_ch or g_ch):
                        continue
                    o_wins = auth or (o_ch and (not g_ch or _o_mod(o) >= _g_mod(g)))
                if o_wins or auth:
                    self._add("~gi", lambda p=p, key=key, o=o, g=g: self._inst_o2g(p, key, o, g), label)
                else:
                    self._add("~oi", lambda p=p, key=key, o=o, g=g: self._inst_g2o(p, key, o, g), label)
            elif o:      # this instance was cancelled in Google
                if auth:
                    self._add("+gi", lambda p=p, key=key, o=o: self._inst_restore_g(p, key, o, all_day), label)
                else:
                    self._add("-oi", lambda p=p, key=key, o=o: (self.graph.delete_event(o["id"], self.otz),
                                                               self.state.drop_inst(self.mid, p.o_id, key)), label)
            else:        # this instance was cancelled in Outlook
                self._add("-gi", lambda p=p, key=key, g=g: (self.gcal.delete(self.target, g["id"]),
                                                           self.state.drop_inst(self.mid, p.o_id, key)), label)

    def _inst_o2g(self, p, key, o, g):
        body = render.render(o, mid=self.mid, tz=self.tz, reminders=self.m.reminders)
        patch = {k: body[k] for k in ("summary", "location", "description", "start", "end", "transparency", "visibility")}
        g = self.gcal.patch(self.target, g["id"], patch)
        self.state.put_inst(self.mid, p.o_id, key, canon.digest(self.oc(o)), canon.digest(self.gc(g)))

    def _inst_g2o(self, p, key, o, g):
        gc = self.gc(g)
        body = canon.to_outlook(gc, canon.diff(gc, self.oc(o)), self.otz)
        if body:
            o = self.graph.patch_event(o["id"], body, self.otz)
        self.state.put_inst(self.mid, p.o_id, key, canon.digest(self.oc(o)), canon.digest(gc))

    def _inst_restore_g(self, p, key, o, all_day):
        inst_id = f"{p.g_id}_{key}" if all_day else f"{p.g_id}_{key}00Z"
        g = self.gcal.patch(self.target, inst_id, {"status": "confirmed"})
        self._inst_o2g(p, key, o, g)

    # ---- delete guard + execution

    def guard(self):
        """Block when too much would be deleted on one side (whole events + instances); returns the reason."""
        base = max(self.pair_count, 1)
        for side, kinds in (("Outlook", ("-o", "-oi")), ("Google", ("-g", "-gi"))):
            n = sum(self.counts.get(k, 0) for k in kinds)
            if n > self.cfg.guard_min and n > base * self.cfg.guard_ratio:
                return f"would delete {n} {side} events (pairs: {self.pair_count})"
        return None

    def summary(self):
        # + created, ~ modified, - deleted; o / g = written to Outlook / Google; an "i" suffix means a single instance
        # within a series
        order = ["+o", "+g", "~o", "~g", "-o", "-g", "~oi", "~gi", "-oi", "-gi", "+gi"]
        parts = [f"{k}={self.counts[k]}" for k in order if self.counts.get(k)]
        return " ".join(parts) or "no changes"

    def execute(self, verbose=False):
        for kind, label, fn in self.actions:
            if verbose:
                self.log(f"    {kind} {label}")
            try:
                fn()
            except recur.Unsupported as e:
                self._err(f"unsupported recurrence ({e}): {label}")
            except Exception as e:  # a single failure does not affect the others
                self._err(f"{kind} {label}: {e.__class__.__name__}: {str(e)[:200]}")
