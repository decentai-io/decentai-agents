from decentai_sdk.base import ToolBase

from .google_api import CalendarClient, GoogleError


async def client_for(call):
    try:
        secret = await call.resources.use_secret("google")
    except Exception as exc:
        return None, f"No Google account is connected to this agent: {exc}"
    return CalendarClient(secret), ""


def failure(exc: GoogleError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "timezone": "",
                    "problem": why}, "success"
        try:
            primary = client.primary()
        except GoogleError as exc:
            return {"connected": False, "email": client.email, "timezone": "",
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(primary.get("id") or client.email),
                "timezone": str(primary.get("timeZone") or "UTC")}, "success"
