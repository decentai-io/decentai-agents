"""The Google Docs agent (see manifest.yaml).

Reads a Google Doc as numbered paragraphs, reads and writes its
comments, and changes its text only in two steps: an edit is proposed
and recorded (nothing in the document moves), then applied as a
separate level-3 call that Google refuses if the document changed
since the proposal.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, CommentsTool, DocsTool, EditsTool


class GoogleDocsAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), DocsTool(self), CommentsTool(self), EditsTool(self)]
