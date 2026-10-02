from decentai_sdk.base import ToolBase

from .gmail import GmailClient, GmailError


async def client_for(call, account=None):
    """The Gmail client over the connected account, or the reason
    there is none — one place, so every function words it the same way.

    A person may have connected more than one account. The one wanted
    is named by address — the function's ``account`` input, or the
    account a draft or a watch was made on — and matched among the
    accounts this agent may use (list_secrets). Unnamed, the platform's
    own choice answers: the chat's binding, the default, or the only
    one; several with no default is refused, and the refusal names them
    so the assistant can ask."""
    wanted = str(account if account is not None
                 else call.inputs.get("account") or "").strip().lower()
    try:
        if wanted:
            rows = await call.resources.list_secrets("google")
            row = _match(rows, wanted)
            if row is None and len(rows) > 1:
                return None, (f"No connected Google account matches "
                              f"{wanted!r}. Connected: {_names(rows)}.")
            # One account connected is the one, whatever it was called
            # when the draft or the watch remembered it.
            secret = await call.resources.use_secret(
                "google",
                ref=row["resource_ref"] if row is not None else None)
        else:
            secret = await call.resources.use_secret("google")
    except Exception as exc:
        try:
            rows = await call.resources.list_secrets("google")
        except Exception:
            rows = []
        if len(rows) > 1:
            return None, (f"Several Google accounts are connected "
                          f"({_names(rows)}) and none is the default: say "
                          f"which with account.")
        return None, f"No Google account is connected to this agent: {exc}"
    return GmailClient(secret), ""


def _match(rows, wanted):
    """The row whose address is the one asked for, else the one whose
    name contains it — a person says "work" as readily as an address."""
    for row in rows:
        if str((row.get("keys") or {}).get("account") or "").lower() == wanted:
            return row
    for row in rows:
        if wanted in str(row.get("name") or "").lower():
            return row
    return None


def _names(rows):
    return ", ".join(
        str((row.get("keys") or {}).get("account") or row.get("name") or "?")
        for row in rows)


def _summary(row):
    keys = row.get("keys") or {}
    return {"account": str(keys.get("account") or ""),
            "name": str(row.get("name") or ""),
            "status": str(keys.get("status") or ""),
            "default": bool(row.get("is_default") or row.get("is_bound"))}


def failure(exc: GmailError):
    """A GmailError as the error result the assistant reads: the kind
    travels so it can tell an expired connection from a refusal from
    an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address — never a token."""
        try:
            rows = await call.resources.list_secrets("google")
        except Exception as exc:
            return {"error": f"The connected accounts could not be listed: {exc}",
                    "kind": "auth"}, "error"
        return {"accounts": [_summary(row) for row in rows]}, "success"

    async def list(self, call):
        """The accounts this agent may use, by address — never a token."""
        try:
            rows = await call.resources.list_secrets("google")
        except Exception as exc:
            return {"error": f"The connected accounts could not be listed: {exc}",
                    "kind": "auth"}, "error"
        return {"accounts": [_summary(row) for row in rows]}, "success"

    async def status(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            profile = client.profile()
        except GmailError as exc:
            return {"connected": False, "email": client.email,
                    "problem": exc.message}, "success"
        return {"connected": True,
                "email": str(profile.get("emailAddress") or client.email)}, "success"
