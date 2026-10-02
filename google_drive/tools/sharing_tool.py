"""Who can open a file, and changing that on authorization.

The same contract as the OneDrive agent's sharing tool. Drive has no
separate link object: sharing by link opens the file to the user's
domain, or to anyone, and the file's own address is the link.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .drive_api import GoogleError, permission_row

PERSONAL_DOMAINS = {"gmail.com", "googlemail.com"}


class SharingTool(ToolBase):
    id = "sharing"

    async def permissions(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            granted = client.permissions(str(call.inputs["item_id"]))
        except GoogleError as exc:
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
        granted, ids, problem = [], [], None
        try:
            name = str(client.item(item_id).get("name") or "")
        except GoogleError as exc:
            return failure(exc)
        # Drive grants one person per request; stop at the first refusal
        # and keep what was already granted on record.
        for recipient in recipients:
            try:
                made = client.add_permission(item_id, {
                    "type": "user", "role": "writer" if role == "write" else "reader",
                    "emailAddress": recipient}, notify=True,
                    message=str(inputs.get("message") or ""))
            except GoogleError as exc:
                problem = exc
                break
            granted.append(recipient)
            ids.append(str(made.get("id") or ""))
        share_ref = ""
        if granted:
            record = await call.resources.create_data("share", {
                "item_id": item_id, "name": name, "kind": "invite",
                "who": ", ".join(granted), "role": role,
                "permission_id": ",".join(ids), "status": "granted"})
            share_ref = record["resource_ref"]
        if problem is not None:
            done = f" Already shared with: {', '.join(granted)}." if granted else ""
            return {"error": problem.message + done, "kind": problem.kind}, "error"
        return {"share_ref": share_ref, "invited": granted, "role": role}, "success"

    async def link(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "organization")
        role = "writer" if link_type == "edit" else "reader"
        try:
            item = client.item(item_id)
            email = client.email or str((client.about().get("user") or {}).get("emailAddress") or "")
            domain = email.rpartition("@")[2].lower()
            if scope == "organization":
                if not domain or domain in PERSONAL_DOMAINS:
                    return {"error": "A personal Google account has no organization to "
                                     "share with; ask whether anyone with the link is "
                                     "meant."}, "error"
                body = {"type": "domain", "domain": domain, "role": role,
                        "allowFileDiscovery": False}
                reach = f"people at {domain}"
            else:
                body = {"type": "anyone", "role": role, "allowFileDiscovery": False}
                reach = "anyone with the link"
            made = client.add_permission(item_id, body, notify=False)
        except GoogleError as exc:
            return failure(exc)
        url = str(item.get("webViewLink") or "")
        record = await call.resources.create_data("share", {
            "item_id": item_id, "name": str(item.get("name") or ""), "kind": "link",
            "who": reach, "role": "write" if link_type == "edit" else "read",
            "link": url, "permission_id": str(made.get("id") or ""), "status": "granted"})
        return {"share_ref": record["resource_ref"], "link": url, "reach": reach}, "success"

    async def revoke(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        permission_id = str(call.inputs["permission_id"])
        try:
            item = client.item(item_id)
            current = {str(p.get("id") or ""): p for p in client.permissions(item_id)}
        except GoogleError as exc:
            return failure(exc)
        if permission_id not in current:
            return {"error": "That item has no such permission; sharing.permissions "
                             "lists the ones it has."}, "error"
        row = permission_row(current[permission_id])
        if row["via"] == "owner":
            return {"error": "The owner's access cannot be removed."}, "error"
        if row["via"] == "inherited":
            return {"error": "This access comes from the shared drive; change it "
                             "there instead."}, "error"
        try:
            client.delete_permission(item_id, permission_id)
        except GoogleError as exc:
            return failure(exc)
        record = await call.resources.create_data("share", {
            "item_id": item_id, "name": str(item.get("name") or ""),
            "kind": "link" if row["via"] == "link" else "invite",
            "who": row["who"], "role": "write" if row["role"] == "write" else "read",
            "link": str(item.get("webViewLink") or "") if row["via"] == "link" else "",
            "permission_id": permission_id, "status": "revoked"})
        return {"revoked": True, "share_ref": record["resource_ref"]}, "success"
