"""Outlook (personal Microsoft account): read calendars via Microsoft Graph.

Auth: device-code login (calsync login, once); after that the refresh token renews automatically.
The refresh token is replaced on every refresh; it is stored in state_dir/ms_token.json (0600).
"""
import json
import os
import time

import requests

from .http import HttpError, request

AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Calendars.ReadWrite"  # account A two-way sync has to write to Outlook
SELECT = ",".join([
    "id", "subject", "body", "start", "end", "isAllDay", "location", "showAs",
    "sensitivity", "organizer", "attendees", "isOnlineMeeting", "onlineMeeting",
    "webLink", "isCancelled", "isReminderOn", "reminderMinutesBeforeStart", "type",
    "seriesMasterId", "originalStart", "lastModifiedDateTime", "isOrganizer", "recurrence",
])


class LoginRequired(Exception):
    pass


class Graph:
    def __init__(self, client_id, token_path, session=None):
        self.client_id = client_id
        self.token_path = token_path
        self.session = session or requests.Session()
        self._tok = None

    # ---- auth ----

    def login(self, out=print):
        r = request(self.session, "POST", f"{AUTH}/devicecode",
                    data={"client_id": self.client_id, "scope": SCOPES})
        dc = r.json()
        out(dc.get("message") or f"Open {dc['verification_uri']} and enter {dc['user_code']}")
        interval = int(dc.get("interval", 5))
        deadline = time.time() + int(dc.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            r = self.session.post(f"{AUTH}/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": dc["device_code"],
            }, timeout=60)
            body = r.json()
            if r.status_code == 200:
                self._save(body)
                return
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise LoginRequired(f"login failed: {err}: {body.get('error_description', '')[:200]}")
        raise LoginRequired("login timed out")

    def _save(self, body):
        tok = {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": int(time.time()) + int(body.get("expires_in", 3600)),
        }
        tmp = self.token_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, self.token_path)
        self._tok = tok

    def _access_token(self):
        if self._tok is None:
            try:
                with open(self.token_path) as f:
                    self._tok = json.load(f)
            except FileNotFoundError:
                raise LoginRequired("no Outlook token; run: calsync login")
        if self._tok["expires_at"] - 120 > time.time():
            return self._tok["access_token"]
        r = self.session.post(f"{AUTH}/token", data={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": self._tok["refresh_token"], "scope": SCOPES,
        }, timeout=60)
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("error") in ("invalid_grant", "interaction_required"):
                raise LoginRequired("Outlook login expired; run: calsync login")
            raise HttpError(r.status_code, r.text, f"{AUTH}/token")
        self._save(r.json())
        return self._tok["access_token"]

    # ---- read ----

    def _paged(self, url, params=None, headers=None):
        while url:
            h = {"Authorization": f"Bearer {self._access_token()}", **(headers or {})}
            data = request(self.session, "GET", url, params=params, headers=h).json()
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries all the parameters

    def calendars(self):
        return list(self._paged(f"{GRAPH}/me/calendars",
                                {"$select": "id,name,owner,canEdit,isDefaultCalendar", "$top": "100"}))

    @staticmethod
    def _prefer(timezone):
        return {"Prefer": f'outlook.timezone="{timezone}", outlook.body-content-type="text", IdType="ImmutableId"'}

    def calendar_view(self, calendar_id, start_utc, end_utc, timezone):
        """One expanded event (every occurrence of a recurring meeting is its own entry). start/end: aware datetimes."""
        params = {
            "startDateTime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": SELECT,
            "$top": "250",
        }
        return list(self._paged(f"{GRAPH}/me/calendars/{calendar_id}/calendarView", params, self._prefer(timezone)))

    # ---- write (two-way sync) ----

    def _call(self, method, url, timezone, ok=(200,), **kw):
        h = {"Authorization": f"Bearer {self._access_token()}", **self._prefer(timezone)}
        return request(self.session, method, url, ok=ok, headers=h, **kw)

    def get_event(self, event_id, timezone, calendar_id=None):
        """Fetch one event; return None if it does not exist. If calendar_id is given, look only in that calendar:
        events deleted in Outlook go to "Deleted Items" and can still be fetched via /me/events/{id},
        but a lookup by calendar gives 404 / 400."""
        base = f"{GRAPH}/me/calendars/{calendar_id}/events" if calendar_id else f"{GRAPH}/me/events"
        try:
            return self._call("GET", f"{base}/{event_id}", timezone, params={"$select": SELECT}).json()
        except HttpError as e:
            if e.status == 404 or (e.status == 400 and "non-calendar folder" in e.text):
                return None
            raise

    def create_event(self, calendar_id, body, timezone):
        r = self._call("POST", f"{GRAPH}/me/calendars/{calendar_id}/events", timezone, ok=(201,), json=body)
        return self.get_event(r.json()["id"], timezone, calendar_id)   # re-fetch: plain-text body, local-timezone times

    def patch_event(self, event_id, body, timezone):
        self._call("PATCH", f"{GRAPH}/me/events/{event_id}", timezone, json=body)
        return self.get_event(event_id, timezone)

    def create_calendar(self, name):
        h = {"Authorization": f"Bearer {self._access_token()}"}
        return request(self.session, "POST", f"{GRAPH}/me/calendars", ok=(201,), headers=h, json={"name": name}).json()

    def delete_calendar(self, calendar_id):
        """Delete a whole calendar (config page "clean up" of calsync calendars); already gone is not an error."""
        h = {"Authorization": f"Bearer {self._access_token()}"}
        try:
            request(self.session, "DELETE", f"{GRAPH}/me/calendars/{calendar_id}", ok=(204,), headers=h)
        except HttpError as e:
            if e.status != 404:
                raise

    def delete_event(self, event_id, timezone):
        try:
            self._call("DELETE", f"{GRAPH}/me/events/{event_id}", timezone, ok=(204,))
        except HttpError as e:
            if not (e.status == 404 or (e.status == 400 and "non-calendar folder" in e.text)):
                raise
