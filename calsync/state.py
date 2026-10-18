"""Pairing state for two-way sync: state_dir/state.db (SQLite).

pairs: one Outlook event (single or series master) <-> one Google event, plus each side's canonical hash
after the last sync.
inst: a single occurrence within a series (paired by original start time), with each side's hash; used to sync
single-occurrence changes and cancellations.
If this database is lost: events from Outlook can be re-paired through their fixed Google IDs; events from Google
will be duplicated in Outlook.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
  map    TEXT NOT NULL,
  o_id   TEXT NOT NULL,
  g_id   TEXT NOT NULL,
  kind   TEXT NOT NULL,          -- single / series
  o_hash TEXT NOT NULL,
  g_hash TEXT NOT NULL,
  start  TEXT NOT NULL,          -- canonical start, used to decide whether it is inside the time window
  PRIMARY KEY (map, o_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS pairs_g ON pairs (map, g_id);
CREATE TABLE IF NOT EXISTS inst (
  map    TEXT NOT NULL,
  o_master TEXT NOT NULL,
  key    TEXT NOT NULL,          -- original start time (all-day: local date; otherwise UTC 'YYYYMMDDTHHMM')
  o_hash TEXT NOT NULL,
  g_hash TEXT NOT NULL,
  PRIMARY KEY (map, o_master, key)
);
CREATE TABLE IF NOT EXISTS mirror_cal (  -- Outlook calendars created by calsync
  map    TEXT PRIMARY KEY,
  o_cal  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_o (   -- read-only source -> Outlook calendar of the same name
  map    TEXT NOT NULL,
  src_id TEXT NOT NULL,
  o_id   TEXT NOT NULL,
  hash   TEXT NOT NULL,
  PRIMARY KEY (map, src_id)
);
"""


class Pair:
    __slots__ = ("o_id", "g_id", "kind", "o_hash", "g_hash", "start")

    def __init__(self, o_id, g_id, kind, o_hash, g_hash, start):
        self.o_id, self.g_id, self.kind = o_id, g_id, kind
        self.o_hash, self.g_hash, self.start = o_hash, g_hash, start


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    def pairs(self, mid):
        rows = self.db.execute("SELECT o_id, g_id, kind, o_hash, g_hash, start FROM pairs WHERE map = ?", (mid,))
        return [Pair(*r) for r in rows]

    def put_pair(self, mid, p):
        self.db.execute("DELETE FROM pairs WHERE map = ? AND g_id = ? AND o_id != ?", (mid, p.g_id, p.o_id))
        self.db.execute("INSERT OR REPLACE INTO pairs VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (mid, p.o_id, p.g_id, p.kind, p.o_hash, p.g_hash, p.start))
        self.db.commit()

    def drop_pair(self, mid, o_id):
        self.db.execute("DELETE FROM pairs WHERE map = ? AND o_id = ?", (mid, o_id))
        self.db.execute("DELETE FROM inst WHERE map = ? AND o_master = ?", (mid, o_id))
        self.db.commit()

    def insts(self, mid, o_master):
        rows = self.db.execute("SELECT key, o_hash, g_hash FROM inst WHERE map = ? AND o_master = ?", (mid, o_master))
        return {k: (oh, gh) for k, oh, gh in rows}

    def put_inst(self, mid, o_master, key, o_hash, g_hash):
        self.db.execute("INSERT OR REPLACE INTO inst VALUES (?, ?, ?, ?, ?)", (mid, o_master, key, o_hash, g_hash))
        self.db.commit()

    def drop_inst(self, mid, o_master, key=None):
        if key is None:
            self.db.execute("DELETE FROM inst WHERE map = ? AND o_master = ?", (mid, o_master))
        else:
            self.db.execute("DELETE FROM inst WHERE map = ? AND o_master = ? AND key = ?", (mid, o_master, key))
        self.db.commit()

    def mirror_rows(self, mid):
        rows = self.db.execute("SELECT src_id, o_id, hash FROM mirror_o WHERE map = ?", (mid,))
        return {sid: (o, h) for sid, o, h in rows}

    def put_mirror(self, mid, src_id, o_id, h):
        self.db.execute("INSERT OR REPLACE INTO mirror_o VALUES (?, ?, ?, ?)", (mid, src_id, o_id, h))
        self.db.commit()

    def drop_mirror(self, mid, src_id):
        self.db.execute("DELETE FROM mirror_o WHERE map = ? AND src_id = ?", (mid, src_id))
        self.db.commit()

    def clear_mirror(self, mid):
        self.db.execute("DELETE FROM mirror_o WHERE map = ?", (mid,))
        self.db.commit()

    def mirror_cal(self, mid):
        row = self.db.execute("SELECT o_cal FROM mirror_cal WHERE map = ?", (mid,)).fetchone()
        return row[0] if row else None

    def mirror_cals(self):
        return {o for (o,) in self.db.execute("SELECT o_cal FROM mirror_cal")}

    def drop_mirror_cal(self, mid):
        self.db.execute("DELETE FROM mirror_cal WHERE map = ?", (mid,))
        self.db.execute("DELETE FROM mirror_o WHERE map = ?", (mid,))
        self.db.commit()

    def put_mirror_cal(self, mid, o_cal):
        self.db.execute("INSERT OR REPLACE INTO mirror_cal VALUES (?, ?)", (mid, o_cal))
        self.db.commit()
