from decentai_sdk.base import ToolBase

from .todoist_api import TodoistClient, TodoistError


async def client_for(call):
    """The Todoist client over the connected account, or the reason there
    is none — one place, so every function words it the same way."""
    try:
        secret = await call.resources.use_secret("todoist")
    except Exception as exc:
        return None, f"No Todoist account is connected to this agent: {exc}"
    return TodoistClient(secret), ""


def failure(exc: TodoistError):
    """A TodoistError as the error result the assistant reads: the kind
    travels so it can tell a revoked connection from a refusal from an
    outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "full_name": "",
                    "timezone": "", "problem": why}, "success"
        try:
            user = client.user()
        except TodoistError as exc:
            return {"connected": False, "email": client.email, "full_name": "",
                    "timezone": "", "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(user.get("email") or client.email),
                "full_name": str(user.get("full_name") or ""),
                "timezone": str((user.get("tz_info") or {}).get("timezone") or "")}, "success"
