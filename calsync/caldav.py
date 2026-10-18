"""iCloud calendar (CalDAV, read-only): Apple ID + app-specific password.

Discovery: caldav.icloud.com -> current-user-principal -> calendar-home-set (pNN-caldav.icloud.com) -> each calendar.
Fetching events: REPORT calendar-query (time range) -> one ics per event, handed to ics.events for expansion.
"""
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests

from .http import request

ROOT = "https://caldav.icloud.com/"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

PROP_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""
PROP_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>"""
PROP_CALS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/></d:prop></d:propfind>"""
QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:time-range start="{start}" end="{end}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"""


class ICloud:
    def __init__(self, apple_id, password, session=None):
        self.session = session or requests.Session()
        self.session.auth = (apple_id, password)
        self._home = None

    def _dav(self, method, url, body, depth):
        r = request(self.session, method, url, ok=(207,), data=body.encode(),
                    headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"})
        return ET.fromstring(r.content)

    def home(self):
        if self._home:
            return self._home
        x = self._dav("PROPFIND", ROOT, PROP_PRINCIPAL, 0)
        principal = urljoin(ROOT, x.find(".//d:current-user-principal/d:href", NS).text.strip())
        x = self._dav("PROPFIND", principal, PROP_HOME, 0)
        self._home = urljoin(principal, x.find(".//c:calendar-home-set/d:href", NS).text.strip())
        return self._home

    def calendars(self):
        """[{name, url}]: only calendars that can hold events (VEVENT)."""
        home = self.home()
        return parse_calendars(self._dav("PROPFIND", home, PROP_CALS, 1), home)

    def events_ics(self, url, start_utc, end_utc):
        body = QUERY.format(start=start_utc.strftime("%Y%m%dT%H%M%SZ"), end=end_utc.strftime("%Y%m%dT%H%M%SZ"))
        x = self._dav("REPORT", url, body, 1)
        return [el.text for el in x.findall(".//c:calendar-data", NS) if el.text]


def parse_calendars(x, base):
    out = []
    for resp in x.findall("d:response", NS):
        href = resp.find("d:href", NS).text.strip()
        prop = resp.find(".//d:prop", NS)
        if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
            continue
        comps = [c.get("name") for c in prop.findall("c:supported-calendar-component-set/c:comp", NS)]
        if comps and "VEVENT" not in comps:
            continue
        name_el = prop.find("d:displayname", NS)
        out.append({"name": (name_el.text or "").strip() if name_el is not None else "", "url": urljoin(base, href)})
    return out
