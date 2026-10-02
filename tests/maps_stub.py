"""A loopback Google Maps Platform: the subset of Places API (New) and
Routes API the Places agent calls, over real HTTP, holding fictional
places around a fictional Dubai Marina and Abu Dhabi.

It behaves the way the real APIs are documented to, in the ways that
catch a client out:

- no ``X-Goog-Api-Key``, or the wrong one, is refused as an invalid key;
- no ``X-Goog-FieldMask`` is refused, and a response carries ONLY the
  fields the mask names — so a client that forgets a field in its mask
  loses that value, and its test fails;
- an API switched off in ``disabled`` answers 403 SERVICE_DISABLED;
- the route matrix answers with a JSON array in reverse order, and a
  zero index is omitted the way proto3 JSON omits defaults.

Distances are the straight line times 1.25, and travel times follow
from a fixed speed per mode — invented for the stub, and nothing the
agent could produce itself.
"""

from __future__ import annotations

import json
import math
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

KEY = "test-key"
SPEED_KMH = {"DRIVE": 60.0, "WALK": 5.0, "BICYCLE": 15.0, "TRANSIT": 30.0}
TRAFFIC_FACTOR = 1.25
ROAD_FACTOR = 1.25
STOPWORDS = {"near", "in", "the", "at", "of", "a", "an", "and", "open", "now"}

PLACES: List[Dict[str, Any]] = [
    {"id": "pl_marina_brew", "displayName": {"text": "Marina Brew House", "languageCode": "en"},
     "formattedAddress": "12 Waterfront Walk, Dubai Marina, Dubai",
     "location": {"latitude": 25.0805, "longitude": 55.1403}, "types": ["cafe", "coffee_shop"],
     "rating": 4.6, "userRatingCount": 812, "priceLevel": "PRICE_LEVEL_MODERATE",
     "currentOpeningHours": {"openNow": True},
     "regularOpeningHours": {"weekdayDescriptions": [
         "Monday: 7:00 AM – 11:00 PM", "Tuesday: 7:00 AM – 11:00 PM",
         "Wednesday: 7:00 AM – 11:00 PM", "Thursday: 7:00 AM – 11:00 PM",
         "Friday: 8:00 AM – 12:00 AM", "Saturday: 8:00 AM – 12:00 AM",
         "Sunday: 8:00 AM – 11:00 PM"]},
     "nationalPhoneNumber": "04 555 0101", "internationalPhoneNumber": "+971 4 555 0101",
     "websiteUri": "https://marinabrew.example", "businessStatus": "OPERATIONAL",
     "googleMapsUri": "https://maps.google.com/?cid=1001"},
    {"id": "pl_dhow_bean", "displayName": {"text": "Dhow & Bean", "languageCode": "en"},
     "formattedAddress": "Pier 7 Promenade, Dubai Marina, Dubai",
     "location": {"latitude": 25.0770, "longitude": 55.1350}, "types": ["cafe", "coffee_shop"],
     "rating": 4.3, "userRatingCount": 240, "priceLevel": "PRICE_LEVEL_INEXPENSIVE",
     "currentOpeningHours": {"openNow": False},
     "googleMapsUri": "https://maps.google.com/?cid=1002"},
    {"id": "pl_lantern", "displayName": {"text": "Lantern Street Kitchen", "languageCode": "en"},
     "formattedAddress": "18 Waterfront Walk, Dubai Marina, Dubai",
     "location": {"latitude": 25.0808, "longitude": 55.1398}, "types": ["restaurant"],
     "rating": 4.4, "userRatingCount": 1320, "priceLevel": "PRICE_LEVEL_EXPENSIVE",
     "currentOpeningHours": {"openNow": True},
     "googleMapsUri": "https://maps.google.com/?cid=1003"},
    {"id": "pl_marina_parking", "displayName": {"text": "Marina Gate Parking", "languageCode": "en"},
     "formattedAddress": "Marina Gate Street, Dubai Marina, Dubai",
     "location": {"latitude": 25.0815, "longitude": 55.1420}, "types": ["parking"],
     "googleMapsUri": "https://maps.google.com/?cid=1004"},
    {"id": "pl_jlt_corner", "displayName": {"text": "Tower Lane Coffee Corner", "languageCode": "en"},
     "formattedAddress": "Tower Lane, Jumeirah Lakes, Dubai",
     "location": {"latitude": 25.0690, "longitude": 55.1440}, "types": ["cafe"],
     "rating": 4.1, "userRatingCount": 95, "currentOpeningHours": {"openNow": True},
     "googleMapsUri": "https://maps.google.com/?cid=1005"},
    {"id": "pl_harbourline", "displayName": {"text": "Harbourline Office", "languageCode": "en"},
     "formattedAddress": "Level 9, Corniche Tower, Abu Dhabi",
     "location": {"latitude": 24.4700, "longitude": 54.3400}, "types": ["corporate_office"],
     "businessStatus": "OPERATIONAL",
     "googleMapsUri": "https://maps.google.com/?cid=1006"},
    {"id": "pl_palmgrove_airport", "displayName": {"text": "Palmgrove Airport", "languageCode": "en"},
     "formattedAddress": "Palmgrove Airport, Dubai",
     "location": {"latitude": 24.9000, "longitude": 55.1600}, "types": ["airport"],
     "googleMapsUri": "https://maps.google.com/?cid=1007"},
    {"id": "pl_pearl_rock", "displayName": {"text": "Pearl Rock Lighthouse", "languageCode": "en"},
     "formattedAddress": "Pearl Rock Island",
     "location": {"latitude": 25.3000, "longitude": 54.9000}, "types": ["tourist_attraction"],
     "googleMapsUri": "https://maps.google.com/?cid=1008"},
]
# Places no road reaches.
ISLANDS = {"pl_pearl_rock"}
# Addresses the stub can geocode, beyond each place's own name and address.
ADDRESSES = {"dubai marina, dubai": (25.0800, 55.1400)}


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def apply_mask(value: Any, mask: str) -> Any:
    """Keep only the fields a field mask names, as Google does. Paths are
    dotted; a repeated field applies the rest of the path to each item."""
    if mask.strip() == "*":
        return value
    tree: Dict[str, Any] = {}
    for path in mask.split(","):
        node = tree
        for part in [p for p in path.strip().split(".") if p]:
            node = node.setdefault(part, {})

    def prune(item: Any, node: Dict[str, Any]) -> Any:
        if not node:
            return item
        if isinstance(item, list):
            return [prune(i, node) for i in item]
        if not isinstance(item, dict):
            return item
        return {k: prune(v, node[k]) for k, v in item.items() if k in node}

    return prune(value, tree)


class MapsStub:
    def __init__(self):
        self.places = {p["id"]: p for p in PLACES}
        self.disabled: set = set()          # "places" and/or "routes"
        self.requests: List[Dict[str, Any]] = []
        self._server: Optional[ThreadingHTTPServer] = None

    # -- the fake world ---------------------------------------------------
    def _resolve(self, waypoint: Dict[str, Any]) -> Tuple[Optional[Tuple[float, float]], str]:
        """A waypoint's coordinates and its place id when it names one."""
        if "placeId" in waypoint:
            place = self.places.get(waypoint["placeId"])
            if place is None:
                return None, ""
            loc = place["location"]
            return (loc["latitude"], loc["longitude"]), place["id"]
        if "location" in waypoint:
            ll = waypoint["location"]["latLng"]
            return (float(ll["latitude"]), float(ll["longitude"])), ""
        text = str(waypoint.get("address") or "").strip().lower()
        if text in ADDRESSES:
            return ADDRESSES[text], ""
        for place in self.places.values():
            if text in (place["displayName"]["text"].lower(), place["formattedAddress"].lower()):
                loc = place["location"]
                return (loc["latitude"], loc["longitude"]), place["id"]
        return None, ""

    def _trip(self, origin, destination, body) -> Dict[str, Any]:
        """One origin/destination pair as a matrix element would carry it."""
        a, a_id = self._resolve(origin)
        b, b_id = self._resolve(destination)
        if a is None or b is None:
            return {"status": {"code": 5, "message": "Waypoint could not be geocoded."}}
        if {a_id, b_id} & ISLANDS and a_id != b_id:
            return {"status": {}, "condition": "ROUTE_NOT_FOUND"}
        mode = body.get("travelMode") or "DRIVE"
        km = haversine_km(a, b) * ROAD_FACTOR
        static = round(km / SPEED_KMH[mode] * 3600)
        traffic = body.get("routingPreference") in ("TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL")
        return {"status": {}, "condition": "ROUTE_EXISTS", "distanceMeters": round(km * 1000),
                "duration": f"{round(static * TRAFFIC_FACTOR) if traffic else static}s",
                "staticDuration": f"{static}s"}

    @staticmethod
    def _trip_refusal(body: Dict[str, Any]) -> Optional[str]:
        mode = body.get("travelMode") or "DRIVE"
        if body.get("routingPreference") and mode not in ("DRIVE", "TWO_WHEELER"):
            return f"Routing preference cannot be set for {mode} travel mode."
        when = body.get("departureTime")
        if when and mode != "TRANSIT":
            moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if moment < datetime.now(timezone.utc):
                return "Timestamp must be set to a future time."
        return None

    def _search_text(self, body: Dict[str, Any]) -> Dict[str, Any]:
        size = int(body.get("pageSize") or body.get("maxResultCount") or 20)
        words = [w for w in str(body.get("textQuery") or "").lower().replace(",", " ").split()
                 if w not in STOPWORDS]
        scored = []
        for place in self.places.values():
            haystack = " ".join([place["displayName"]["text"], place["formattedAddress"],
                                 *place["types"]]).lower().replace("_", " ")
            score = sum(1 for w in words if w in haystack or (w == "coffee" and "cafe" in haystack))
            if score == 0:
                continue
            if body.get("openNow") and not place.get("currentOpeningHours", {}).get("openNow"):
                continue
            scored.append((-score, place["displayName"]["text"], place))
        scored.sort(key=lambda s: (s[0], s[1]))
        return {"places": [s[2] for s in scored[:size]]}

    def _search_nearby(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        circle = (body.get("locationRestriction") or {}).get("circle") or {}
        radius = float(circle.get("radius") or 0)
        if not 0 < radius <= 50000:
            return 400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                   "message": "radius must be in (0, 50000]."}}
        centre = (circle["center"]["latitude"], circle["center"]["longitude"])
        wanted = set(body.get("includedTypes") or [])
        rows = []
        for place in self.places.values():
            loc = place["location"]
            km = haversine_km(centre, (loc["latitude"], loc["longitude"]))
            if km * 1000 <= radius and (not wanted or wanted & set(place["types"])):
                rows.append((km, place))
        rows.sort(key=lambda r: r[0])
        size = int(body.get("maxResultCount") or 20)
        return 200, {"places": [r[1] for r in rows[:size]]}

    # -- the server -------------------------------------------------------
    def start(self) -> "MapsStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _gate(self, path: str, body: Dict[str, Any]) -> Optional[str]:
                """The checks Google makes before it reads a request; the
                field mask when they pass, else None (already answered)."""
                mask = self.headers.get("X-Goog-FieldMask")
                stub.requests.append({"path": path, "mask": mask, "body": body,
                                      "key": self.headers.get("X-Goog-Api-Key")})
                if self.headers.get("X-Goog-Api-Key") != KEY:
                    self._json(400, {"error": {
                        "code": 400, "status": "INVALID_ARGUMENT",
                        "message": "API key not valid. Please pass a valid API key.",
                        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                     "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]}})
                    return None
                service = "places" if path.startswith("/places") else "routes"
                if service in stub.disabled:
                    host = f"{service}.googleapis.com"
                    name = "Places API (New)" if service == "places" else "Routes API"
                    self._json(403, {"error": {
                        "code": 403, "status": "PERMISSION_DENIED",
                        "message": f"{name} has not been used in project 123456 before or "
                                   f"it is disabled. Enable it by visiting https://console."
                                   f"developers.google.com/apis/api/{host}/overview?project="
                                   f"123456 then retry.",
                        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                     "reason": "SERVICE_DISABLED", "domain": "googleapis.com",
                                     "metadata": {"service": host}}]}})
                    return None
                if not mask:
                    self._json(400, {"error": {
                        "code": 400, "status": "INVALID_ARGUMENT",
                        "message": "FieldMask is a required parameter. See https://cloud."
                                   "google.com/apis/docs/system-parameters on how to provide "
                                   "it."}})
                    return None
                return mask

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                mask = self._gate(path, {})
                if mask is None:
                    return
                if path.startswith("/places/"):
                    place = stub.places.get(urllib.parse.unquote(path[len("/places/"):]))
                    if place is None:
                        return self._json(404, {"error": {
                            "code": 404, "status": "NOT_FOUND",
                            "message": "Requested entity was not found."}})
                    return self._json(200, apply_mask(place, mask))
                self._json(404, {"error": {"code": 404, "message": f"no route {path}"}})

            def do_POST(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                body = self._body()
                mask = self._gate(path, body)
                if mask is None:
                    return
                if path == "/places:searchText":
                    return self._json(200, apply_mask(stub._search_text(body), mask))
                if path == "/places:searchNearby":
                    status, answer = stub._search_nearby(body)
                    return self._json(status, apply_mask(answer, mask) if status == 200 else answer)
                if path in ("/distanceMatrix/v2:computeRouteMatrix", "/directions/v2:computeRoutes"):
                    refusal = stub._trip_refusal(body)
                    if refusal:
                        return self._json(400, {"error": {"code": 400, "message": refusal,
                                                          "status": "INVALID_ARGUMENT"}})
                if path == "/distanceMatrix/v2:computeRouteMatrix":
                    elements = []
                    for oi, origin in enumerate(body.get("origins") or []):
                        for di, dest in enumerate(body.get("destinations") or []):
                            element = stub._trip(origin["waypoint"], dest["waypoint"], body)
                            # proto3 JSON leaves zero values out.
                            if oi:
                                element["originIndex"] = oi
                            if di:
                                element["destinationIndex"] = di
                            elements.append(element)
                    elements.reverse()
                    return self._json(200, apply_mask(elements, mask))
                if path == "/directions/v2:computeRoutes":
                    element = stub._trip(body["origin"], body["destination"], body)
                    if element.get("condition") != "ROUTE_EXISTS":
                        return self._json(200, {})
                    metres = element["distanceMeters"]
                    route = {**{k: element[k] for k in ("distanceMeters", "duration", "staticDuration")},
                             "description": "E11",
                             "warnings": ["This route has tolls."] if body.get("travelMode", "DRIVE") == "DRIVE" else [],
                             "legs": [{"distanceMeters": metres, "steps": [
                                 {"distanceMeters": round(metres * 0.1),
                                  "navigationInstruction": {"maneuver": "DEPART", "instructions": "Head south on Waterfront Walk"}},
                                 {"distanceMeters": round(metres * 0.85),
                                  "navigationInstruction": {"maneuver": "MERGE", "instructions": "Merge onto E11"}},
                                 {"distanceMeters": metres - round(metres * 0.1) - round(metres * 0.85),
                                  "navigationInstruction": {"maneuver": "TURN_RIGHT", "instructions": "Turn right to the destination"}},
                             ]}]}
                    return self._json(200, apply_mask({"routes": [route]}, mask))
                self._json(404, {"error": {"code": 404, "message": f"no route {path}"}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, api_key: str = KEY) -> Dict[str, Any]:
        """The credential as the platform hands it to the agent: the key
        an administrator pasted, and the loopback base URL."""
        return {"api_key": api_key, "api_base_url": self.url}

    def masks(self, path: str) -> List[str]:
        return [r["mask"] for r in self.requests if r["path"] == path]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
