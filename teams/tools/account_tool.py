from decentai_sdk.base import ToolBase

from .graph_teams import GraphError, TeamsClient


async def client_for(call):
    try:
        secret = await call.resources.use_secret("microsoft")
    except Exception as exc:
        return None, f"No Microsoft account is connected to this agent: {exc}"
    return TeamsClient(secret), ""


def failure(exc: GraphError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            profile = client.profile()
        except GraphError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(profile.get("mail") or profile.get("userPrincipalName")
                             or client.email),
                "name": str(profile.get("displayName") or "")}, "success"
