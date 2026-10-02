"""The Google Tasks agent (see manifest.yaml).

A person's own Google Tasks lists over the Tasks API. Reading is level
0; creating, editing, completing and reopening a personal task is level
1, like a Gmail draft — private and inert until the person acts on it;
deleting one is level 3. A watch keeps a cursor in Google's own clock,
so a schedule can hand on only what was ticked off or changed since the
last look.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ListsTool, SyncTool, TasksTool


class GoogleTasksAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ListsTool(self), TasksTool(self), SyncTool(self)]
