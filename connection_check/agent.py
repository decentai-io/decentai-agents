"""The Connection Check agent (see manifest.yaml).

Stateless, and it keeps nothing: every call opens connections from
where this agent runs, closes them, and says what happened. Nothing is
sent to a host it tries and nothing is read from one.
"""

from decentai_sdk.base import AgentBase

from .tools import CheckTool


class ConnectionCheckAgent(AgentBase):
    def tools(self):
        return [CheckTool(self)]
