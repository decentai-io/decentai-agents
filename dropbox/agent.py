"""The Dropbox agent (see manifest.yaml).

The third twin of the Google Drive and OneDrive agents, over Dropbox
API v2: reads the account freely, brings copies in for other agents to
read, changes the Dropbox — uploads, folders, moves, deletes, shares —
only as level-3 actions, and watches a folder so a schedule wakes the
assistant only when something in it changed.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ChangesTool, FilesTool, SharingTool


class DropboxAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self), ChangesTool(self)]
