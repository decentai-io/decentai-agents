"""The Outlook agent (see manifest.yaml).

Stateless: every Graph call carries the access token the platform
handed it for the connected account, every draft and watch is a
platform record, and nothing is kept in the process. The agent never
sends on its own: a send is a separate level-3 function on a draft the
user has seen.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, DraftTool, SearchTool, WatchTool


class OutlookAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), DraftTool(self),
                WatchTool(self)]
