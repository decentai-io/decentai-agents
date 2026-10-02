from decentai_sdk.base import ToolBase

from .slack_api import SlackClient, SlackError


async def client_for(call):
    try:
        secret = await call.resources.use_secret("slack")
    except Exception as exc:
        return None, f"No Slack account is connected to this agent: {exc}"
    return SlackClient(secret), ""


def failure(exc: SlackError):
    return {"error": exc.message, "kind": exc.kind}, "error"


def not_connected(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "problem": why}, "success"
        try:
            me = client.me()
        except SlackError as exc:
            return {"connected": False, "problem": exc.message}, "success"
        return {"connected": True, "team": str(me.get("team") or ""),
                "user": str(me.get("user") or ""),
                "user_id": str(me.get("user_id") or "")}, "success"
