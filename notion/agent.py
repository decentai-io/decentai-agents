"""The Notion agent (see manifest.yaml).

One Notion workspace, as far as the person shared it with the
integration while signing in: pages and databases found, read and
queried freely; pages, rows, blocks and comments written only as
level-3 actions; a database watched for new and changed rows on a
schedule.
"""

from decentai_sdk.base import AgentBase

from .tools import (AccountTool, BlocksTool, CommentsTool, DatabasesTool,
                    PagesTool, SearchTool, WatchTool)


class NotionAgent(AgentBase):
    def tools(self):
        return [AccountTool(self), SearchTool(self), PagesTool(self),
                DatabasesTool(self), BlocksTool(self), CommentsTool(self),
                WatchTool(self)]
