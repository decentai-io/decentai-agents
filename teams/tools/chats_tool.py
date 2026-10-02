"""Reading chats, sending on authorization, and whether anyone answered.

Sending is level 3 in the manifest, so by the time it runs here it has
waited for the user. Every message sent is kept as a record, which is
what chats.replies reads from.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph_teams import (GraphError, chat_row, is_message, members_of,
                          message_row, moment)


class ChatsTool(ToolBase):
    id = "chats"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            me = client.profile()
            chats = client.chats()
        except GraphError as exc:
            return failure(exc)
        rows = [chat_row(c, str(me.get("id") or "")) for c in chats]
        person = str(call.inputs.get("with") or "").strip().lower()
        if person:
            rows = [r for r in rows if person in r["members"].lower()]
        rows.sort(key=lambda r: r["updated"], reverse=True)
        return {"chats": rows[: int(call.inputs.get("max_results") or 20)]}, "success"

    async def messages(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        chat_id = str(call.inputs["chat_id"])
        try:
            me_id = str(client.profile().get("id") or "")
            chat = client.chat(chat_id)
            found = client.messages(chat_id, int(call.inputs.get("max_results") or 20))
        except GraphError as exc:
            return failure(exc)
        members = members_of(chat)
        rows = [message_row(m, members, me_id) for m in found if is_message(m)]
        rows.reverse()
        return {"chat_id": chat_id, "topic": chat_row(chat, me_id)["topic"],
                "messages": rows}, "success"

    async def send(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        text = str(call.inputs["text"]).strip()
        chat_id = str(call.inputs.get("chat_id") or "").strip()
        to = str(call.inputs.get("to") or "").strip()
        if not text:
            return {"error": "There is nothing to send."}, "error"
        if not chat_id and not to:
            return {"error": "Give chat_id, or to (an email address) for a "
                             "one-to-one chat."}, "error"
        if to and "@" not in to:
            return {"error": f"to is an email address; find the address for {to} "
                             f"first."}, "error"
        try:
            me = client.profile()
            me_id = str(me.get("id") or "")
            if chat_id:
                who = chat_row(client.chat(chat_id), me_id)["topic"]
            else:
                chat_id = str(client.one_on_one(
                    str(me.get("userPrincipalName") or me.get("mail") or client.email), to)
                    .get("id") or "")
                who = to
            await call.progress(f"Sending to {who}")
            made = client.send(chat_id, text)
        except GraphError as exc:
            return failure(exc)
        record = await call.resources.create_data("sent", {
            "kind": "chat", "chat_id": chat_id, "to": who or chat_id, "text": text[:200],
            "message_id": str(made.get("id") or ""),
            "sent_at": str(made.get("createdDateTime") or ""),
            "link": str(made.get("webUrl") or "")})
        return {"sent_ref": record["resource_ref"], "chat_id": chat_id,
                "message_id": str(made.get("id") or ""),
                "sent_at": str(made.get("createdDateTime") or "")}, "success"

    async def replies(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        ref = str(call.inputs["sent_ref"])
        try:
            sent = (await call.resources.read_data("sent", ref)).get("keys") or {}
        except Exception:
            return {"error": f"No sent message {ref}."}, "error"
        if sent.get("kind") == "channel":
            return {"error": "Replies in a channel cannot be read here: that needs "
                             "a permission only an administrator can grant."}, "error"
        chat_id = str(sent.get("chat_id") or "")
        after = moment(sent.get("sent_at"))
        try:
            me_id = str(client.profile().get("id") or "")
            chat = client.chat(chat_id)
            found = client.messages(chat_id, 50)
        except GraphError as exc:
            return failure(exc)
        members = members_of(chat)
        rows = [message_row(m, members, me_id) for m in found if is_message(m)]
        rows = [r for r in rows if not r["mine"] and after is not None
                and (moment(r["sent"]) or after) > after]
        rows.reverse()
        return {"replied": bool(rows), "chat_id": chat_id, "replies": rows}, "success"
