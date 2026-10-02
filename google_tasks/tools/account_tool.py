from decentai_sdk.base import ToolBase

from .google_api import GoogleError, TasksClient


async def client_for(call):
    try:
        secret = await call.resources.use_secret("google")
    except Exception as exc:
        return None, f"No Google account is connected to this agent: {exc}"
    return TasksClient(secret), ""


def failure(exc: GoogleError):
    return {"error": exc.message, "kind": exc.kind}, "error"


def invalid(message: str):
    return {"error": message, "kind": "invalid"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            # The Tasks API has no profile of its own; reading one list is
            # what proves the grant reaches the tasks.
            client.lists(1)
        except GoogleError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True, "email": client.email}, "success"
