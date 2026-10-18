# calsync

Self-hosted calendar sync between **Outlook (personal Microsoft account)**, **Google Calendar**, **iCloud** and
**ICS subscription links**, with an optional web page (Cloudflare Worker + Cloudflare Access) to choose what gets
synced and to preview every change before it is applied.

- Pure Python 3.12 standard library + `requests` + `cryptography` (both usually shipped by the distro). No virtualenv, no pip.
- Runs as a locked-down systemd service on any small Linux VPS. It opens no ports; it only makes outbound HTTPS calls.
- Built for one household's calendars, but nothing in the code is tied to a particular account.

## What it does

| Mapping | Direction | Typical use |
|---|---|---|
| `[[sync]]` `mode = "two-way"` | Outlook ↔ dedicated Google calendar | keep an Outlook calendar and a Google calendar identical, edit on either side |
| `[[sync]]` `mode = "one-way"` | Outlook → Google | merge several Outlook calendars into one Google calendar (e.g. a partner's primary calendar) |
| `[[mirror]]` | iCloud calendar or ICS link → Outlook + Google | read-only sources: travel bookings, holidays, shared family calendars |

Details copied: title, time, location, body, organizer, attendees (with response status), online-meeting link.
Attendees are written **into the description only** — never into Google's attendee list, so nobody gets invited.
Every Google write uses `sendUpdates=none`.

### Two-way rules

- Only one side changed → copy it to the other side. Both changed → **last modified wins**
  (Outlook `lastModifiedDateTime` vs Google `updated`).
- Meetings organized by someone else, and cancelled meetings: **Outlook wins**; edits/deletes made in Google are reverted next round.
- Deleted on one side and untouched on the other → deleted on both (subject to the mass-delete guard).
  Deleted on one side but edited on the other → the deleted side is recreated (edits are never lost).
- Recurring events stay series on both sides. Single-occurrence edits and cancellations are paired by their original start time.
  Common Google recurrence rules are translated to Outlook patterns (daily / weekly / monthly by date or weekday incl. "last" /
  yearly, with interval, until, count). Anything else is reported and left alone.
- State lives in `state.db` (SQLite): pairs plus a canonical hash of each side, so formatting noise never ping-pongs.
  If it is lost, events are re-paired via markers stored on the Google events.

### Read-only sources (`[[mirror]]`)

- Source: an iCloud calendar (CalDAV with an app-specific password) **or** a `webcal://` / `https://` ICS link
  (the link is a secret; it is read from a file, never from the config).
- Targets: an Outlook calendar **created and owned by calsync** (it never adopts an existing calendar with the same name),
  and any number of Google calendars.
- Recurrences are expanded inside the window (needs `python3-dateutil`), with EXDATE and RECURRENCE-ID overrides.
- A source that suddenly returns nothing, or cannot be fetched, leaves all targets untouched and raises an alert.

### Safety

- **Window**: 30 days back to 365 days ahead (configurable). Events that slide out of the window are left alone.
- **Ownership markers**: one-way targets only touch events carrying calsync's private marker, so you can share a calendar
  with calsync and keep adding your own events to it.
- **Mass-delete guard**: if one mapping would delete more than 10 events **and** more than 50 % of what it manages,
  that mapping is skipped for the round and an alert is sent. Override once with `calsync run --only NAME --force`.
- **Alerts**: optional HTTP push endpoint (`[notify]`): one message when a run starts failing, a reminder every 24 h while
  it keeps failing, one message when it recovers.

## The config page (optional)

`worker/` is a Cloudflare Worker with its own D1 database:

- Pick which iCloud calendars to mirror (to Outlook / Google account A / merged into account B), which Outlook calendars
  to merge into account B, and add ICS subscription links (stored AES-GCM encrypted; the page only ever shows "set").
- **Every change is previewed first.** Saving creates a draft; the server computes a dry run (`+ ~ -` per target) and
  sends it back; nothing happens until you press *Confirm*. Before applying, the server dry-runs again and refuses if any
  target would now delete more than the preview showed plus 10.
- Removing a calendar never deletes what was already synced. Leftovers are listed and removed only when you press *Clean up*.
- Sync interval (5 / 10 / 15 / 30 minutes) and a *Sync now* button.
- Login is **Cloudflare Access** (e.g. email one-time PIN). The Worker also verifies the Access JWT itself and rejects
  everything if Access is not configured (fail closed).
- The server talks to the Worker outbound only (`/agent/*`, bearer key; the Worker stores only its SHA-256).
  If the Worker is unreachable, the last applied settings keep running; after 24 h an alert is raised.

Without `[managed]` in the config, calsync is configured purely by `config.toml`.

## Setup

### 1. Microsoft app registration (personal account)

1. Sign in to <https://entra.microsoft.com> with the Outlook account → *App registrations* → *New registration*;
   supported account types: **Personal Microsoft accounts only**; no redirect URI.
2. *Authentication* → *Allow public client flows* → **Yes**.
3. *API permissions* → Microsoft Graph → *Delegated* → `Calendars.ReadWrite` (or `Calendars.Read` if you only use one-way).
4. Copy the *Application (client) ID* into `[outlook] client_id` (it is not a secret).

### 2. Google service account

1. In <https://console.cloud.google.com> create a project, enable the **Google Calendar API**, create a service account
   (no roles needed) and a JSON key.
2. Share every target calendar with the service account's email, permission **Make changes to events**.
   A service account cannot create calendars inside your account: create dedicated calendars yourself (or with any
   Calendar API client), then share them.
3. Put the calendar IDs (`…@group.calendar.google.com`, or the Gmail address for a primary calendar) into the config.

### 3. Install on the server

```bash
CALSYNC_HOST=root@your-server CALSYNC_PORT=22 CALSYNC_KEY=~/.ssh/id_ed25519 tools/install.sh           # dry run
CALSYNC_HOST=root@your-server CALSYNC_PORT=22 CALSYNC_KEY=~/.ssh/id_ed25519 tools/install.sh --apply   # install
```

`install.sh` installs the **committed** version (`git archive HEAD`) into `/opt/calsync`, creates the `calsync` user,
`/etc/calsync` (root:calsync 750) and `/var/lib/calsync` (calsync 700), and the systemd units. Then on the server:

```bash
cp /opt/calsync/config.example.toml /etc/calsync/config.toml    # edit it
/opt/calsync/deploy/calsync-cli login                            # Outlook device-code login, once
/opt/calsync/deploy/calsync-cli calendars                        # check names and access
/opt/calsync/deploy/calsync-cli run --dry-run -v                 # see what would happen
/opt/calsync/deploy/calsync-cli run
```

When the first real run looks right, enable the scheduler from your workstation: `tools/install.sh --apply --enable`.
`calsync-poll.timer` runs every minute; it starts a full sync when the interval is due (default 10 minutes), when
*Sync now* was pressed, or right after new settings were applied.

### 4. Config page (optional)

1. `cd worker && npm install`, create the D1 database (`npx wrangler d1 create calsync`), put its id and your hostname into
   `wrangler.jsonc` and `scripts/deploy.conf`.
2. Secrets, piped so they never appear on a command line:
   `AGENT_KEY_SHA256` (SHA-256 hex of a random server key) and `SUB_KEY` (32 random bytes, base64url).
   Copy the server key to `/etc/calsync/manage.key` (root:calsync 640).
3. `scripts/deploy.sh` (dry run) → `scripts/deploy.sh --apply` (backs up D1, runs migrations, deploys, smoke-tests,
   rolls back automatically if the smoke test fails).
4. In Cloudflare Zero Trust: add the *One-time PIN* login method, create a self-hosted Access application for your hostname
   with a single *Allow* policy for your email, and put the team domain and the application's AUD tag into `wrangler.jsonc`.
5. Add `[managed]` to `config.toml` (see `config.example.toml`). The next run uploads the calendar inventory and imports the
   current config as version 1 without changing any event.

## Day to day

- Logs: `journalctl -u calsync-poll -n 50` (counts per mapping and errors; `-v` on a manual run prints each change).
- Run once by hand: `systemctl start calsync` or `calsync-cli run`.
- Removed a `[[sync]]` / changed its target: old events stay; `calsync-cli orphans` lists them, `orphans --apply` deletes them.
- Outlook login expired (alert says so): `calsync-cli login`.
- Upgrade: commit → `tools/install.sh --apply`. Roll back: `/opt/calsync.prev` holds the previous version.
- Tests: `python3 -m unittest discover -s tests`. `tools/e2e_test.py` runs a full two-way scenario against a pair of
  dedicated test calendars (it empties them first).

## Files on the server

| Path | Content | Mode |
|---|---|---|
| `/opt/calsync/` | code (`VERSION` = commit) | root |
| `/etc/calsync/config.toml` | config | root:calsync 640 |
| `/etc/calsync/google-sa.json` | Google service-account key | root:calsync 640 |
| `/etc/calsync/icloud.pass` | iCloud app-specific password | root:calsync 640 |
| `/etc/calsync/notify.key`, `manage.key` | alert endpoint key, config-page key | root:calsync 640 |
| `/var/lib/calsync/ms_token.json` | Outlook refresh token (rotates on every refresh) | calsync 600 |
| `/var/lib/calsync/state.db` | two-way pairs and hashes, calsync-owned Outlook calendars | calsync |
| `/var/lib/calsync/managed/` | settings generated by the config page, subscription links (600), schedule | calsync 700 |

## Cost of the sync interval

One full round for ~17 mappings takes about 10 s and ~40 requests (Graph 18, Google 18, iCloud 4).
At 5 minutes that is ~5.5k Graph and ~5.2k Google requests a day — far below Graph's 10,000 per 10 minutes per mailbox
and Google's default 1,000,000 per day. The limits to respect are the unpublished ones: iCloud (every CalDAV request
authenticates with the app password) and third-party ICS feeds, which Google and Outlook themselves fetch only every
few hours. Hence no option below 5 minutes.

## Notes

- Code comments are English; the config page UI is currently Chinese.
- This repository is a periodically exported copy of a private working repository, so history is coarse.

## License

MIT — see [LICENSE](LICENSE).
