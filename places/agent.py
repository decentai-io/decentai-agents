"""The Places and Distances agent (see manifest.yaml).

Stateless: every call reads the organization's Google Maps Platform
key from the bound credential and asks Google. Nothing is kept, and
nothing here is estimated — every place, rating and travel time is a
value from Google's answer.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, PlacesTool, RoutesTool


class PlacesAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), PlacesTool(self), RoutesTool(self)]
