"""The conversations the person is in, and what was said in them.

Everything here is a read. Answers stay small: at most 25 messages, the
text of each clipped, and one extra message fetched so ``more`` is
known without guessing.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, not_connected
from .slack_api import (People, SlackError, conversation_name, is_said, is_ts,
                        kind_of, message_row, ts_number)

TYPES = "public_channel,private_channel,mpim,im"


class ConversationsTool(ToolBase):
    id = "conversations"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        most = int(call.inputs.get("max_results") or 50)
        wanted = str(call.inputs.get("kind") or "").strip()
        try:
            me_id = str(client.me().get("user_id") or "")
            found = client.my_conversations(TYPES)
            if wanted:
                found = [c for c in found if kind_of(c) == wanted]
            people = People(client)
            rows = [{"conversation_id": str(c.get("id") or ""), "kind": kind_of(c),
                     "name": conversation_name(c, people, client, me_id)}
                    for c in found[:most]]
        except SlackError as exc:
            return failure(exc)
        return {"conversations": rows, "more": len(found) > most}, "success"

    async def messages(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        oldest = str(call.inputs.get("oldest") or "").strip()
        latest = str(call.inputs.get("latest") or "").strip()
        for label, value in (("oldest", oldest), ("latest", latest)):
            if value and not is_ts(value):
                return {"error": f"{label} is a Slack ts such as 1712345678.123456, "
                                 f"not {value!r}.", "kind": "invalid"}, "error"
        try:
            me = client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            people = People(client)
            name = conversation_name(client.conversation(channel), people, client, me_id)
            page = client.history(channel, most + 1, oldest=oldest, latest=latest)
            found = [m for m in page.get("messages") or [] if is_said(m)]
            more = len(found) > most or bool(page.get("has_more"))
            found = sorted(found[:most], key=lambda m: ts_number(m.get("ts")))
            rows = [message_row(m, channel, people, me_id, team_url) for m in found]
        except SlackError as exc:
            return failure(exc)
        return {"conversation_id": channel, "name": name, "messages": rows,
                "more": more}, "success"

    async def replies(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        thread_ts = str(call.inputs["thread_ts"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        if not is_ts(thread_ts):
            return {"error": f"thread_ts is a Slack ts such as 1712345678.123456, "
                             f"not {thread_ts!r}.", "kind": "invalid"}, "error"
        try:
            me = client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            # The parent comes first and is counted; one more than asked
            # for says whether the thread goes on.
            page = client.replies(channel, thread_ts, most + 1)
            found = [m for m in page.get("messages") or [] if is_said(m)]
            people = People(client)
            rows = [message_row(m, channel, people, me_id, team_url) for m in found[:most]]
        except SlackError as exc:
            return failure(exc)
        return {"conversation_id": channel, "thread_ts": thread_ts, "messages": rows,
                "more": len(found) > most or bool(page.get("has_more"))}, "success"
