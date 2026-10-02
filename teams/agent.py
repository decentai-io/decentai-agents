"""The Microsoft Teams agent (see manifest.yaml).

Reads the account's chats as text; sends a chat message or posts to a
channel only as a level-3 action that waits for the user; and keeps
what it sent, so "did they answer" has an answer.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ChatsTool, TeamsTool


class TeamsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ChatsTool(self), TeamsTool(self)]
