"""Who can open a file, and changing that on authorization.

The same contract as the drive twins' sharing tools. Dropbox shares a
file and a folder differently: a file has members of its own, while a
folder must first become a shared folder and then gains members. Links
are separate objects with their own audience. A permission id here
says how to take the access away — ``member:<address>``,
``account:<dbid>``, ``group:<id>`` or ``link:<url>`` — because Dropbox
has no single permission id to hand back.
"""

import time

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .dropbox_api import DropboxError, link_row, member_rows, tag

SHARE_JOB_POLLS = 5


class SharingTool(ToolBase):
    id = "sharing"

    @staticmethod
    def _access(client, item):
        """Everyone who can open the item, and the shared folder id its
        members belong to ("" for a file, or a folder nobody shares)."""
        sharing = item.get("sharing_info") or {}
        item_id = str(item.get("id") or "")
        if tag(item) == "folder":
            shared_folder_id = str(sharing.get("shared_folder_id") or "")
            parent_id = str(sharing.get("parent_shared_folder_id") or "")
            if shared_folder_id:
                rows = member_rows(client.folder_members(shared_folder_id))
            elif parent_id:
                rows = member_rows(client.folder_members(parent_id))
                for row in rows:
                    if row["via"] != "owner":
                        row["via"] = "inherited"
            else:
                rows = []
        else:
            shared_folder_id = ""
            rows = member_rows(client.file_members(item_id))
        if not any(r["via"] == "owner" for r in rows):
            # An item nobody shares lists no members; its owner is the
            # account itself, which is the one fact Dropbox leaves unsaid.
            rows.insert(0, {"permission_id": "owner", "who": client.email or "you",
                            "role": "owner", "via": "owner", "link": ""})
        rows.extend(link_row(link) for link in client.links(item_id))
        return rows, shared_folder_id

    async def permissions(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            item = client.metadata(str(call.inputs["item_id"]))
            rows, _ = self._access(client, item)
        except DropboxError as exc:
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
        item_id = str(inputs["item_id"])
        role = str(inputs.get("role") or "read")
        message = str(inputs.get("message") or "")
        granted, problem = [], None
        try:
            item = client.metadata(item_id)
            if tag(item) == "folder":
                shared_folder_id = self._shared_folder(client, item)
                client.add_folder_members(shared_folder_id, recipients, role, message)
                granted = list(recipients)
            else:
                # One call for everyone; Dropbox answers per member, so a
                # refusal for one does not undo the others.
                for answer in client.add_file_members(item_id, recipients, role, message):
                    email = str((answer.get("member") or {}).get("email") or "")
                    if tag(answer.get("result")) == "success":
                        granted.append(email)
                    elif problem is None:
                        problem = DropboxError("http", f"Dropbox did not share with "
                                                       f"{email}: {tag(answer.get('result'))}.")
        except DropboxError as exc:
            problem = exc
        share_ref = ""
        if granted:
            record = await call.resources.create_data("share", {
                "item_id": str(item.get("id") or item_id), "name": str(item.get("name") or ""),
                "kind": "invite", "who": ", ".join(granted), "role": role,
                "permission_id": ",".join("member:" + g for g in granted),
                "status": "granted"})
            share_ref = record["resource_ref"]
        if problem is not None:
            done = f" Already shared with: {', '.join(granted)}." if granted else ""
            return {"error": problem.message + done, "kind": problem.kind}, "error"
        return {"share_ref": share_ref, "invited": granted, "role": role}, "success"

    @staticmethod
    def _shared_folder(client, folder):
        """The folder's shared folder id, making it a shared folder first
        when it is not one yet. Dropbox may finish that in the background;
        the job is asked after a few times, never re-sent."""
        existing = str((folder.get("sharing_info") or {}).get("shared_folder_id") or "")
        if existing:
            return existing
        launched = client.share_folder(str(folder.get("path_display") or ""))
        job = ""
        for _ in range(SHARE_JOB_POLLS):
            if launched.get("shared_folder_id"):
                return str(launched["shared_folder_id"])
            if tag(launched) == "async_job_id":
                job = str(launched.get("async_job_id") or "")
            elif tag(launched) != "in_progress" or not job:
                raise DropboxError("http", f"Dropbox could not share the folder: "
                                           f"{tag(launched) or launched}.")
            time.sleep(1)
            launched = client.share_job(job)
        raise DropboxError("unknown", "Dropbox has not finished sharing the folder; "
                                      "check sharing.permissions in a minute before "
                                      "inviting again.")

    async def link(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "organization")
        access = "editor" if link_type == "edit" else "viewer"
        try:
            item = client.metadata(item_id)
            if scope == "organization":
                team = client.account().get("team") or {}
                if not team:
                    return {"error": "A personal Dropbox account has no team to share "
                                     "with; ask whether anyone with the link is "
                                     "meant."}, "error"
                audience = "team"
                reach = f"people in {team.get('name')}" if team.get("name") else \
                    "people in your team"
            else:
                audience, reach = "public", "anyone with the link"
            path = str(item.get("path_display") or item_id)
            try:
                made = client.create_link(path, audience, access)
            except DropboxError as exc:
                if "shared_link_already_exists" not in exc.summary:
                    raise
                # The item already has a link: Dropbox refused to make a
                # second, so nothing was written. That link is changed to
                # what was asked instead.
                made = client.modify_link(self._existing_link(client, exc, item_id),
                                          audience, access)
        except DropboxError as exc:
            return failure(exc)
        url = str(made.get("url") or "")
        record = await call.resources.create_data("share", {
            "item_id": str(item.get("id") or item_id), "name": str(item.get("name") or ""),
            "kind": "link", "who": reach, "role": "write" if link_type == "edit" else "read",
            "link": url, "permission_id": f"link:{url}", "status": "granted"})
        return {"share_ref": record["resource_ref"], "link": url, "reach": reach}, "success"

    @staticmethod
    def _existing_link(client, exc, item_id):
        error = (exc.body or {}).get("error") or {}
        metadata = ((error.get("shared_link_already_exists") or {}).get("metadata") or {})
        if metadata.get("url"):
            return str(metadata["url"])
        links = client.links(item_id)
        if not links:
            raise exc
        return str(links[0].get("url") or "")

    async def revoke(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        item_id = str(call.inputs["item_id"])
        permission_id = str(call.inputs["permission_id"])
        try:
            item = client.metadata(item_id)
            rows, shared_folder_id = self._access(client, item)
        except DropboxError as exc:
            return failure(exc)
        row = next((r for r in rows if r["permission_id"] == permission_id), None)
        if row is None:
            return {"error": "That item has no such permission; sharing.permissions "
                             "lists the ones it has."}, "error"
        if row["via"] == "owner":
            return {"error": "The owner's access cannot be removed."}, "error"
        if row["via"] == "inherited":
            return {"error": "This access comes from a shared parent folder; stop "
                             "sharing the folder instead."}, "error"
        kind, _, value = permission_id.partition(":")
        try:
            if kind == "link":
                client.revoke_link(value)
            else:
                member = {".tag": "email", "email": value} if kind == "member" else \
                    {".tag": "dropbox_id", "dropbox_id": value}
                if tag(item) == "folder":
                    client.remove_folder_member(shared_folder_id, member)
                else:
                    client.remove_file_member(item_id, member)
        except DropboxError as exc:
            return failure(exc)
        record = await call.resources.create_data("share", {
            "item_id": str(item.get("id") or item_id), "name": str(item.get("name") or ""),
            "kind": "link" if kind == "link" else "invite",
            "who": row["who"], "role": "write" if row["role"] == "write" else "read",
            "link": row["link"], "permission_id": permission_id, "status": "revoked"})
        return {"revoked": True, "share_ref": record["resource_ref"]}, "success"
