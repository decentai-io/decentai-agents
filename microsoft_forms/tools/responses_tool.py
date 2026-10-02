"""Responses: listed newest first, and watched as they arrive.

A response watch remembers the highest response ID it has handed on,
and how many rows the table held then. Forms numbers responses in
order and appends each as a row, so new() reads the rows from about
where the count left off and hands on those whose ID is higher, oldest
first; its ``responses`` list is what a schedule wakes the assistant
on, so a quiet check costs no model call.

The ID is the cursor, not the row count, because a person tidying the
workbook deletes rows: after that the count points past rows nobody
has seen. So the read starts LOOKBACK rows before the remembered
count, and the ID decides what is new — a response is handed on once,
however the rows around it moved.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import GraphError
from .responses import response_row, response_table

#: Rows read before the remembered count, for rows deleted since.
LOOKBACK = 25


def _ids(region, lines, start):
    rows = [response_row(region, line, start + i) for i, line in enumerate(lines)]
    return [r for r in rows if r is not None]


class ResponsesTool(ToolBase):
    id = "responses"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        most = int(call.inputs.get("max_results") or 10)
        try:
            region, problem = response_table(client, item_id, str(call.inputs.get("table") or ""))
            if problem:
                return problem
            start = max(0, region.rows - most)
            lines = client.read_rows(item_id, region, start, region.rows)
        except GraphError as exc:
            return failure(exc)
        rows = sorted(_ids(region, lines, start), key=lambda r: r["response_id"], reverse=True)
        return {"table": region.name, "total": region.rows, "responses": rows}, "success"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        try:
            item = client.item(item_id)
            region, problem = response_table(client, item_id, str(call.inputs.get("table") or ""))
            if problem:
                return problem
            start = max(0, region.rows - LOOKBACK)
            lines = client.read_rows(item_id, region, start, region.rows)
        except GraphError as exc:
            return failure(exc)
        last_id = max([r["response_id"] for r in _ids(region, lines, start)] or [0])
        workbook = str(item.get("name") or "")
        record = await call.resources.create_data("watch", {
            "workbook_id": item_id, "workbook": workbook, "table": region.name,
            "last_id": last_id, "row_count": region.rows, "status": "watching",
            "note": str(call.inputs.get("note") or ""),
        })
        return {"watch_ref": record["resource_ref"], "workbook": workbook,
                "table": region.name, "last_id": last_id,
                "responses": region.rows}, "success"

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
        handed, checked, more = [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            item_id = str(keys.get("workbook_id") or "")
            last_id = int(keys.get("last_id") or 0)
            seen_rows = int(keys.get("row_count") or 0)
            try:
                region, problem = response_table(client, item_id, str(keys.get("table") or ""))
                if problem:
                    return problem
                base = min(seen_rows, region.rows)
                start = max(0, base - LOOKBACK)
                # One more than asked for past the count, so "more" is
                # known without handing it on.
                stop = min(region.rows, base + most + 1)
                lines = client.read_rows(item_id, region, start, stop)
            except GraphError as exc:
                return failure(exc)
            fresh = sorted((r for r in _ids(region, lines, start) if r["response_id"] > last_id),
                           key=lambda r: r["response_id"])
            if len(fresh) > most:
                fresh = fresh[:most]
                more = True
                row_count = max(r["row_number"] for r in fresh) - region.header_row
            else:
                more = more or stop < region.rows
                row_count = stop
            for row in fresh:
                handed.append({"watch_ref": ref, "workbook": str(keys.get("workbook") or ""),
                               "table": region.name, **row})
            if fresh:
                last_id = fresh[-1]["response_id"]
            if (last_id, row_count) != (int(keys.get("last_id") or 0), seen_rows):
                await call.resources.update_data("watch", ref, {
                    "last_id": last_id, "row_count": row_count})
        return {"checked": checked, "responses": handed, "more": more}, "success"
