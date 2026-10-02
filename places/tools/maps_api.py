"""A small Google Maps Platform client over an API key.

Two APIs, one key: Places API (New) and Routes API. Both refuse a
request without ``X-Goog-FieldMask``, and both bill by the fields a
mask names, so every call here names exactly the fields its caller
reads — never ``*``.

The failure kinds match the other agents' clients — ``auth`` (the key,
or the project behind it, must be fixed by a person), ``http`` (Google
refused), ``not_found`` — and every call is a read, so each is retried
once on a dropped connection. ``unknown`` never arises: nothing here
changes anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

PLACES_BASE_URL = "https://places.googleapis.com/v1"
ROUTES_BASE_URL = "https://routes.googleapis.com"
TIMEOUT = 20

# The names Google's console uses, so a person can find the switch.
PLACES_API = "Places API (New)"
ROUTES_API = "Routes API"


class MapsError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class MapsClient:
    def __init__(self, secret: Dict[str, Any]):
        self.api_key = str(secret.get("api_key") or "").strip()
        # Tests point both APIs at one loopback server; their paths do
        # not collide, so one override serves both.
        override = str(secret.get("api_base_url") or "").rstrip("/")
        self.places_base = override or PLACES_BASE_URL
        self.routes_base = override or ROUTES_BASE_URL

    # -- transport -------------------------------------------------------
    @staticmethod
    def _error(response: requests.Response) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {"message": (response.text or "").strip()[:300]}
        if isinstance(body, list) and body:
            body = body[0]
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {"message": str(body)[:300]}

    @staticmethod
    def _reasons(error: Dict[str, Any]) -> List[str]:
        return [str(d.get("reason") or "") for d in error.get("details") or []
                if isinstance(d, dict)]

    def _refusal(self, response: requests.Response, api: str) -> MapsError:
        error = self._error(response)
        message = str(error.get("message") or "")[:300]
        reasons = self._reasons(error)
        lowered = message.lower()
        if "API_KEY_INVALID" in reasons or "api key not valid" in lowered:
            return MapsError("auth", "Google refused the Google Maps Platform key as "
                                     "invalid — check the key pasted in the agent's "
                                     "Credentials tab.")
        if ("SERVICE_DISABLED" in reasons or "has not been used in project" in lowered
                or "is disabled" in lowered):
            return MapsError("auth", f"The {api} is not enabled in the Google Cloud "
                                     f"project this key belongs to — enable the {api} "
                                     f"there, wait a few minutes, and try again.")
        if "API_KEY_SERVICE_BLOCKED" in reasons or "are blocked" in lowered:
            return MapsError("auth", f"This key is restricted and may not call the {api} "
                                     f"— add the {api} to the key's API restrictions.")
        if "BILLING_DISABLED" in reasons or "billing" in lowered:
            return MapsError("auth", f"The {api} needs billing enabled on the Google "
                                     f"Cloud project this key belongs to.")
        if response.status_code in (401, 403):
            return MapsError("auth", f"Google refused this key for the {api}: {message}")
        if response.status_code == 404:
            return MapsError("not_found", f"Google has no such place: {message}")
        if response.status_code == 429:
            return MapsError("http", f"The {api} quota for this key is used up for now: "
                                     f"{message}")
        return MapsError("http", f"The {api} refused the request: {message}")

    def _request(self, method: str, url: str, api: str, fields: List[str],
                 json: Optional[Dict[str, Any]] = None) -> Any:
        if not self.api_key:
            raise MapsError("auth", "The Google Maps Platform credential has no API key "
                                    "— paste one in the agent's Credentials tab.")
        headers = {"X-Goog-Api-Key": self.api_key,
                   "X-Goog-FieldMask": ",".join(fields)}
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                response = requests.request(method, url, json=json,
                                            headers=headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                continue
            if response.status_code >= 400:
                raise self._refusal(response, api)
            return response.json() if response.content else {}
        raise MapsError("http", f"The {api} could not be reached: {last}")

    # -- Places API (New) ------------------------------------------------
    def search_text(self, body: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        return self._request("POST", f"{self.places_base}/places:searchText",
                             PLACES_API, fields, json=body)

    def search_nearby(self, body: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        return self._request("POST", f"{self.places_base}/places:searchNearby",
                             PLACES_API, fields, json=body)

    def place(self, place_id: str, fields: List[str]) -> Dict[str, Any]:
        return self._request("GET", f"{self.places_base}/places/{quote(place_id, safe='')}",
                             PLACES_API, fields)

    # -- Routes API ------------------------------------------------------
    def route_matrix(self, body: Dict[str, Any], fields: List[str]) -> List[Dict[str, Any]]:
        # Over REST the matrix answers with a JSON array of elements, one
        # per origin/destination pair, in no promised order.
        answer = self._request("POST",
                               f"{self.routes_base}/distanceMatrix/v2:computeRouteMatrix",
                               ROUTES_API, fields, json=body)
        return answer if isinstance(answer, list) else []

    def routes(self, body: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        return self._request("POST", f"{self.routes_base}/directions/v2:computeRoutes",
                             ROUTES_API, fields, json=body)
