"""Google Calendar: service-account auth (JWT), calling the REST API directly.

The target calendar must first be shared with the service account in Google Calendar
("Make changes to events" permission).
Every event the tool writes carries extendedProperties.private:
  calsync=1, calsync_map=<mapping ID>, calsync_src=<Outlook event ID>, calsync_hash=<content hash>
Only events with these markers are updated / deleted; other events are never touched.
"""
import base64
import json
import time
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .http import HttpError, request

API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
LIST_FIELDS = "items(id,status,extendedProperties),nextPageToken"


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class GCal:
    def __init__(self, key_file, session=None, write_pause=0.15):
        with open(key_file) as f:
            self.key = json.load(f)
        self.email = self.key["client_email"]
        self.session = session or requests.Session()
        self.write_pause = write_pause  # short pause between writes: fewer rate-limit hits on a first sync of thousands
        self._tok = None
        self._exp = 0

    def _token(self):
        if self._tok and self._exp - 120 > time.time():
            return self._tok
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key.get("private_key_id", "")}
        claims = {"iss": self.email, "scope": SCOPE, "aud": self.key["token_uri"],
                  "iat": now, "exp": now + 3600}
        signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
        pk = serialization.load_pem_private_key(self.key["private_key"].encode(), password=None)
        sig = pk.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        r = request(self.session, "POST", self.key["token_uri"], data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": f"{signing_input}.{_b64url(sig)}",
        })
        body = r.json()
        self._tok = body["access_token"]
        self._exp = now + int(body.get("expires_in", 3600))
        return self._tok

    def _req(self, method, path, ok=(200,), **kw):
        headers = {"Authorization": f"Bearer {self._token()}"}
        return request(self.session, method, f"{API}{path}", ok=ok, headers=headers, **kw)

    @staticmethod
    def _cal(calendar_id):
        return f"/calendars/{quote(calendar_id, safe='')}/events"

    def check_access(self, calendar_id):
        """Return None if this calendar can be read, otherwise an error description."""
        try:
            self._req("GET", self._cal(calendar_id), params={"maxResults": "1", "fields": "items(id)"})
            return None
        except HttpError as e:
            return f"HTTP {e.status}"

    def list_marked(self, calendar_id, *, map_id=None, time_min=None, time_max=None):
        """Return {event_id: private_props}, only for events carrying the calsync marker (deleted ones excluded)."""
        prop = f"calsync_map={map_id}" if map_id else "calsync=1"
        params = {"privateExtendedProperty": prop, "maxResults": "2500",
                  "singleEvents": "true", "showDeleted": "false", "fields": LIST_FIELDS}
        if time_min:
            params["timeMin"] = time_min.isoformat()
        if time_max:
            params["timeMax"] = time_max.isoformat()
        out = {}
        while True:
            data = self._req("GET", self._cal(calendar_id), params=params).json()
            for ev in data.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                out[ev["id"]] = ev.get("extendedProperties", {}).get("private", {})
            token = data.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    # ---- for two-way sync: the whole calendar (a dedicated calendar, not filtered by marker) ----

    def list_all(self, calendar_id, time_min, time_max):
        """Single events and series masters in the window (not expanded, deleted ones excluded;
        modified single occurrences within a series are fetched separately via instances)."""
        params = {"maxResults": "2500", "singleEvents": "false", "showDeleted": "false",
                  "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat()}
        out = []
        while True:
            data = self._req("GET", self._cal(calendar_id), params=params).json()
            out += [e for e in data.get("items", []) if e.get("status") != "cancelled" and not e.get("recurringEventId")]
            token = data.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    def get(self, calendar_id, event_id):
        """Fetch one event; return None if it does not exist or is deleted."""
        try:
            ev = self._req("GET", f"{self._cal(calendar_id)}/{event_id}").json()
        except HttpError as e:
            if e.status in (404, 410):
                return None
            raise
        return None if ev.get("status") == "cancelled" else ev

    def instances(self, calendar_id, event_id, time_min, time_max):
        params = {"maxResults": "2500", "showDeleted": "false",
                  "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat()}
        out = []
        while True:
            data = self._req("GET", f"{self._cal(calendar_id)}/{event_id}/instances", params=params).json()
            out += [e for e in data.get("items", []) if e.get("status") != "cancelled"]
            token = data.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    def patch(self, calendar_id, event_id, body):
        r = self._req("PATCH", f"{self._cal(calendar_id)}/{event_id}", params={"sendUpdates": "none"}, json=body)
        time.sleep(self.write_pause)
        return r.json()

    def insert(self, calendar_id, body):
        """Insert with a fixed ID; if the ID already exists (even a deleted one), overwrite the whole event."""
        try:
            r = self._req("POST", self._cal(calendar_id), params={"sendUpdates": "none"},
                          json=body, ok=(200,))
        except HttpError as e:
            if e.status != 409 or "id" not in body:
                raise
            return self.update(calendar_id, body["id"], body)
        time.sleep(self.write_pause)
        return r.json()

    def update(self, calendar_id, event_id, body):
        r = self._req("PUT", f"{self._cal(calendar_id)}/{event_id}", params={"sendUpdates": "none"},
                      json=body, ok=(200,))
        time.sleep(self.write_pause)
        return r.json()

    def delete(self, calendar_id, event_id):
        self._req("DELETE", f"{self._cal(calendar_id)}/{event_id}", params={"sendUpdates": "none"},
                  ok=(200, 204, 410))
        time.sleep(self.write_pause)
