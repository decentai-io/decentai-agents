"""The Microsoft Calendar agent (see manifest.yaml).

Google Calendar's twin over Microsoft Graph: the same functions, the
same records, the same rules. Graph speaks UTC to it and it does the
arithmetic itself; finding a time asks no model; creating, moving and
cancelling are level-3 actions that wait for the user.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, EventsTool


class MicrosoftCalendarAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), EventsTool(self)]
