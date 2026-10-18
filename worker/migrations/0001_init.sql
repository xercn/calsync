-- calsync config page (calsync.example.com)
-- revs: each "Save and preview" creates a new revision whose content is never edited again;
-- status goes draft → confirmed → applied (or failed / discarded / superseded)
CREATE TABLE revs (
  id           INTEGER PRIMARY KEY,
  body         TEXT NOT NULL,          -- {"b_merge": [...], "mirrors": [...]}
  base         INTEGER,                -- the applied revision this one was based on
  status       TEXT NOT NULL,
  seed         INTEGER NOT NULL DEFAULT 0,  -- 1 = first import of the server's existing config (no preview needed)
  created_at   INTEGER NOT NULL,
  created_by   TEXT,
  preview      TEXT,                   -- preview computed by the server {rows, errors}
  preview_at   INTEGER,
  confirmed_at INTEGER,
  applied_at   INTEGER,
  result       TEXT                    -- execution result / failure reason
);
CREATE INDEX revs_status ON revs (status, id);

-- Subscription links: AES-GCM (key in Worker secret SUB_KEY); the page only shows "already set"
CREATE TABLE subs (
  id         TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  iv         TEXT NOT NULL,
  ct         TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- Things uploaded by the server (inventory = selectable calendar list + last sync result + leftover events), plus flags
CREATE TABLE meta (
  k  TEXT PRIMARY KEY,
  v  TEXT NOT NULL,
  at INTEGER NOT NULL
);

-- Cleanup of "events left behind after sync stopped": created only when clicked on the page, executed by the server
CREATE TABLE cleanups (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,          -- sync / mirror
  name         TEXT NOT NULL,
  target       TEXT NOT NULL,          -- outlook / Google calendar ID
  count        INTEGER NOT NULL,       -- count shown on the page at click time (not deleted if the real count exceeds it by more than 10)
  status       TEXT NOT NULL,          -- pending / done / failed
  requested_at INTEGER NOT NULL,
  done_at      INTEGER,
  result       TEXT
);

CREATE TABLE audit (
  id     INTEGER PRIMARY KEY,
  at     INTEGER NOT NULL,
  who    TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
