from decentai_sdk.base import ToolBase

from .box_api import BoxClient, BoxError


async def client_for(call):
    try:
        secret = await call.resources.use_secret("box")
    except Exception as exc:
        return None, f"No Box account is connected to this agent: {exc}"
    return BoxClient(secret), ""


def failure(exc: BoxError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            me = client.me()
        except BoxError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(me.get("login") or client.email),
                "enterprise": str((me.get("enterprise") or {}).get("name") or ""),
                "used_bytes": int(me.get("space_used") or 0),
                "total_bytes": int(me.get("space_amount") or 0)}, "success"
