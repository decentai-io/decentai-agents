"""The Google Drive agent (see manifest.yaml).

OneDrive's twin over the Drive API: reads the drive freely, brings
copies in for other agents to read, and changes the drive — uploads,
folders, moves, trash, shares — only as level-3 actions.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, FilesTool, SharingTool


class GoogleDriveAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self)]
