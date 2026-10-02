"""The Documents agent (see manifest.yaml).

Reads what a person uploaded, page by page and honestly about pages it
cannot read; extracts fields only with a quoted passage behind each;
compares versions by arithmetic; and produces Word and PDF files it
reads back before returning. Stateless: every file is a platform file,
every extraction a record.
"""

from decentai_sdk.base import AgentBase

from .tools import CompareTool, ProduceTool, ReadTool


class DocumentsAgent(AgentBase):
    def tools(self):
        return [ReadTool(self), CompareTool(self), ProduceTool(self)]
