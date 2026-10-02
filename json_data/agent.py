"""The JSON agent (see manifest.yaml).

Reads what a person uploaded and says what is actually in it, answers by
path, checks against a schema with the validator's own verdict, compares
two versions by arithmetic, and writes CSV and JSON files it hands back
by ref. Stateless: every file is a platform file, every validation a
record.
"""

from decentai_sdk.base import AgentBase

from .tools import CheckTool, CompareTool, ReadTool, ShapeTool


class JsonDataAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), CheckTool(self), CompareTool(self),
                ShapeTool(self)]
