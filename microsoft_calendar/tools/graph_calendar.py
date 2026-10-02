"""A small Microsoft Graph calendar client over the connected account.

One class, no SDK, and the same failure kinds as the Outlook agent's
mail client:

- ``auth``: the account is not connected, or Microsoft no longer
  accepts its grant — reconnect it from the Credentials page.
- ``http``: Graph answered with an error — the message says what.
- ``unknown``: a write was sent and no answer came back. The outcome
  is unknown; nothing retries it.
- ``not_found``: Graph has no such event.

Reads are retried once on a dropped connection; writes never are. A
free/busy lookup is a POST, but it is a read, and is retried like one.

Every request asks Graph for UTC and for immutable ids. UTC because a
mailbox's own zone is usually a Windows name the agent has to translate
before it can reckon in it — so the agent does the arithmetic, and
Graph only ever speaks UTC to it. Immutable ids because an event keeps
its id when Outlook moves it. Ids may contain "/" and "=", so every one
is escaped before it goes into a path.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://graph.microsoft.com/v1.0"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

EVENT_FIELDS = ("id,subject,start,end,isAllDay,location,organizer,attendees,"
                "isCancelled,webLink,showAs")


class GraphError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found"
        self.message = message


def utc_text(moment: datetime) -> str:
    """An aware moment as Graph's dateTime: UTC, no offset, to the second."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def from_graph(value: Any) -> Optional[datetime]:
    """Graph's {dateTime, timeZone} as an aware UTC datetime. Asked for
    UTC, Graph answers in UTC with seven fractional digits, which the
    standard library will not parse; the fraction is not needed."""
    text = str((value or {}).get("dateTime") or "")
    if not text:
        return None
    return datetime.fromisoformat(text.split(".")[0]).replace(tzinfo=timezone.utc)


def event_path(event_id: str) -> str:
    return "me/events/" + urllib.parse.quote(str(event_id), safe="")


class CalendarClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        self.api_base_url = (str(secret.get("api_base_url") or "")
                             or API_BASE_URL).rstrip("/")

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise GraphError("auth", "The Microsoft account is not connected — "
                                     "connect it from the agent's Credentials "
                                     "tab.")
        return self.access_token

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:300]
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:300]
        return str(error or body)[:300]

    # -- one request -----------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False, absolute: str = "") -> Dict[str, Any]:
        url = absolute or f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Prefer": 'IdType="ImmutableId", outlook.timezone="UTC"',
            }
            try:
                response = requests.request(
                    method, url, params=params, json=json,
                    headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GraphError(
                        "unknown", f"No answer from Microsoft for {method} "
                                   f"{path}: the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GraphError("auth", "The Microsoft connection has expired "
                                         "or was revoked — reconnect the "
                                         "account from its Credentials page.")
            if response.status_code == 404:
                raise GraphError("not_found", "The calendar has no such event.")
            if response.status_code >= 400:
                raise GraphError("http", f"Microsoft refused {method} {path}: "
                                         f"{self._detail(response)}")
            if response.status_code in (202, 204) or not response.content:
                return {}
            return response.json()
        raise GraphError("http", f"Microsoft could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "me", params={"$select": "mail,userPrincipalName,displayName"})

    def mailbox_timezone(self) -> str:
        """The zone as the mailbox keeps it — usually a Windows name."""
        answer = self._request("GET", "me/mailboxSettings", params={"$select": "timeZone"})
        return str(answer.get("timeZone") or "")

    def calendar_view(self, start: datetime, end: datetime, top: int = 25,
                      page_link: str = "") -> Dict[str, Any]:
        """Every occurrence between two moments, recurring ones expanded.
        Graph pages with a full next-link URL, which is the page token."""
        if page_link:
            return self._request("GET", "", absolute=page_link)
        return self._request("GET", "me/calendarView", params={
            "startDateTime": utc_text(start), "endDateTime": utc_text(end),
            "$orderby": "start/dateTime", "$top": top, "$select": EVENT_FIELDS,
        })

    def get_event(self, event_id: str) -> Dict[str, Any]:
        return self._request("GET", event_path(event_id))

    def get_schedule(self, addresses: List[str], start: datetime,
                     end: datetime) -> Dict[str, Any]:
        return self._request("POST", "me/calendar/getSchedule", json={
            "schedules": addresses,
            "startTime": {"dateTime": utc_text(start), "timeZone": "UTC"},
            "endTime": {"dateTime": utc_text(end), "timeZone": "UTC"},
            "availabilityViewInterval": 15,
        })

    # -- writes ----------------------------------------------------------
    def create_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "me/events", json=event, write=True)

    def update_event(self, event_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", event_path(event_id), json=patch, write=True)

    def delete_event(self, event_id: str) -> None:
        """On the organizer's calendar, Graph tells the attendees."""
        self._request("DELETE", event_path(event_id), write=True)
