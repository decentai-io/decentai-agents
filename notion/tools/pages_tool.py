"""Reading a page, and creating or changing one.

A page is read as its properties and its body, one line per block, a
window at a time: ``from`` and ``max_blocks`` choose the window and
``total`` says how long the page is. Children of a toggle or a list item
are fetched one level down, because that is where a checklist or a
folded section keeps its content; anything deeper is named, not walked,
so one page cannot turn into hundreds of requests.

Every write goes through the schema first (see schema.py) and leaves a
``write`` record: what was sent, and for an update what was there
before, so a person can see and undo what the agent changed.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .notion_api import NotionError
from .schema import DatabaseSchema, PageRow, SchemaProblem
from .text import BlockText, PropertyText, Text

#: How many top-level blocks are read to count a page. Past it the
#: total is a floor, and the result says so.
BLOCK_CAP = 1000
#: How many children of one block are fetched.
CHILDREN_CAP = 100


def paragraph(text: str):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": Text.write(text)}}


class PagesTool(ToolBase):
    id = "pages"

    # -- reading ---------------------------------------------------------
    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_id = str(call.inputs["page_id"]).strip()
        start = int(call.inputs.get("from") or 0)
        most = int(call.inputs.get("max_blocks") or 30)
        try:
            page = client.page(page_id)
            blocks, capped = client.all_children(page_id, BLOCK_CAP)
            lines = BlockText.lines(blocks)
            rows = []
            for index in range(start, min(start + most, len(blocks))):
                block = blocks[index]
                rows.append({"index": index, "depth": 0, "block_id": str(block.get("id") or ""),
                             "type": str(block.get("type") or ""), "text": lines[index]})
                if block.get("has_children") and block.get("type") not in BlockText.OWN_PAGES:
                    rows.extend(self._nested(client, block, index))
        except NotionError as exc:
            return failure(exc)
        result = {**PageRow.row(page), "parent": PageRow.parent(page),
                  "blocks": rows, "from": start, "total": len(blocks),
                  "total_capped": capped, "more": start + most < len(blocks)}
        if result["more"]:
            result["next_from"] = start + most
        return result, "success"

    @staticmethod
    def _nested(client, block, index):
        """One level of a block's children; anything deeper is named."""
        answer = client.children(str(block.get("id") or ""), CHILDREN_CAP)
        children = answer.get("results") or []
        rows = []
        for child, text in zip(children, BlockText.lines(children)):
            if child.get("has_children") and child.get("type") not in BlockText.OWN_PAGES:
                text += " (has nested content, not shown)"
            rows.append({"index": index, "depth": 1, "block_id": str(child.get("id") or ""),
                         "type": str(child.get("type") or ""), "text": text})
        if answer.get("has_more"):
            rows.append({"index": index, "depth": 1, "block_id": "", "type": "more",
                         "text": f"(more than {CHILDREN_CAP} nested blocks; the rest not shown)"})
        return rows

    # -- writing ---------------------------------------------------------
    @staticmethod
    def _with_title(schema, values, title):
        """The title input folded into the property values, once."""
        values = dict(values or {})
        if not title:
            return values
        name = schema.title_property()
        if any(str(k).lower() == name.lower() for k in values):
            raise SchemaProblem(f"The title was given twice: as title and as the "
                                f"'{name}' property. Give it once.")
        values[name] = title
        return values

    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        database_id = str(inputs.get("database_id") or "").strip()
        parent_page_id = str(inputs.get("parent_page_id") or "").strip()
        title = str(inputs.get("title") or "").strip()
        if bool(database_id) == bool(parent_page_id):
            return invalid("Give exactly one of database_id (a new row) or "
                           "parent_page_id (a new page under that page).")
        try:
            if database_id:
                schema = DatabaseSchema(client.database(database_id))
                try:
                    payload, shown = schema.build(
                        self._with_title(schema, inputs.get("properties"), title))
                except SchemaProblem as problem:
                    return invalid(str(problem))
                parent = {"database_id": database_id}
                title = title or next((shown[n] for n in shown
                                       if n == schema.title_property()), "")
            else:
                if inputs.get("properties"):
                    return invalid("A page under a page has only a title; properties "
                                   "belong to rows of a database.")
                if not title:
                    return invalid("A new page needs a title.")
                payload = {"title": {"title": Text.write(title)}}
                shown = {"title": title}
                parent = {"page_id": parent_page_id}
            body = {"parent": parent, "properties": payload}
            paragraphs = [str(p) for p in inputs.get("paragraphs") or []]
            if paragraphs:
                body["children"] = [paragraph(p) for p in paragraphs]
            created = client.create_page(body)
        except NotionError as exc:
            return failure(exc)
        row = PageRow.row(created, with_properties=False)
        record = await call.resources.create_data("write", {
            "action": "page_created", "page_id": row["page_id"],
            "title": row["title"] or title, "parent_id": database_id or parent_page_id,
            "link": row["url"], "changed": ", ".join(shown),
            "detail": {"sent": shown, "paragraphs": len(paragraphs)}})
        return {"page_id": row["page_id"], "url": row["url"], "title": row["title"] or title,
                "parent": PageRow.parent(created) or ("database " + database_id if database_id
                                                      else "page " + parent_page_id),
                "write_ref": record["resource_ref"]}, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        page_id = str(call.inputs["page_id"]).strip()
        title = str(call.inputs.get("title") or "").strip()
        values = dict(call.inputs.get("properties") or {})
        if not values and not title:
            return invalid("Nothing to change: give properties or a title.")
        try:
            page = client.page(page_id)
            if page.get("archived") or page.get("in_trash"):
                return invalid("That page is in the Notion trash; restore it in Notion first.")
            parent = page.get("parent") or {}
            if parent.get("type") == "database_id":
                schema = DatabaseSchema(client.database(str(parent.get("database_id"))))
                try:
                    payload, shown = schema.build(self._with_title(schema, values, title))
                except SchemaProblem as problem:
                    return invalid(str(problem))
            else:
                if values:
                    return invalid("This page is not a database row; only its title "
                                   "can be changed.")
                payload, shown = {"title": {"title": Text.write(title)}}, {"title": title}
            before = page.get("properties") or {}
            previous = {name: PropertyText.render(before[name]) if name in before else ""
                        for name in payload}
            updated = client.update_page(page_id, payload)
        except NotionError as exc:
            return failure(exc)
        after = updated.get("properties") or {}
        now = {name: PropertyText.render(after[name]) if name in after else shown.get(name, "")
               for name in payload}
        row = PageRow.row(updated, with_properties=False)
        record = await call.resources.create_data("write", {
            "action": "page_updated", "page_id": page_id, "title": row["title"],
            "parent_id": PageRow.parent_id(page), "link": row["url"],
            "changed": ", ".join(payload),
            "detail": {"previous": previous, "now": now}})
        return {"page_id": page_id, "url": row["url"], "title": row["title"],
                "changed": list(payload), "previous": previous, "now": now,
                "write_ref": record["resource_ref"]}, "success"
