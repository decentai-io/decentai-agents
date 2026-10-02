"""Who can open a file, and changing that on authorization.

Every grant and every revocation is kept as a share record, so "what
have I shared, and with whom" has an answer that does not depend on
remembering the chat.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph_drive import GraphError, permission_row

REACH = {"organization": "people in your organization",
         "anonymous": "anyone with the link"}


class SharingTool(ToolBase):
    id = "sharing"

    async def permissions(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            granted = client.permissions(str(call.inputs["item_id"]))
        except GraphError as exc:
            return failure(exc)
        return {"permissions": [permission_row(p) for p in granted]}, "success"

    async def invite(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        recipients = [str(r).strip() for r in inputs["recipients"] if str(r).strip()]
        names = [r for r in recipients if "@" not in r]
        if names:
            return {"error": "Recipients are email addresses; find the address for "
                             + ", ".join(names) + " first."}, "error"
        item_id = str(inputs["item_id"])
        role = str(inputs.get("role") or "read")
        try:
            name = str(client.item(item_id).get("name") or "")
            granted = client.invite(item_id, recipients, role, str(inputs.get("message") or ""))
        except GraphError as exc:
            return failure(exc)
        record = await call.resources.create_data("share", {
            "item_id": item_id, "name": name, "kind": "invite",
            "who": ", ".join(recipients), "role": role,
            "permission_id": ",".join(str(p.get("id") or "") for p in granted),
            "status": "granted"})
        return {"share_ref": record["resource_ref"], "invited": recipients,
                "role": role}, "success"

    async def link(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "organization")
        try:
            name = str(client.item(item_id).get("name") or "")
            made = client.create_link(item_id, link_type, scope)
        except GraphError as exc:
            return failure(exc)
        url = str((made.get("link") or {}).get("webUrl") or "")
        record = await call.resources.create_data("share", {
            "item_id": item_id, "name": name, "kind": "link", "who": REACH[scope],
            "role": "write" if link_type == "edit" else "read", "link": url,
            "permission_id": str(made.get("id") or ""), "status": "granted"})
        return {"share_ref": record["resource_ref"], "link": url,
                "reach": REACH[scope]}, "success"

    async def revoke(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        permission_id = str(call.inputs["permission_id"])
        try:
            name = str(client.item(item_id).get("name") or "")
            current = {str(p.get("id") or ""): p for p in client.permissions(item_id)}
        except GraphError as exc:
            return failure(exc)
        if permission_id not in current:
            return {"error": "That item has no such permission; sharing.permissions "
                             "lists the ones it has."}, "error"
        row = permission_row(current[permission_id])
        if row["via"] == "owner":
            return {"error": "The owner's access cannot be removed."}, "error"
        if row["via"] == "inherited":
            return {"error": "This access comes from a parent folder; stop sharing "
                             "the folder instead."}, "error"
        try:
            client.delete_permission(item_id, permission_id)
        except GraphError as exc:
            return failure(exc)
        record = await call.resources.create_data("share", {
            "item_id": item_id, "name": name,
            "kind": "link" if row["via"] == "link" else "invite",
            "who": row["who"], "role": row["role"], "link": row["link"],
            "permission_id": permission_id, "status": "revoked"})
        return {"revoked": True, "share_ref": record["resource_ref"]}, "success"
