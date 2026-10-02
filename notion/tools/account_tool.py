from decentai_sdk.base import ToolBase

from .notion_api import NotionClient, NotionError


async def client_for(call):
    try:
        secret = await call.resources.use_secret("notion")
    except Exception as exc:
        return None, f"No Notion workspace is connected to this agent: {exc}"
    return NotionClient(secret), ""


def failure(exc: NotionError):
    result = {"error": exc.message, "kind": exc.kind}
    if exc.retry_after:
        result["retry_after_seconds"] = exc.retry_after
    return result, "error"


def invalid(message: str):
    return {"error": message, "kind": "invalid"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "workspace": "", "problem": why}, "success"
        try:
            me = client.me()
        except NotionError as exc:
            return {"connected": False, "workspace": "", "problem": exc.message}, "success"
        bot = me.get("bot") or {}
        result = {"connected": True,
                  "workspace": str(bot.get("workspace_name") or ""),
                  "integration": str(me.get("name") or "")}
        if client.account:
            result["account"] = client.account
        return result, "success"
