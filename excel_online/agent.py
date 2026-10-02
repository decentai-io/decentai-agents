"""The Excel Online agent (see manifest.yaml).

Reads workbooks in OneDrive freely; appends to a table or overwrites a
range only as level-3 actions that wait for the user, each recorded
with what it wrote and what it replaced; and watches a table or a
worksheet so a schedule wakes the assistant only when rows arrived.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, RangeTool, RowsTool, TableTool, WorkbooksTool


class ExcelOnlineAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), WorkbooksTool(self), RangeTool(self),
                TableTool(self), RowsTool(self)]
