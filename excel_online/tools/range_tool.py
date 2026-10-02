"""Reading a block of cells, and overwriting one.

A read is capped: at most max_rows rows and MAX_COLUMNS columns come
back, each cell's text clipped, and the result says how much was cut so
the assistant can ask for a narrower address rather than believe it saw
everything.

An overwrite is level 3 in the manifest, so by the time it runs here
the user has agreed. Before a cell changes, what was there is read (as
formulas, which is what would put it back) and kept in the write
record; the write itself goes through a workbook session so it lands
as one change.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import (MAX_COLUMNS, GraphError, box_address, clip,
                             header_names, parse_box, split_address)


def _box(call):
    """The address the user gave, as a box — or the refusal to return."""
    local = split_address(call.inputs.get("address"))[1]
    box = parse_box(local)
    if box is None:
        return None, ({"error": f"'{call.inputs.get('address')}' is not a block of "
                                f"cells; give an A1 address with row numbers, like "
                                f"A1:D40 or B7.", "kind": "invalid"}, "error")
    return box, None


class RangeTool(ToolBase):
    id = "range"

    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        sheet = str(call.inputs["worksheet"])
        most = int(call.inputs.get("max_rows") or 20)
        with_header = bool(call.inputs.get("header"))
        try:
            if str(call.inputs.get("address") or "").strip():
                box, problem = _box(call)
                if problem:
                    return problem
            else:
                used = client.used_range(item_id, sheet)
                box = parse_box(split_address(used.get("address"))[1])
                if box is None:
                    return {"error": f"Worksheet {sheet} has no block of cells to "
                                     f"read.", "kind": "invalid"}, "error"
            r1, c1, r2, c2 = box
            shown_c2 = min(c2, c1 + MAX_COLUMNS - 1)
            header, first = [], r1
            if with_header:
                header = header_names((client.cells(
                    item_id, sheet, box_address(r1, c1, r1, shown_c2)) or [[]])[0])
                first = r1 + 1
            total = max(0, r2 - first + 1)
            shown = min(total, most)
            rows = client.cells(item_id, sheet, box_address(
                first, c1, first + shown - 1, shown_c2)) if shown else []
        except GraphError as exc:
            return failure(exc)
        result = {"worksheet": sheet, "address": box_address(r1, c1, r2, c2),
                  "rows": [[clip(cell) for cell in line] for line in rows],
                  "row_count": total, "rows_cut": total - shown,
                  "columns_cut": c2 - shown_c2}
        if with_header:
            result["header"] = header
        return result, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        sheet = str(call.inputs["worksheet"])
        values = call.inputs["values"]
        box, problem = _box(call)
        if problem:
            return problem
        r1, c1, r2, c2 = box
        address = box_address(r1, c1, r2, c2)
        height, width = r2 - r1 + 1, c2 - c1 + 1
        if len(values) != height or any(len(line) != width for line in values):
            return {"error": f"{address} is {height} row(s) by {width} column(s); "
                             f"values must be exactly that shape. Nothing was "
                             f"written.", "kind": "invalid"}, "error"
        try:
            item = client.item(item_id)
            previous = client.cells(item_id, sheet, address, field="formulas")
            session = client.create_session(item_id)
        except GraphError as exc:
            return failure(exc)
        keys = {"kind": "update_range", "workbook_id": item_id,
                "workbook": str(item.get("name") or ""),
                "target": f"{sheet}!{address}", "rows": height}
        kept = {"written": {"rows": values}, "previous": {"rows": previous}}
        try:
            client.set_range(item_id, sheet, address, values, session)
        except GraphError as exc:
            if exc.kind == "unknown":
                # It may have landed. The record says so, with what was
                # there before, so nobody has to guess what to restore.
                record = await call.resources.create_data(
                    "write", {**keys, "status": "unknown", **kept})
                return {"error": exc.message, "kind": "unknown",
                        "write_ref": record["resource_ref"]}, "error"
            return failure(exc)
        finally:
            client.close_session(item_id, session)
        record = await call.resources.create_data(
            "write", {**keys, "status": "written", **kept})
        return {"write_ref": record["resource_ref"], "worksheet": sheet,
                "address": address, "cells": height * width,
                "previous": [[clip(cell) for cell in line] for line in previous]}, "success"
