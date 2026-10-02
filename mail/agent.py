"""The Mail agent (see manifest.yaml).

Stateless: every call signs in to the account's own mail server, does
its work and signs out. What is kept is kept by the platform — drafts
and watches, as records.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, DraftTool, SearchTool, WatchTool


class MailAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), DraftTool(self),
                WatchTool(self)]
