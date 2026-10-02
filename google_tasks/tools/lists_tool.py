"""Task lists: which there are, and making a new one."""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .google_api import GoogleError, second


def list_row(row):
    return {
        "list_id": str(row.get("id") or ""),
        "name": str(row.get("title") or ""),
        "updated": second(row.get("updated")),
    }


class ListsTool(ToolBase):
    id = "lists"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        most = int(call.inputs.get("max_results") or 20)
        try:
            rows, more = client.lists(most)
        except GoogleError as exc:
            return failure(exc)
        return {"lists": [list_row(r) for r in rows], "more": more}, "success"

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            created = client.create_list(str(call.inputs["name"]).strip())
        except GoogleError as exc:
            return failure(exc)
        return list_row(created), "success"
