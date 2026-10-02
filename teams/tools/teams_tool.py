"""The teams the account belongs to, their channels, and posting to
one on authorization."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph_teams import GraphError


class TeamsTool(ToolBase):
    id = "teams"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            teams = client.joined_teams()
        except GraphError as exc:
            return failure(exc)
        return {"teams": [{"team_id": str(t.get("id") or ""),
                           "name": str(t.get("displayName") or ""),
                           "description": str(t.get("description") or "")}
                          for t in teams]}, "success"

    async def channels(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            channels = client.channels(str(call.inputs["team_id"]))
        except GraphError as exc:
            return failure(exc)
        return {"channels": [{"channel_id": str(c.get("id") or ""),
                              "name": str(c.get("displayName") or ""),
                              "kind": str(c.get("membershipType") or "standard")}
                             for c in channels]}, "success"

    async def post(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        team_id, channel_id = str(inputs["team_id"]), str(inputs["channel_id"])
        text = str(inputs["text"]).strip()
        if not text:
            return {"error": "There is nothing to post."}, "error"
        try:
            team = next((t for t in client.joined_teams() if str(t.get("id")) == team_id), None)
            if team is None:
                return {"error": "The account is not in that team; teams.list shows "
                                 "the ones it is in."}, "error"
            channel = next((c for c in client.channels(team_id) if str(c.get("id")) == channel_id), None)
            if channel is None:
                return {"error": "That team has no such channel; teams.channels lists "
                                 "them."}, "error"
            where = f"{team.get('displayName')} / {channel.get('displayName')}"
            await call.progress(f"Posting to {where}")
            made = client.post(team_id, channel_id, text, str(inputs.get("subject") or ""))
        except GraphError as exc:
            return failure(exc)
        record = await call.resources.create_data("sent", {
            "kind": "channel", "chat_id": channel_id, "team_id": team_id, "to": where,
            "text": text[:200], "message_id": str(made.get("id") or ""),
            "sent_at": str(made.get("createdDateTime") or ""),
            "link": str(made.get("webUrl") or "")})
        return {"sent_ref": record["resource_ref"], "message_id": str(made.get("id") or ""),
                "link": str(made.get("webUrl") or "")}, "success"
