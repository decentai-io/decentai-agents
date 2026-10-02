"""The Google Sheets agent (see manifest.yaml).

Live spreadsheets over the Sheets API: reads them freely, writes cells
only as level-3 actions, and watches a tab for rows added since the
last look — which is how form responses and sign-ups arrive.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, RowsTool, SpreadsheetsTool, ValuesTool


class GoogleSheetsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SpreadsheetsTool(self), ValuesTool(self), RowsTool(self)]
