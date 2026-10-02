import math

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .maps_api import MapsError

# The Place fields a row shows. Each is named in the field mask and
# nothing else is: the mask decides what Google returns and what it bills.
ROW_FIELDS = ["id", "displayName", "formattedAddress", "location", "rating",
              "userRatingCount", "priceLevel", "currentOpeningHours.openNow",
              "googleMapsUri"]
DETAIL_FIELDS = ROW_FIELDS + ["nationalPhoneNumber", "internationalPhoneNumber",
                              "websiteUri", "regularOpeningHours.weekdayDescriptions",
                              "businessStatus"]

EARTH_RADIUS_KM = 6371.0088


def clip(value, size=200):
    text = str(value or "")
    return text if len(text) <= size else text[: size - 1] + "…"


def straight_line_km(lat1, lng1, lat2, lng2):
    """Great-circle distance: arithmetic on Google's own coordinates, and
    named as what it is — not a travel distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def row(place):
    """One place as the agent shows it. A field Google did not return is
    left out rather than filled in."""
    out = {"place_id": str(place.get("id") or ""),
           "name": clip((place.get("displayName") or {}).get("text"), 120),
           "address": clip(place.get("formattedAddress"))}
    location = place.get("location") or {}
    if "latitude" in location and "longitude" in location:
        out["lat"] = float(location["latitude"])
        out["lng"] = float(location["longitude"])
    if place.get("rating") is not None:
        out["rating"] = float(place["rating"])
    if place.get("userRatingCount") is not None:
        out["rating_count"] = int(place["userRatingCount"])
    level = str(place.get("priceLevel") or "")
    if level and level != "PRICE_LEVEL_UNSPECIFIED":
        out["price_level"] = level.replace("PRICE_LEVEL_", "").lower()
    hours = place.get("currentOpeningHours") or {}
    if "openNow" in hours:
        out["open_now"] = bool(hours["openNow"])
    if place.get("googleMapsUri"):
        out["maps_link"] = str(place["googleMapsUri"])
    return out


class PlacesTool(ToolBase):
    id = "places"

    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        size = int(inputs.get("max_results") or 10)
        body = {"textQuery": str(inputs["query"]), "pageSize": size}
        if ("lat" in inputs) != ("lng" in inputs):
            return {"error": "A location bias needs both lat and lng."}, "error"
        if "lat" in inputs:
            body["locationBias"] = {"circle": {
                "center": {"latitude": float(inputs["lat"]),
                           "longitude": float(inputs["lng"])},
                "radius": float(inputs.get("radius_m") or 5000)}}
        if inputs.get("open_now"):
            body["openNow"] = True
        try:
            answer = client.search_text(body, [f"places.{f}" for f in ROW_FIELDS])
        except MapsError as exc:
            return failure(exc)
        places = [row(p) for p in (answer.get("places") or [])[:size]]
        return {"places": places, "count": len(places)}, "success"

    async def nearby(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        size = int(inputs.get("max_results") or 10)
        try:
            if inputs.get("place_id"):
                # Nearby Search takes a circle, never a place, so a place
                # id is resolved to Google's coordinates for it first.
                centre = client.place(str(inputs["place_id"]), ["location"]).get("location") or {}
                if "latitude" not in centre or "longitude" not in centre:
                    return {"error": "Google returned no location for that place.",
                            "kind": "not_found"}, "error"
                lat, lng = float(centre["latitude"]), float(centre["longitude"])
            elif "lat" in inputs and "lng" in inputs:
                lat, lng = float(inputs["lat"]), float(inputs["lng"])
            else:
                return {"error": "Give a place_id, or both lat and lng."}, "error"
            body = {"maxResultCount": size, "rankPreference": "DISTANCE",
                    "locationRestriction": {"circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": float(inputs.get("radius_m") or 1000)}}}
            if inputs.get("types"):
                body["includedTypes"] = [str(t) for t in inputs["types"]]
            answer = client.search_nearby(body, [f"places.{f}" for f in ROW_FIELDS])
        except MapsError as exc:
            return failure(exc)
        places = []
        for place in (answer.get("places") or [])[:size]:
            shown = row(place)
            if "lat" in shown:
                shown["straight_line_km"] = round(
                    straight_line_km(lat, lng, shown["lat"], shown["lng"]), 2)
            places.append(shown)
        return {"center": {"lat": lat, "lng": lng}, "places": places,
                "count": len(places)}, "success"

    async def details(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            place = client.place(str(call.inputs["place_id"]), DETAIL_FIELDS)
        except MapsError as exc:
            return failure(exc)
        out = row(place)
        for source, target in (("nationalPhoneNumber", "phone"),
                               ("internationalPhoneNumber", "international_phone"),
                               ("websiteUri", "website"),
                               ("businessStatus", "business_status")):
            if place.get(source):
                out[target] = clip(place[source], 300)
        out["hours"] = [clip(line, 120) for line in
                        ((place.get("regularOpeningHours") or {})
                         .get("weekdayDescriptions") or [])[:7]]
        return out, "success"
