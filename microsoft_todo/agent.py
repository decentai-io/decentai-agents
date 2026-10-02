"""The Microsoft To Do agent (see manifest.yaml).

A person's own Microsoft To Do lists over Microsoft Graph. Reading is
level 0; creating, editing, completing and reopening a personal task is
level 1, like a mail draft — private and inert until the person acts on
it; deleting one is level 3. A watch keeps a cursor in Graph's own
clock, so a schedule can hand on only what was ticked off or changed
since the last look.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ListsTool, SyncTool, TasksTool


class MicrosoftTodoAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), ListsTool(self), TasksTool(self), SyncTool(self)]
