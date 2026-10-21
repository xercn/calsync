"""Command line: python3 -m calsync [--config PATH] <login|calendars|run|poll|orphans|test-notify|icloud-calendars>"""
import argparse
import fcntl
import os
import sys

from . import config, managed, mirror, notify, render, sync, twoway
from .caldav import ICloud
from .gcal import GCal
from .http import HttpError
from .msgraph import Graph, LoginRequired
from .state import State


def log(msg):
    print(msg, flush=True)


def _graph(cfg):
    return Graph(cfg.outlook_client_id, cfg.token_file or os.path.join(cfg.state_dir, "ms_token.json"))


def _icloud(cfg):
    if not cfg.icloud_apple_id:
        return None
    with open(cfg.icloud_password_file) as f:
        return ICloud(cfg.icloud_apple_id, f.read().strip())


def cmd_icloud_calendars(cfg, args):
    ic = _icloud(cfg)
    if ic is None:
        log("no [icloud] section in config")
        return 1
    for c in ic.calendars():
        log(f"  {c['name']}")


def _resolve(cals, mapping):
    hits = [c for c in cals if c["name"] == mapping.source]
    if not hits:
        raise LookupError(f"Outlook calendar not found: {mapping.source!r}")
    if len(hits) > 1:
        raise LookupError(f"Outlook calendar name is ambiguous: {mapping.source!r}")
    if mapping.mode == "two-way" and not hits[0].get("canEdit"):
        raise LookupError(f"Outlook calendar {mapping.source!r} is read-only; it cannot be two-way")
    return hits[0]["id"]


def _run_two_way(cfg, args, graph, gcal, m, cal_id, mid, narrow, wide):
    eng = twoway.Engine(graph=graph, gcal=gcal, state=State(os.path.join(cfg.state_dir, "state.db")),
                        cfg=cfg, mapping=m, mid=mid, cal_id=cal_id, narrow=narrow, wide=wide,
                        log=log, dry_run=args.dry_run)
    eng.plan()
    reason = eng.guard()
    if reason and not args.force:
        raise sync.GuardTripped(reason)
    log(f"{m.name} [two-way]: {eng.summary()}{' [dry-run]' if args.dry_run else ''}")
    if args.dry_run:
        if args.verbose:
            for kind, label, _ in eng.actions:
                log(f"    {kind} {label}")
    else:
        eng.execute(verbose=args.verbose)
    return eng.errors


def cmd_login(cfg, args):
    _graph(cfg).login(out=log)
    log("Outlook login OK; token saved.")


def cmd_calendars(cfg, args):
    cals = _graph(cfg).calendars()
    log("Outlook calendars:")
    for c in cals:
        flag = (" (default)" if c.get("isDefaultCalendar") else "") + ("" if c.get("canEdit") else " (read-only)")
        owner = ((c.get("owner") or {}).get("address")) or ""
        log(f"  {c['name']}{flag}  owner={owner}")
    try:
        gcal = GCal(cfg.google_key_file)
    except OSError as e:
        log(f"\nGoogle key not readable: {e}")
        return 1
    log(f"\nGoogle service account: {gcal.email}")
    log("Configured mappings:")
    for m in cfg.mappings:
        try:
            _resolve(cals, m)
            src = "ok"
        except LookupError as e:
            src = str(e)
        err = gcal.check_access(m.target)
        log(f"  {m.name}: outlook {src}; google {'ok' if err is None else err + ' (shared with the service account?)'}")


def _lock(cfg):
    """Only one run at a time (shared by the scheduled sync and config page polling; the Outlook token changes on
    every refresh, so no concurrency). Returns None if the lock cannot be taken."""
    lock = open(os.path.join(cfg.state_dir, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


class Ctx:
    """Connections shared within one round."""

    def __init__(self, cfg):
        self.graph = _graph(cfg)
        self.gcal = GCal(cfg.google_key_file)
        self.cals = self.graph.calendars()
        self._ic = False
        self.cfg = cfg

    def icloud(self):
        if self._ic is False:
            self._ic = _icloud(self.cfg)
        return self._ic


def sync_all(cfg, ctx, *, dry_run, force=False, verbose=False, only=None, caps=None):
    """Run all mappings (or only those in `only`). → (errors, rows); rows has one row per target {kind, name, target,
    create, update, delete, existing, error}."""
    errors, rows = [], []
    graph, gcal, cals = ctx.graph, ctx.gcal, ctx.cals
    narrow, wide = sync.windows(cfg)
    only = set(only or [])
    for m in cfg.mappings:
        if only and m.name not in only:
            continue
        before = len(errors)
        row = {"kind": "two-way" if m.mode == "two-way" else "sync", "name": m.name, "target": m.target}
        rows.append(row)
        try:
            cal_id = _resolve(cals, m)
            mid = render.map_id(cal_id, m.target)
            if m.mode == "two-way":
                ns = argparse.Namespace(dry_run=dry_run, force=force, verbose=verbose)
                errors += _run_two_way(cfg, ns, graph, gcal, m, cal_id, mid, narrow, wide)
                if len(errors) > before:
                    row["error"] = errors[before]
                    log("ERROR " + "\nERROR ".join(errors[before:]))
                continue
            src = graph.calendar_view(cal_id, wide[0], wide[1], cfg.outlook_timezone)
            dst = gcal.list_marked(m.target, map_id=mid, time_min=narrow[0], time_max=narrow[1])
            p = sync.plan(src, dst, mid=mid, mapping=m, cfg=cfg, narrow=narrow)
            row.update(create=len(p.creates), update=len(p.updates), delete=len(p.deletes), existing=p.existing)
            sync.check_cap(caps, ("sync", m.name, m.target), len(p.deletes))
            reason = sync.check_guard(p, cfg)
            if reason and not force:
                raise sync.GuardTripped(reason)
            log(f"{m.name}: {p.summary()}{' [dry-run]' if dry_run else ''}")
            if verbose:
                for b in p.creates:
                    log(f"    + {b['start']} {b['summary']}")
                for b in p.updates:
                    log(f"    ~ {b['start']} {b['summary']}")
                for g in p.deletes:
                    log(f"    - {g}")
            if not dry_run:
                sync.apply(p, gcal, m.target)
        except sync.GuardTripped as e:
            errors.append(f"{m.name}: {e}; skipped (run with --force after checking)")
        except LoginRequired as e:
            errors.append(str(e))
            row["error"] = str(e)
            break
        except (sync.CapExceeded, LookupError, HttpError, OSError, ValueError, KeyError) as e:
            errors.append(f"{m.name}: {e.__class__.__name__}: {e}")
        if len(errors) > before:
            row.setdefault("error", errors[-1].split(": ", 1)[-1])
            log("ERROR " + errors[-1])

    mirrors = [x for x in cfg.mirrors if not only or x.name in only]
    if mirrors:
        try:
            ic = ctx.icloud()
        except OSError as e:
            ic = None
            errors.append(f"icloud: {e}")
        st = State(os.path.join(cfg.state_dir, "state.db"))
        for mir in mirrors:
            errs = mirror.run(mir=mir, cfg=cfg, graph=graph, gcal=gcal, icloud=ic, state=st, narrow=narrow,
                              wide=wide, dry_run=dry_run, force=force, verbose=verbose, log=log, rows=rows, caps=caps)
            for e in errs:
                log("ERROR " + e)
            errors += errs
    return errors, rows


def _setup(cfg, dry_run):
    """→ (ctx, None) or (None, error description)."""
    try:
        return Ctx(cfg), None
    except LoginRequired as e:
        return None, str(e)
    except (HttpError, OSError, ValueError) as e:
        return None, f"setup: {e}"


def _full_run(cfg, ctx, errors):
    """A full sync round + alerts + record the time + upload the inventory. → errors."""
    errs, rows = sync_all(cfg, ctx, dry_run=False)
    errors = errors + errs
    notify.report(cfg, errors, log)
    managed.record_run(cfg)
    if cfg.managed_url:
        managed.after_run(cfg, ctx, errors, rows, log=log)
    return errors


def cmd_run(cfg, args):
    lock = _lock(cfg)
    if lock is None:
        log("another run is in progress; exiting")
        return 0
    full = not args.dry_run and not args.only
    ctx, err = _setup(cfg, args.dry_run)
    if err:
        log("ERROR " + err)
        if full:
            notify.report(cfg, [err], log)
            managed.record_run(cfg)
        return 1
    errors = []
    if full and cfg.managed_url:          # first check the config page for work (confirmed settings, dry-run, cleanup)
        cfg, errs = managed.cycle(cfg, ctx, log=log)
        errors += errs
    if full:
        errors = _full_run(cfg, ctx, errors)
    else:
        errors, _ = sync_all(cfg, ctx, dry_run=args.dry_run, force=args.force, verbose=args.verbose, only=args.only)
    return 1 if errors else 0


def cmd_poll(cfg, args):
    """Once a minute (calsync-poll.timer, the only timer): ask the config page whether anything is pending; run a full
    sync round when the interval set on the page (default 10 minutes) is up, "Sync now" was clicked on the page, or
    settings have just taken effect. If the page is unreachable, sync as usual at the last known interval."""
    lock = _lock(cfg)
    if lock is None:
        return 0
    p, errors = None, []
    if cfg.managed_url:
        try:
            p = managed.Api(cfg).poll()
            managed.poll_ok(cfg)
        except managed.ApiError as e:
            managed.poll_failed(cfg, e, log=log)
            stale = managed.stale_error(cfg)
            errors += [stale] if stale else []
    minutes = managed.interval(cfg, p)
    now_req = p.get("sync_now") if p else None
    is_due = managed.due(cfg, minutes)
    if not (is_due or now_req or (p and managed.has_work(cfg, p))):
        return 0
    ctx, err = _setup(cfg, False)
    if err:
        log("ERROR " + err)
        if is_due or now_req:             # only a failed full-sync attempt counts towards alerts
            notify.report(cfg, errors + [err], log)
            managed.record_run(cfg)       # retry at the interval, do not hit it every minute
        return 1
    new_cfg = cfg
    if p:
        new_cfg, errs = managed.cycle(cfg, ctx, poll=p, log=log)
        errors += errs
    if is_due or now_req or new_cfg is not cfg:   # also sync right away when settings have just taken effect
        errors = _full_run(new_cfg, ctx, errors)
        if now_req:
            managed.ack_sync(new_cfg, now_req, log)
    return 1 if errors else 0


def cmd_orphans(cfg, args):
    """Delete events left behind by mappings no longer in the config (e.g. target changed, a [[sync]] removed)."""
    cals = _graph(cfg).calendars()
    gcal = GCal(cfg.google_key_file)
    live = {}
    for m in cfg.mappings:
        live.setdefault(m.target, set()).add(render.map_id(_resolve(cals, m), m.target))
    for target, mids in live.items():
        marked = gcal.list_marked(target)
        stale = {gid: p.get("calsync_map") for gid, p in marked.items() if p.get("calsync_map") not in mids}
        log(f"{target}: {len(marked)} marked, {len(stale)} orphaned")
        if args.apply:
            for gid in stale:
                gcal.delete(target, gid)
            log(f"  deleted {len(stale)}")


def cmd_test_notify(cfg, args):
    ok = notify.send(cfg, "low", "Calendar sync test", "Test message from calsync.", log)
    log("sent" if ok else "not sent")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="calsync", description="Outlook → Google calendar one-way sync")
    ap.add_argument("--config", help=f"config file (default: $CALSYNC_CONFIG or {config.DEFAULT_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Outlook device-code login (once)")
    sub.add_parser("calendars", help="list Outlook calendars and check configured mappings")
    r = sub.add_parser("run", help="sync all mappings")
    r.add_argument("--dry-run", action="store_true", help="compare only, write nothing")
    r.add_argument("--only", action="append", metavar="NAME", help="only this [[sync]] name (repeatable)")
    r.add_argument("--force", action="store_true", help="ignore the mass-delete guard")
    r.add_argument("-v", "--verbose", action="store_true", help="print each change (includes titles)")
    o = sub.add_parser("orphans", help="find events left by removed mappings")
    o.add_argument("--apply", action="store_true", help="delete them")
    sub.add_parser("test-notify", help="send a low-level test message to /notify")
    sub.add_parser("icloud-calendars", help="list iCloud calendars (needs [icloud] in config)")
    sub.add_parser("poll", help="check the config page (calsync-poll.timer, every minute)")
    args = ap.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        log(f"config error: {e}")
        return 2
    if not os.path.isdir(cfg.state_dir) or not os.access(cfg.state_dir, os.W_OK):
        log(f"state_dir missing or not writable: {cfg.state_dir}")
        return 2
    handler = {"login": cmd_login, "calendars": cmd_calendars, "run": cmd_run,
               "orphans": cmd_orphans, "test-notify": cmd_test_notify,
               "icloud-calendars": cmd_icloud_calendars, "poll": cmd_poll}[args.cmd]
    try:
        return handler(cfg, args) or 0
    except LoginRequired as e:
        log(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
