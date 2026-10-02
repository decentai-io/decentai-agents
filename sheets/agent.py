"""The Spreadsheets agent (see manifest.yaml).

Every number is added in code, every match is a rule the user gave,
every produced workbook carries its change log and is read back before
it is returned. The original file is never modified.
"""

from decentai_sdk.base import AgentBase

from .tools import CalcTool, CheckTool, CleanTool, CombineTool, ReadTool, ReconcileTool


class SheetsAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), CheckTool(self), ReconcileTool(self),
                CleanTool(self), CombineTool(self), CalcTool(self)]
