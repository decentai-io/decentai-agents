"""The OneDrive agent (see manifest.yaml).

Reads the drive freely; brings copies in for other agents to read; and
changes the drive — uploads, folders, moves, deletes, shares — only as
level-3 actions that wait for the user.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, FilesTool, SharingTool


class OneDriveAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FilesTool(self), SharingTool(self)]
