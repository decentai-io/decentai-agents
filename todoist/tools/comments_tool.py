"""A comment on a task — visible to everyone the project is shared with,
which is why adding one is a level-3 action."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .todoist_api import TodoistError


class CommentsTool(ToolBase):
    id = "comments"

    async def add(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        task_id = str(call.inputs["task_id"])
        try:
            task = client.task(task_id)
            comment = client.add_comment(task_id, str(call.inputs["content"]))
        except TodoistError as exc:
            return failure(exc)
        if not comment.get("id"):
            return {"error": "Todoist accepted the comment but returned no id; "
                             "the outcome is unknown.", "kind": "unknown"}, "error"
        return {"comment_id": str(comment["id"]), "task_id": task_id,
                "task": str(task.get("content") or ""),
                "posted_at": str(comment.get("posted_at") or ""),
                "link": client.task_link(task_id)}, "success"
