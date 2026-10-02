"""The Microsoft Forms agent (see manifest.yaml).

Microsoft Forms has no supported API for forms or their responses, so
this agent reads the one place a form's responses are kept that Graph
does serve: an Excel workbook in OneDrive whose table gains a row per
response. It finds such workbooks, lists responses, and watches one so
a schedule wakes the assistant only when a response arrived. It
changes nothing, in Forms or in the workbook.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, FormsTool, ResponsesTool


class MicrosoftFormsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FormsTool(self), ResponsesTool(self)]
