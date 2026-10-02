"""A small Google Calendar client over the bound credential.

The same shape as the Gmail agent's client, and the same three
failure kinds — ``auth`` (reconnect), ``http`` (Google refused),
``unknown`` (a write got no answer; never retried) — plus ``not_found``.
Reads are retried once on a dropped connection; writes never are.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

API_BASE_URL = "https://www.googleapis.com/calendar/v3"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


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
            raise GoogleError("auth", "The Google account is not connected — "
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
            return str(error.get("message") or error)[:300]
        return str(error or body)[:300]

    def _request(self, method: str, path: str, *, params=None, json=None,
                 write: bool = False) -> Dict[str, Any]:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for attempt in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(
                    method, url, params=params, json=json,
                    headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GoogleError(
                        "unknown", f"No answer from Google Calendar for "
                                   f"{method} {path}: the outcome is unknown "
                                   f"({exc}).")
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                     "was revoked — reconnect the account "
                                     "from its Credentials page.")
            if response.status_code in (404, 410):
                raise GoogleError("not_found", f"Google Calendar has no {path}.")
            if response.status_code >= 400:
                raise GoogleError("http", f"Google Calendar refused {method} "
                                          f"{path}: {self._detail(response)}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GoogleError("http", f"Google Calendar could not be reached: {last}")

    # -- reads -----------------------------------------------------------
    def primary(self) -> Dict[str, Any]:
        return self._request("GET", "calendars/primary")

    def list_events(self, time_min: str, time_max: str, query: str = "",
                    max_results: int = 25, page_token: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
            "orderBy": "startTime", "maxResults": max_results,
        }
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "calendars/primary/events", params=params)

    def get_event(self, event_id: str) -> Dict[str, Any]:
        return self._request("GET", f"calendars/primary/events/{event_id}")

    def free_busy(self, time_min: str, time_max: str,
                  calendars: List[str]) -> Dict[str, Any]:
        body = {"timeMin": time_min, "timeMax": time_max,
                "items": [{"id": c} for c in calendars]}
        return self._request("POST", "freeBusy", json=body)

    # -- writes ----------------------------------------------------------
    def insert_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "calendars/primary/events",
                             params={"sendUpdates": "all"}, json=event,
                             write=True)

    def patch_event(self, event_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PATCH", f"calendars/primary/events/{event_id}",
                             params={"sendUpdates": "all"}, json=patch,
                             write=True)

    def delete_event(self, event_id: str) -> None:
        self._request("DELETE", f"calendars/primary/events/{event_id}",
                      params={"sendUpdates": "all"}, write=True)
