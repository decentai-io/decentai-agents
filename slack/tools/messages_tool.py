"""Finding messages, whether someone answered, and sending on
authorization.

Sending is level 3 in the manifest, so by the time it runs here it has
waited for the user. Every message sent is kept as a ``sent`` record
holding the ts Slack gave it, which is what messages.answered takes.
"""

from urllib.parse import parse_qs, urlparse

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, not_connected
from .slack_api import (People, SlackError, conversation_name, is_said, is_ts, kind_of,
                        message_row, permalink, ts_number, ts_time)

MOST_ROWS = 25


def search_row(match, people: People):
    """One search.messages match, shaped like a message row plus where
    it was said. Search does not return thread_ts; its permalink does."""
    channel = match.get("channel") or {}
    link = str(match.get("permalink") or "")
    thread_ts = (parse_qs(urlparse(link).query).get("thread_ts") or [""])[0]
    kind = kind_of(channel)
    name = str(channel.get("name") or channel.get("id") or "")
    if kind == "dm" and name[:1] in ("U", "W"):
        # A DM's "name" in search is the other person's user id.
        name = people.name(name)
    ts = str(match.get("ts") or "")
    return {"conversation_id": str(channel.get("id") or ""), "conversation": name,
            "kind": kind, "ts": ts, "thread_ts": thread_ts, "sent": ts_time(ts),
            "author": people.author(match), "author_id": str(match.get("user") or ""),
            "text": people.readable(match.get("text")), "link": link}


class MessagesTool(ToolBase):
    id = "messages"

    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        query = str(call.inputs["query"]).strip()
        most = int(call.inputs.get("max_results") or 20)
        if not query:
            return {"error": "Give something to search for.", "kind": "invalid"}, "error"
        try:
            found = client.search(query, most)
            people = People(client)
            rows = [search_row(m, people) for m in (found.get("matches") or [])[:most]]
        except SlackError as exc:
            return failure(exc)
        paging = found.get("paging") or {}
        total = int(found.get("total") or paging.get("total") or len(rows))
        return {"messages": rows, "total": total, "more": total > len(rows)}, "success"

    async def answered(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        ts = str(call.inputs["ts"]).strip()
        if not is_ts(ts):
            return {"error": f"ts is a Slack ts such as 1712345678.123456, not {ts!r}.",
                    "kind": "invalid"}, "error"
        after = ts_number(ts)
        try:
            me = client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            thread = client.replies(channel, ts, 200).get("messages") or []
            target = next((m for m in thread if str(m.get("ts")) == ts), None)
            if target is None:
                return {"error": f"There is no message {ts} in {channel}.",
                        "kind": "not_found"}, "error"
            root = str(target.get("thread_ts") or ts)
            if root != ts:
                # The message is itself a reply: its thread is its parent's.
                thread = client.replies(channel, root, 200).get("messages") or []
            later = client.history(channel, 100, oldest=ts).get("messages") or []
            seen, answers = set(), []
            for where, messages in (("thread", thread), ("conversation", later)):
                for message in messages:
                    key = str(message.get("ts") or "")
                    if (key in seen or ts_number(key) <= after or not is_said(message)
                            or str(message.get("user") or "") == me_id):
                        continue
                    seen.add(key)
                    answers.append((where, message))
            answers.sort(key=lambda pair: ts_number(pair[1].get("ts")))
            people = People(client)
            rows = [{**message_row(m, channel, people, me_id, team_url), "where": where}
                    for where, m in answers[:MOST_ROWS]]
        except SlackError as exc:
            return failure(exc)
        by = []
        for row in rows:
            if row["author"] not in by:
                by.append(row["author"])
        return {"answered": bool(answers), "by": by, "replies": rows,
                "more": len(answers) > MOST_ROWS}, "success"

    async def send(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        channel = str(call.inputs["conversation_id"]).strip()
        text = str(call.inputs["text"]).strip()
        thread_ts = str(call.inputs.get("thread_ts") or "").strip()
        if not text:
            return {"error": "There is nothing to send.", "kind": "invalid"}, "error"
        if thread_ts and not is_ts(thread_ts):
            return {"error": f"thread_ts is a Slack ts such as 1712345678.123456, "
                             f"not {thread_ts!r}.", "kind": "invalid"}, "error"
        try:
            me = client.me()
            me_id, team_url = str(me.get("user_id") or ""), str(me.get("url") or "")
            # Read before writing: an unknown conversation is refused here,
            # and the record names where it went in words.
            name = conversation_name(client.conversation(channel), People(client),
                                     client, me_id)
            await call.progress(f"Sending to {name}")
            made = client.post(channel, text, thread_ts)
        except SlackError as exc:
            return failure(exc)
        ts = str(made.get("ts") or "")
        channel = str(made.get("channel") or channel)
        link = permalink(team_url, channel, ts, thread_ts)
        record = await call.resources.create_data("sent", {
            "conversation_id": channel, "conversation": name, "thread_ts": thread_ts,
            "text": text[:200], "ts": ts, "sent_at": ts_time(ts), "link": link})
        return {"sent_ref": record["resource_ref"], "conversation_id": channel,
                "ts": ts, "thread_ts": thread_ts, "link": link}, "success"
