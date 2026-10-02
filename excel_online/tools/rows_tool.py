"""Row watches: the thing a schedule can ask a workbook.

A watch remembers how many data rows a table (or a worksheet's used
range, read as a table) held when it was last checked. new() reads the
rows past that count, hands them on keyed by header, and moves the
count; its ``rows`` list is what a schedule wakes the assistant on, so
a check that finds nothing costs no model call.

A count is only a cursor while rows are added at the end, which is how
a table grows. When the count has SHRUNK — rows deleted, or a sort or
filter pasted over — the rows past the old count are not new, and the
rows that are new cannot be told apart from the ones that moved. So
nothing is reported for that watch, the count starts again from what
is there, and the ``reset`` list says so in a sentence.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import GraphError, blank, keyed


class RowsTool(ToolBase):
    id = "rows"

    @staticmethod
    def _region(client, item_id, table, worksheet):
        return (client.table_region(item_id, table) if table
                else client.sheet_region(item_id, worksheet))

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        table = str(call.inputs.get("table") or "").strip()
        worksheet = str(call.inputs.get("worksheet") or "").strip()
        if not table and not worksheet:
            return {"error": "Name the table to watch (preferred) or the worksheet.",
                    "kind": "invalid"}, "error"
        try:
            item = client.item(item_id)
            region = self._region(client, item_id, table, worksheet)
        except GraphError as exc:
            return failure(exc)
        workbook = str(item.get("name") or "")
        record = await call.resources.create_data("watch", {
            "workbook_id": item_id, "workbook": workbook,
            "table": region.name if table else "", "worksheet": region.worksheet,
            "cursor": region.rows, "status": "watching",
            "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "workbook": workbook,
                "table": region.name if table else "", "worksheet": region.worksheet,
                "rows": region.rows, "headers": region.headers}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, reset, checked, more = [], [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            item_id = str(keys.get("workbook_id") or "")
            table = str(keys.get("table") or "")
            workbook = str(keys.get("workbook") or "")
            cursor = int(keys.get("cursor") or 0)
            try:
                region = self._region(client, item_id, table, str(keys.get("worksheet") or ""))
                if region.rows < cursor:
                    await call.resources.update_data("watch", ref, {"cursor": region.rows})
                    what = f"table {region.name}" if table else f"worksheet {region.worksheet}"
                    reset.append({
                        "watch_ref": ref, "workbook": workbook, "table": table,
                        "worksheet": region.worksheet, "rows": region.rows,
                        "note": f"The {what} in {workbook} now has {region.rows} rows, "
                                f"fewer than the {cursor} it had at the last check: rows "
                                f"were deleted or moved, so nothing is reported for it "
                                f"this time and the watch counts again from here."})
                    continue
                # The row count is read before the rows, so "more" is
                # exact: whatever lies past this check's slice is next
                # time's, and the cursor moves only through what was read.
                stop = min(region.rows, cursor + most)
                more = more or region.rows > stop
                lines = client.read_rows(item_id, region, cursor, stop)
            except GraphError as exc:
                return failure(exc)
            for i, line in enumerate(lines):
                # A blank row in the middle is counted, not reported.
                if blank(line):
                    continue
                rows.append({"watch_ref": ref, "workbook": workbook, "table": table,
                             "worksheet": region.worksheet,
                             "row_number": region.row_number(cursor + i),
                             "values": keyed(region.headers, line)})
            if stop > cursor:
                await call.resources.update_data("watch", ref, {"cursor": stop})
        return {"checked": checked, "rows": rows, "more": more, "reset": reset}, "success"
