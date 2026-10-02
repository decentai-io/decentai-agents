from decentai_sdk.base import ToolBase

from .dropbox_api import DropboxClient, DropboxError


async def client_for(call):
    try:
        secret = await call.resources.use_secret("dropbox")
    except Exception as exc:
        return None, f"No Dropbox account is connected to this agent: {exc}"
    return DropboxClient(secret), ""


def failure(exc: DropboxError):
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            account = client.account()
            space = client.space()
        except DropboxError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        allocation = space.get("allocation") or {}
        return {"connected": True,
                "email": str(account.get("email") or client.email),
                "team": str((account.get("team") or {}).get("name") or ""),
                "used_bytes": int(space.get("used") or 0),
                "total_bytes": int(allocation.get("allocated") or 0)}, "success"
