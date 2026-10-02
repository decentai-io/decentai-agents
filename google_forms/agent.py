"""The Google Forms agent (see manifest.yaml).

Reads Google Forms and their responses over the Forms API, and watches
a form for responses submitted since the last look. It changes nothing
in Google: every function is a read or a watch record.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, FormsTool, ResponsesTool


class GoogleFormsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), FormsTool(self), ResponsesTool(self)]
