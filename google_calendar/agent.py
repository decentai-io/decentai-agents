"""The Google Calendar agent (see manifest.yaml).

Stateless: every Google call mints its access token from the bound
credential, every proposal and booking is a platform record. Slot
finding is arithmetic over free/busy, never a model's guess; booking
is a separate level-3 action.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, EventsTool


class GoogleCalendarAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), EventsTool(self)]
