"""The Word Online agent (see manifest.yaml).

Word documents in OneDrive, read from the file itself — Graph has no
API for a document's paragraphs or its comments, so the agent
downloads the .docx and reads the package. Text changes in two steps:
an edit is proposed and recorded, then applied as a separate level-3
call that uploads the changed file only if nobody saved it since.
"""

from decentai_sdk.base import AgentBase

from .tools import AccountTool, ChangesTool, CommentsTool, DocsTool, EditsTool


class WordOnlineAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), DocsTool(self), CommentsTool(self),
                EditsTool(self), ChangesTool(self)]
