from decentai_sdk.base import ToolBase

from .mailbox import Mailbox
from .servers import Account, MailError

SECRET = "account"


async def account_for(call, account=None):
    """The account to work on, or the reason there is none — one place,
    so every function words it the same way.

    A person may have connected more than one account. The one wanted
    is named by address — the function's ``account`` input, or the
    account a draft or a watch was made on — and matched among the
    accounts this agent may use. Unnamed, the platform's own choice
    answers: the chat's binding, the default, or the only one; several
    with no default is refused, and the refusal names them so the
    assistant can ask."""
    wanted = str(account if account is not None
                 else call.inputs.get("account") or "").strip().lower()
    try:
        if wanted:
            rows = await call.resources.list_secrets(SECRET)
            row = _match(rows, wanted)
            if row is None and len(rows) > 1:
                return None, (f"No connected mail account matches {wanted!r}. "
                              f"Connected: {_names(rows)}.")
            secret = await call.resources.use_secret(
                SECRET, ref=row["resource_ref"] if row is not None else None)
        else:
            secret = await call.resources.use_secret(SECRET)
    except Exception as exc:
        try:
            rows = await call.resources.list_secrets(SECRET)
        except Exception:
            rows = []
        if len(rows) > 1:
            return None, (f"Several mail accounts are connected ({_names(rows)}) "
                          f"and none is the default: say which with account.")
        return None, (f"No mail account is connected to this agent — add one on "
                      f"the agent's Credentials tab: {exc}")
    found = Account(secret)
    problems = found.problems()
    if problems:
        return None, " ".join(problems)
    return found, ""


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
            "provider": str(keys.get("provider") or ""),
            "default": bool(row.get("is_default") or row.get("is_bound"))}


def failure(exc: MailError):
    """A MailError as the error result the assistant reads: the kind
    travels so it can tell a refused sign-in from a refusal of the
    platform's from an outcome nobody knows."""
    return {"error": exc.message, "kind": exc.kind}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def list(self, call):
        """The accounts this agent may use, by address — never a
        password."""
        try:
            rows = await call.resources.list_secrets(SECRET)
        except Exception as exc:
            return {"error": f"The connected accounts could not be listed: {exc}",
                    "kind": "auth"}, "error"
        return {"accounts": [_summary(row) for row in rows]}, "success"

    async def status(self, call):
        account, why = await account_for(call)
        if account is None:
            return {"connected": False, "email": "", "problem": why}, "success"
        try:
            with Mailbox(account) as mailbox:
                mailbox.open(Mailbox.INBOX)
        except MailError as exc:
            return {"connected": False, "email": account.email,
                    "problem": exc.message}, "success"
        return {"connected": True, "email": account.email}, "success"
