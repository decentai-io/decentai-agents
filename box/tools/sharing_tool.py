"""Who can open a file, and changing that on authorization.

The same contract as the other drive agents' sharing tools. In Box a
person is given access by a collaboration — its id is the permission
id — and an item has at most one shared link, whose permission id is
"link". Access granted on a folder above reaches the item too; that is
shown as inherited and changed on the folder, not here.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .box_api import BoxError, access_rows, kind_of


class SharingTool(ToolBase):
    id = "sharing"

    @staticmethod
    def _access(client, item):
        return access_rows(item, client.collaborations(kind_of(item), str(item["id"])))

    async def permissions(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.item(str(call.inputs["item_id"]))
            rows = self._access(client, item)
        except BoxError as exc:
            return failure(exc)
        return {"permissions": rows}, "success"

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
        role = str(inputs.get("role") or "read")
        try:
            item = client.item(str(inputs["item_id"]))
        except BoxError as exc:
            return failure(exc)
        granted, ids, problem = [], [], None
        # Box grants one person per request; stop at the first refusal
        # and keep what was already granted on record.
        for recipient in recipients:
            try:
                made = client.collaborate(kind_of(item), str(item["id"]), recipient, role)
            except BoxError as exc:
                problem = exc
                break
            granted.append(recipient)
            ids.append(str(made.get("id") or ""))
        share_ref = ""
        if granted:
            record = await call.resources.create_data("share", {
                "item_id": str(item["id"]), "name": str(item.get("name") or ""),
                "kind": "invite", "who": ", ".join(granted), "role": role,
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
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "organization")
        try:
            item = client.item(str(call.inputs["item_id"]))
            kind = kind_of(item)
            if link_type == "edit" and kind == "folder":
                return {"error": "Box lets a link edit a file, not a folder; share the "
                                 "folder with people instead."}, "error"
            if scope == "organization":
                enterprise = client.me().get("enterprise") or {}
                if not enterprise:
                    return {"error": "A personal Box account has no company to share "
                                     "with; ask whether anyone with the link is "
                                     "meant."}, "error"
                access = "company"
                reach = f"people at {enterprise.get('name')}" if enterprise.get("name") \
                    else "people in your company"
            else:
                access, reach = "open", "anyone with the link"
            settings = {"access": access, "permissions": {"can_download": True}}
            if kind == "file":
                settings["permissions"]["can_edit"] = link_type == "edit"
            updated = client.set_link(kind, str(item["id"]), settings)
        except BoxError as exc:
            return failure(exc)
        url = str((updated.get("shared_link") or {}).get("url") or "")
        record = await call.resources.create_data("share", {
            "item_id": str(item["id"]), "name": str(item.get("name") or ""), "kind": "link",
            "who": reach, "role": "write" if link_type == "edit" else "read",
            "link": url, "permission_id": "link", "status": "granted"})
        return {"share_ref": record["resource_ref"], "link": url, "reach": reach}, "success"

    async def revoke(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        permission_id = str(call.inputs["permission_id"])
        try:
            item = client.item(str(call.inputs["item_id"]))
            rows = self._access(client, item)
        except BoxError as exc:
            return failure(exc)
        row = next((r for r in rows if r["permission_id"] == permission_id), None)
        if row is None:
            return {"error": "That item has no such permission; sharing.permissions "
                             "lists the ones it has."}, "error"
        if row["via"] == "owner":
            return {"error": "The owner's access cannot be removed."}, "error"
        if row["via"] == "inherited":
            return {"error": "This access comes from a folder above; stop sharing the "
                             "folder instead."}, "error"
        try:
            if permission_id == "link":
                client.set_link(kind_of(item), str(item["id"]), None)
            else:
                client.remove_collaboration(permission_id)
        except BoxError as exc:
            return failure(exc)
        record = await call.resources.create_data("share", {
            "item_id": str(item["id"]), "name": str(item.get("name") or ""),
            "kind": "link" if row["via"] == "link" else "invite",
            "who": row["who"], "role": "write" if row["role"] == "write" else "read",
            "link": row["link"], "permission_id": permission_id, "status": "revoked"})
        return {"revoked": True, "share_ref": record["resource_ref"]}, "success"
