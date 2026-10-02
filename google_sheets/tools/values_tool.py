"""Reading cells, and writing them on authorization.

A read hands the model rows as lists, the header apart, and never more
than it can use: long cells are clipped and a large range is cut off
with the number of rows there were and the row to continue from.

Both writes are level 3 — the sheet is shared with other people, and
what lands in it is theirs to see at once. Each keeps a ``write`` record
of what it wrote; an update also keeps what the cells held before, so
the person can see exactly what changed and put it back.
"""

import json
import re

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .sheets_api import (CELL_LIMIT, GoogleError, a1, clip, column_letter, column_number,
                         find_tab, no_tab)

#: The most cells one read shows the model; past it the read says where
#: to continue rather than handing over the whole tab.
MAX_CELLS = 200
#: A record's value half is capped at 8,192 characters of JSON by the
#: platform; what was written is kept up to a little under that.
RECORD_CHARS = 7000
CELLS = re.compile(r"^([A-Za-z]{0,3})(\d*)(?::([A-Za-z]{0,3})(\d*))?$")


def shown(rows, max_rows):
    """As many whole rows as fit under max_rows and MAX_CELLS — always at
    least one — each cell clipped. Returns (rows, clipped cell count)."""
    out, cells, clipped = [], 0, 0
    for row in rows:
        if len(out) >= max_rows or (out and cells + len(row) > MAX_CELLS):
            break
        row = row[:MAX_CELLS]
        clipped += sum(1 for c in row if len(str(c)) > CELL_LIMIT)
        out.append([clip(c) for c in row])
        cells += len(row)
    return out, clipped


def keepable(rows):
    """What was written, as a record can hold it: cells clipped, rows
    dropped from the end until the JSON fits."""
    kept = [[clip(c) for c in row] for row in rows]
    clipped = False
    while kept and len(json.dumps(kept)) > RECORD_CHARS:
        kept.pop()
        clipped = True
    return {"rows": kept, "clipped": clipped}


class ValuesTool(ToolBase):
    id = "values"

    @staticmethod
    def _tab(client, spreadsheet_id, tab):
        """(meta, the tab) — or (meta, None) when the spreadsheet has no
        such tab."""
        meta = client.spreadsheet(spreadsheet_id)
        return meta, find_tab(meta, tab)

    # -- reads -----------------------------------------------------------
    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        cells = str(inputs.get("cells") or "").replace("$", "").strip()
        match = CELLS.match(cells)
        if not match:
            return {"error": f"cells is a range on the tab like 'A1:F50' or 'B:D', "
                             f"not {cells!r}; the tab goes in tab.",
                    "kind": "invalid"}, "error"
        with_header = inputs.get("header", True) is not False
        max_rows = int(inputs.get("max_rows") or 50)
        try:
            meta, tab = self._tab(client, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            values = client.values(spreadsheet_id, a1(tab["tab"], cells))
        except GoogleError as exc:
            return failure(exc)
        first_row = int(match.group(2) or 1)
        header = []
        if with_header and values:
            header = [clip(c) for c in values[0]]
            values = values[1:]
            first_row += 1
        rows, clipped = shown(values, max_rows)
        result = {"tab": tab["tab"], "range": a1(tab["tab"], cells),
                  "header": header, "rows": rows, "first_row": first_row,
                  "total_rows": len(values), "shown_rows": len(rows),
                  "truncated": len(rows) < len(values), "clipped_cells": clipped}
        if result["truncated"]:
            result["next_row"] = first_row + len(rows)
        return result, "success"

    # -- writes ----------------------------------------------------------
    async def append(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        rows = [[str(c) for c in row] for row in inputs["rows"]]
        try:
            meta, tab = self._tab(client, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            answer = client.append(spreadsheet_id, tab["tab"], rows)
        except GoogleError as exc:
            return failure(exc)
        updates = answer.get("updates") or {}
        updated_range = str(updates.get("updatedRange") or "")
        record = await call.resources.create_data("write", {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet": str((meta.get("properties") or {}).get("title") or ""),
            "tab": tab["tab"], "kind": "append", "range": updated_range,
            "rows": int(updates.get("updatedRows") or len(rows)),
            "cells": int(updates.get("updatedCells") or 0),
            "link": str(meta.get("spreadsheetUrl") or ""),
            "written": keepable(rows)})
        return {"write_ref": record["resource_ref"], "updated_range": updated_range,
                "updated_rows": int(updates.get("updatedRows") or len(rows)),
                "updated_cells": int(updates.get("updatedCells") or 0)}, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        start = re.match(r"^([A-Za-z]{1,3})([1-9]\d*)$", str(inputs["start_cell"]).replace("$", ""))
        if not start:
            return {"error": "start_cell is one cell like 'B2'.", "kind": "invalid"}, "error"
        rows = [[str(c) for c in row] for row in inputs["rows"]]
        left, top = column_number(start.group(1)), int(start.group(2))
        right = left + max(len(r) for r in rows) - 1
        bottom = top + len(rows) - 1
        area = f"{column_letter(left)}{top}:{column_letter(right)}{bottom}"
        try:
            meta, tab = self._tab(client, spreadsheet_id, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            # Past the grid Google refuses the range; say so in words,
            # and point at the function that adds rows.
            if bottom > tab["grid_rows"] or right > tab["grid_columns"]:
                return {"error": f"{area} runs past the tab '{tab['tab']}', which has "
                                 f"{tab['grid_rows']} rows and {tab['grid_columns']} "
                                 f"columns. To add rows at the end, use values.append.",
                        "kind": "invalid"}, "error"
            target = a1(tab["tab"], area)
            # What the cells held, read before anything is written: the
            # record of an overwrite is only honest if it has this.
            before = client.values(spreadsheet_id, target)
            answer = client.update(spreadsheet_id, target, rows)
        except GoogleError as exc:
            return failure(exc)
        width = right - left + 1
        previous = [(list(row) + [""] * width)[:width] for row in before]
        previous += [[""] * width for _ in range(len(rows) - len(previous))]
        updated_range = str(answer.get("updatedRange") or target)
        record = await call.resources.create_data("write", {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet": str((meta.get("properties") or {}).get("title") or ""),
            "tab": tab["tab"], "kind": "update", "range": updated_range,
            "rows": len(rows), "cells": int(answer.get("updatedCells") or 0),
            "link": str(meta.get("spreadsheetUrl") or ""),
            "written": keepable(rows), "previous": keepable(previous)})
        shown_previous, _ = shown(previous, len(previous))
        return {"write_ref": record["resource_ref"], "updated_range": updated_range,
                "updated_cells": int(answer.get("updatedCells") or 0),
                "previous": shown_previous,
                "previous_truncated": len(shown_previous) < len(previous)}, "success"
