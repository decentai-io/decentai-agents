from decentai_sdk.base import ToolBase

from .maps_api import MapsClient, MapsError

NO_KEY = ("No Google Maps Platform key is granted to this agent — an administrator "
          "adds one on the agent's Credentials tab")


async def client_for(call):
    try:
        secret = await call.resources.use_secret("maps")
    except Exception as exc:
        return None, f"{NO_KEY}: {exc}"
    return MapsClient(secret), ""


def failure(exc: MapsError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    # A fixed point, so the Routes probe needs no geocoding of its own.
    PROBE = {"latitude": 25.2048, "longitude": 55.2708}

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "places_api": "", "routes_api": "",
                    "problem": why}, "success"
        # Each API is probed on its own: a key enabled for one and not the
        # other is the commonest setup mistake, and the answer names which.
        places, routes = "ok", "ok"
        try:
            # Text Search asking only for ids is the cheapest Places call.
            client.search_text({"textQuery": "coffee", "pageSize": 1}, ["places.id"])
        except MapsError as exc:
            places = exc.message
        try:
            # One origin, one destination: a single billed matrix element.
            point = {"waypoint": {"location": {"latLng": self.PROBE}}}
            client.route_matrix({"origins": [point], "destinations": [point],
                                 "travelMode": "DRIVE"}, ["originIndex", "condition"])
        except MapsError as exc:
            routes = exc.message
        problems = [p for p in (places, routes) if p != "ok"]
        result = {"connected": not problems, "places_api": places, "routes_api": routes}
        if problems:
            result["problem"] = " ".join(problems)
        return result, "success"
