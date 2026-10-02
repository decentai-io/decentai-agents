"""The Notebook agent — DecentAI's reference agent (see manifest.yaml
and README.md).

Stateless by design: every note, document and secret flows through the
mediated ``call.resources`` — the platform's data layer, scoped per call
to what the function declared — so the agent itself holds nothing. The
sync REMOTE stays simulated (no network), but its connection secret is
real. The tools hold no state of their own.
"""

from decentai_sdk.base import AgentBase

from .tools import ArchiveTool, NoteTool, SyncTool


class NotebookAgent(AgentBase):
    def tools(self):
        return [NoteTool(self), ArchiveTool(self), SyncTool(self)]
