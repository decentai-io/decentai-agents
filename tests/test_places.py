"""The Places and Distances agent in a real worker against a loopback
Google Maps Platform (tests/maps_stub.py). The places are fictional,
around a fictional Dubai Marina and an office in Abu Dhabi.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.maps_stub import MapsStub

MARINA = {"lat": 25.0800, "lng": 55.1400}
SEARCH, NEARBY = "/places:searchText", "/places:searchNearby"
MATRIX, ROUTES = "/distanceMatrix/v2:computeRouteMatrix", "/directions/v2:computeRoutes"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def maps():
    stub = MapsStub().start()
    yield stub
    stub.stop()


def executor(maps, api_key="test-key"):
    provider = InMemoryResourceProvider(secrets={"places__maps": maps.secret(api_key)})
    return FunctionExecutor(provider=provider)


def invoke(agents, ex, name, inputs):
    return run(ex.invoke(agents["places"], name, inputs, chat_level=0))


def tomorrow_at_eight():
    moment = datetime.now(timezone(timedelta(hours=4))) + timedelta(days=1)
    return moment.replace(hour=8, minute=0, second=0, microsecond=0).isoformat()


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["places"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_every_function_is_a_read(self, agents):
        levels = {f["permission_level"] for t in agents["places"].manifest.document["tools"]
                  for f in t["functions"]}
        assert levels == {0}

    def test_the_key_is_typed_in_and_encrypted(self, agents):
        maps = agents["places"].manifest.resource("secrets", "maps")
        assert "oauth" not in maps
        fields = {f["name"]: (f["type"], f["storage"], f["required"]) for f in maps["fields"]}
        assert fields == {"api_key": ("secret", "values", True),
                          "api_base_url": ("string", "keys", False)}


class TestTheKey:
    def test_status_confirms_both_apis(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.account.status", {})
        assert status == "success", result
        assert result == {"connected": True, "places_api": "ok", "routes_api": "ok"}
        # The probes are the cheap ones: ids only, and a single element.
        assert maps.masks(SEARCH) == ["places.id"]
        assert maps.requests[-1]["path"] == MATRIX
        assert len(maps.requests[-1]["body"]["destinations"]) == 1

    def test_a_bad_key_is_auth_and_says_what_to_check(self, agents, maps):
        ex = executor(maps, api_key="wrong-key")
        result, status = invoke(agents, ex, "places.places.search", {"query": "coffee"})
        assert status == "error" and result["kind"] == "auth"
        assert "invalid" in result["error"] and "Credentials tab" in result["error"]
        answer, status = invoke(agents, ex, "places.account.status", {})
        assert status == "success" and answer["connected"] is False
        assert "invalid" in answer["places_api"] and "invalid" in answer["routes_api"]

    def test_an_api_not_enabled_is_named(self, agents, maps):
        maps.disabled = {"routes"}
        ex = executor(maps)
        answer, status = invoke(agents, ex, "places.account.status", {})
        assert status == "success", answer
        assert answer["connected"] is False and answer["places_api"] == "ok"
        assert "Routes API is not enabled" in answer["routes_api"]
        assert "enable the Routes API" in answer["problem"]

        maps.disabled = {"places"}
        result, status = invoke(agents, ex, "places.places.details", {"place_id": "pl_marina_brew"})
        assert status == "error" and result["kind"] == "auth"
        assert "Places API (New) is not enabled" in result["error"]

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "places.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Google Maps Platform key" in result["problem"]
        result, status = invoke(agents, ex, "places.places.search", {"query": "coffee"})
        assert status == "error" and result["kind"] == "auth"
        assert "No Google Maps Platform key" in result["error"]


class TestFieldMasks:
    def test_the_stub_refuses_a_request_without_a_mask(self, maps):
        """Google refuses a request with no field mask; so does the stub —
        it never answers such a request with everything."""
        answer = requests.post(maps.url + SEARCH, json={"textQuery": "coffee"},
                               headers={"X-Goog-Api-Key": "test-key"}, timeout=5)
        assert answer.status_code == 400
        assert "FieldMask is a required parameter" in answer.json()["error"]["message"]

    def test_the_stub_returns_only_the_fields_a_mask_names(self, maps):
        """So a field the client forgets to name is missing from its
        answer, and that function's own test fails."""
        answer = requests.post(maps.url + SEARCH, json={"textQuery": "Marina Brew House"},
                               headers={"X-Goog-Api-Key": "test-key",
                                        "X-Goog-FieldMask": "places.displayName"}, timeout=5)
        assert answer.status_code == 200
        assert all(set(p) == {"displayName"} for p in answer.json()["places"])

    def test_every_call_names_its_fields_and_never_asks_for_everything(self, agents, maps):
        ex = executor(maps)
        invoke(agents, ex, "places.places.search", {"query": "coffee"})
        invoke(agents, ex, "places.places.nearby", {"place_id": "pl_marina_brew", "types": ["parking"]})
        invoke(agents, ex, "places.places.details", {"place_id": "pl_marina_brew"})
        invoke(agents, ex, "places.routes.distance", {
            "origin": MARINA, "destinations": [{"place_id": "pl_harbourline"}]})
        invoke(agents, ex, "places.routes.between", {
            "origin": MARINA, "destination": {"place_id": "pl_harbourline"}})
        paths = {r["path"] for r in maps.requests}
        assert paths == {SEARCH, NEARBY, "/places/pl_marina_brew", MATRIX, ROUTES}
        for request in maps.requests:
            assert request["mask"] and "*" not in request["mask"], request
        # Resolving a place to a point asks for its location and nothing more.
        assert maps.masks("/places/pl_marina_brew")[0] == "location"


class TestPlaces:
    def test_search_returns_googles_rows_only(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.search", {
            "query": "coffee near Dubai Marina", "open_now": True, **MARINA, "radius_m": 2000})
        assert status == "success", result
        names = [p["name"] for p in result["places"]]
        assert names[0] == "Marina Brew House" and "Dhow & Bean" not in names
        assert result["count"] == len(names)
        assert result["places"][0] == {
            "place_id": "pl_marina_brew", "name": "Marina Brew House",
            "address": "12 Waterfront Walk, Dubai Marina, Dubai",
            "lat": 25.0805, "lng": 55.1403, "rating": 4.6, "rating_count": 812,
            "price_level": "moderate", "open_now": True,
            "maps_link": "https://maps.google.com/?cid=1001"}
        sent = maps.requests[-1]["body"]
        assert sent["openNow"] is True
        assert sent["locationBias"]["circle"] == {
            "center": {"latitude": 25.08, "longitude": 55.14}, "radius": 2000.0}

    def test_a_place_without_a_rating_is_shown_without_one(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.search", {
            "query": "Harbourline office Abu Dhabi", "max_results": 1})
        assert status == "success", result
        (office,) = result["places"]
        assert office["name"] == "Harbourline Office"
        assert not {"rating", "rating_count", "price_level", "open_now"} & set(office)

    def test_nearby_is_nearest_first_within_the_radius(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.nearby", {
            **MARINA, "types": ["cafe"], "radius_m": 1000})
        assert status == "success", result
        assert [p["name"] for p in result["places"]] == ["Marina Brew House", "Dhow & Bean"]
        distances = [p["straight_line_km"] for p in result["places"]]
        assert distances == sorted(distances) and distances[0] < 0.1
        assert maps.requests[-1]["body"]["rankPreference"] == "DISTANCE"

    def test_nearby_a_place_resolves_it_first(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.nearby", {
            "place_id": "pl_marina_brew", "types": ["parking"], "radius_m": 500})
        assert status == "success", result
        assert result["center"] == {"lat": 25.0805, "lng": 55.1403}
        assert [p["name"] for p in result["places"]] == ["Marina Gate Parking"]

    def test_details_reads_hours_phone_and_website(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.details",
                                {"place_id": "pl_marina_brew"})
        assert status == "success", result
        assert result["phone"] == "04 555 0101"
        assert result["international_phone"] == "+971 4 555 0101"
        assert result["website"] == "https://marinabrew.example"
        assert len(result["hours"]) == 7 and result["hours"][0].startswith("Monday:")
        assert result["open_now"] is True and result["business_status"] == "OPERATIONAL"

    def test_an_unknown_place_is_not_found(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.places.details",
                                {"place_id": "pl_nowhere"})
        assert status == "error" and result["kind"] == "not_found"


class TestRoutes:
    def test_distance_to_several_destinations_in_input_order(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.routes.distance", {
            "origin": {"address": "Dubai Marina, Dubai"},
            "destinations": [{"place_id": "pl_harbourline"}, {"place_id": "pl_palmgrove_airport"},
                             {"place_id": "pl_pearl_rock"}, {"address": "Nowhere Street 99"}]})
        assert status == "success", result
        assert result["mode"] == "drive" and result["traffic_aware"] is False
        assert result["estimated_at"]
        office, airport, island, unknown = result["results"]
        assert [r["index"] for r in result["results"]] == [0, 1, 2, 3]
        # The stub's own numbers, passed through: 1.25 x the straight line
        # at 60 km/h.
        assert office["found"] is True and 120 < office["distance_km"] < 160
        assert office["duration_minutes"] == pytest.approx(office["distance_km"], abs=0.2)
        assert "duration_in_traffic_minutes" not in office
        assert airport["found"] is True and airport["distance_km"] < office["distance_km"]
        assert island == {**island, "found": False, "problem": "ROUTE_NOT_FOUND"}
        assert unknown["found"] is False and "geocoded" in unknown["problem"]
        link = parse_qs(urlparse(office["directions_link"]).query)
        assert link["destination_place_id"] == ["pl_harbourline"]
        assert link["origin"] == ["Dubai Marina, Dubai"] and link["travelmode"] == ["driving"]
        assert "routingPreference" not in maps.requests[-1]["body"]

    def test_driving_at_a_time_gives_traffic_and_without(self, agents, maps):
        leave = tomorrow_at_eight()
        result, status = invoke(agents, executor(maps), "places.routes.distance", {
            "origin": MARINA, "destinations": [{"place_id": "pl_harbourline"}],
            "departure_time": leave})
        assert status == "success", result
        assert result["traffic_aware"] is True and result["departure_time"].endswith("Z")
        (office,) = result["results"]
        assert office["duration_in_traffic_minutes"] > office["duration_minutes"]
        sent = maps.requests[-1]["body"]
        assert sent["routingPreference"] == "TRAFFIC_AWARE"
        assert sent["departureTime"] == result["departure_time"]

    def test_walking_asks_for_no_traffic(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.routes.distance", {
            "origin": MARINA, "destinations": [{"place_id": "pl_lantern"}],
            "mode": "walk", "departure_time": tomorrow_at_eight()})
        assert status == "success", result
        assert result["traffic_aware"] is False
        assert "routingPreference" not in maps.requests[-1]["body"]
        assert "walking" in result["results"][0]["directions_link"]

    def test_bad_locations_and_times_are_refused_before_google(self, agents, maps):
        ex = executor(maps)
        result, status = invoke(agents, ex, "places.routes.distance", {
            "origin": {"address": "Dubai Marina, Dubai", "place_id": "pl_lantern"},
            "destinations": [{"place_id": "pl_harbourline"}]})
        assert status == "error" and "exactly one" in result["error"]
        result, status = invoke(agents, ex, "places.routes.distance", {
            "origin": MARINA, "destinations": [{"place_id": "pl_harbourline"}],
            "departure_time": "2026-09-14T08:00:00"})
        assert status == "error" and "offset" in result["error"]
        result, status = invoke(agents, ex, "places.routes.between", {
            "origin": MARINA, "destination": {"place_id": "pl_harbourline"},
            "departure_time": "2020-01-01T08:00:00+04:00"})
        assert status == "error" and "past" in result["error"]
        assert maps.requests == []

    def test_between_summarises_one_trip(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.routes.between", {
            "origin": {"place_id": "pl_marina_brew"},
            "destination": {"address": "Harbourline Office"}})
        assert status == "success", result
        assert result["found"] is True and result["summary"] == "E11"
        assert result["warnings"] == ["This route has tolls."]
        assert result["steps_total"] == 3
        assert [s["instruction"] for s in result["steps"]][1] == "Merge onto E11"
        assert sum(s["distance_km"] for s in result["steps"]) == pytest.approx(
            result["distance_km"], abs=0.1)

    def test_between_with_no_route_says_so(self, agents, maps):
        result, status = invoke(agents, executor(maps), "places.routes.between", {
            "origin": MARINA, "destination": {"place_id": "pl_pearl_rock"}})
        assert status == "success", result
        assert result["found"] is False and "no route" in result["problem"]
        assert "duration_minutes" not in result
