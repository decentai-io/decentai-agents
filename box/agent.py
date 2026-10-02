"""The Box agent (see manifest.yaml).

The fourth twin of the Google Drive, OneDrive and Dropbox agents, over
Box API 2.0: reads the account freely, brings copies in for other agents
to read, changes the Box — uploads, folders, moves, trash, shares —
only as level-3 actions, and watches the account's change stream so a
schedule wakes the assistant only when something changed.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ChangesTool, FilesTool, SharingTool


class BoxAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self), ChangesTool(self)]
