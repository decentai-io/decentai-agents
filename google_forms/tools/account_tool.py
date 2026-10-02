from decentai_sdk.base import ToolBase

from .forms_api import FormsClient, GoogleError


async def client_for(call):
    """The Forms client over the bound credential, or the reason there
    is none — one place, so every function words it the same way."""
    try:
        secret = await call.resources.use_secret("google")
    except Exception as exc:
        return None, f"No Google account is connected to this agent: {exc}"
    return FormsClient(secret), ""


def failure(exc: GoogleError):
    """A GoogleError as the error result the assistant reads: the kind
    travels so it can tell an expired connection from a refusal."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            about = client.about()
        except GoogleError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str((about.get("user") or {}).get("emailAddress") or client.email)}, "success"
