"""Rows watches: how form responses and sign-ups arrive.

A ROWS watch remembers a tab and how many rows it had — the cursor. new()
reads only the rows after the cursor, hands them on keyed by the header
row, and moves the cursor past them; its ``rows`` list is what a schedule
wakes the assistant on, so a check that finds nothing costs no model
call.

A row count sees rows added at the end. It does not see a cell edited in
the middle, and it cannot tell rows deleted from rows sorted: when a tab
has FEWER rows than the cursor, new() hands on nothing for that watch,
moves the cursor to the rows there are now, and says so in ``reset``.
Guessing which rows are new after that would be inventing.

The tab is remembered by its sheet id as well as its title, so a renamed
tab is still the tab being watched.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .sheets_api import GoogleError, a1, clip, column_letter, find_tab, no_tab, tab_row


def header_names(cells, width):
    """Column names from the header row: blanks become the column letter,
    and a repeated name gets its column letter beside it, so no answer
    overwrites another."""
    names, seen = [], set()
    for index in range(width):
        letter = column_letter(index + 1)
        name = str(cells[index]).strip() if index < len(cells) else ""
        name = name or letter
        if name in seen:
            name = f"{name} ({letter})"
        seen.add(name)
        names.append(name)
    return names


def blank(row) -> bool:
    return not any(str(c).strip() for c in row)


class RowsTool(ToolBase):
    id = "rows"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        spreadsheet_id = str(inputs["spreadsheet_id"])
        header_row = int(inputs.get("header_row", 1))
        try:
            meta = client.spreadsheet(spreadsheet_id)
            tab = find_tab(meta, str(inputs["tab"]))
            if tab is None:
                return no_tab(meta, str(inputs["tab"]))
            # The rows there are now; everything after them is news.
            present = len(client.values(spreadsheet_id, self._whole(tab)))
            header = []
            if header_row:
                found = client.values(spreadsheet_id, self._row(tab, header_row))
                header = [clip(c) for c in (found[0] if found else [])]
        except GoogleError as exc:
            return failure(exc)
        record = await call.resources.create_data("watch", {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet": str((meta.get("properties") or {}).get("title") or ""),
            "tab": tab["tab"], "sheet_id": tab["sheet_id"],
            "header_row": header_row, "cursor": present, "status": "watching",
            "note": str(inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "tab": tab["tab"],
                "header": header, "cursor": present}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
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
            try:
                found, gone, has_more = await self._check(call, client, ref, keys, most)
            except GoogleError as exc:
                return failure(exc)
            rows.extend(found)
            if gone:
                reset.append(gone)
            more = more or has_more
        return {"checked": checked, "rows": rows, "reset": reset, "more": more}, "success"

    # -- one watch -------------------------------------------------------
    async def _check(self, call, client, ref, keys, most):
        """(rows handed on, a reset entry or None, whether more wait)."""
        spreadsheet_id = str(keys.get("spreadsheet_id") or "")
        cursor = int(float(keys.get("cursor") or 0))
        header_row = int(float(keys.get("header_row") or 0))
        entry = {"watch_ref": ref, "spreadsheet": str(keys.get("spreadsheet") or ""),
                 "tab": str(keys.get("tab") or "")}
        try:
            meta = client.spreadsheet(spreadsheet_id)
        except GoogleError as exc:
            if exc.kind != "not_found":
                raise
            await call.resources.update_data("watch", ref, {"status": "closed"})
            return [], {**entry, "cursor": cursor,
                        "message": "The spreadsheet is gone, or this account can no "
                                   "longer open it; the watch was closed."}, False
        tab = self._same_tab(meta, keys)
        if tab is None:
            await call.resources.update_data("watch", ref, {"status": "closed"})
            return [], {**entry, "cursor": cursor,
                        "message": f"The tab '{entry['tab']}' is no longer in the "
                                   f"spreadsheet; the watch was closed."}, False
        entry["tab"] = tab["tab"]
        if tab["tab"] != keys.get("tab"):
            await call.resources.update_data("watch", ref, {"tab": tab["tab"]})

        first_new = max(cursor, header_row) + 1
        # The window starts at the last row already seen, so one read also
        # says whether that row is still there; it ends one row past what
        # may be handed on, so "more" is known without reading further.
        start = cursor if cursor >= 1 else first_new
        end = min(tab["grid_rows"], first_new + most)
        has_header = 1 <= header_row <= tab["grid_rows"]
        has_window = cursor <= tab["grid_rows"] and start <= end
        ranges = []
        if has_header:
            ranges.append(self._row(tab, header_row))
        if has_window:
            ranges.append(a1(tab["tab"], f"A{start}:{column_letter(tab['grid_columns'])}{end}"))
        # The header is read at every check, not remembered: a form that
        # gains a question gains a column, and its answers need its name.
        answers = client.batch_values(spreadsheet_id, ranges) if ranges else []
        header = (answers[0][0] if answers and answers[0] else []) if has_header else []
        window = answers[-1] if has_window and answers else []

        if cursor >= 1 and (cursor > tab["grid_rows"] or not window or blank(window[0])):
            # The last row seen is empty now: rows went, or it was cleared.
            # Only a full count can tell which.
            present = len(client.values(spreadsheet_id, self._whole(tab)))
            if present < cursor:
                await call.resources.update_data("watch", ref, {"cursor": present})
                return [], {**entry, "cursor": present,
                            "message": f"'{tab['tab']}' now has {present} rows, fewer "
                                       f"than the {cursor} it had, so rows were deleted. "
                                       f"Nothing was handed on; the watch "
                                       f"continues from row {present + 1}."}, False

        names = header_names(header, len(header))
        found, last = [], cursor
        for number in range(first_new, first_new + most):
            index = number - start
            if index >= len(window):
                break
            last = number
            cells = window[index]
            if blank(cells):
                continue
            shaped = {(names[i] if i < len(names) else column_letter(i + 1)): clip(c)
                      for i, c in enumerate(cells) if str(c).strip()}
            found.append({**entry, "row": number, "cells": shaped})
        has_more = (first_new + most - start) < len(window)
        if last > cursor:
            await call.resources.update_data("watch", ref, {"cursor": last})
        return found, None, has_more

    # -- ranges ----------------------------------------------------------
    @staticmethod
    def _same_tab(meta, keys):
        """The watched tab by its sheet id — a renamed tab is still it —
        or by title when the id is not recorded."""
        sheet_id = keys.get("sheet_id")
        if sheet_id not in (None, ""):
            for sheet in meta.get("sheets") or []:
                row = tab_row(sheet)
                if row["sheet_id"] == int(float(sheet_id)):
                    return row
            return None
        return find_tab(meta, str(keys.get("tab") or ""))

    @staticmethod
    def _whole(tab):
        return a1(tab["tab"], f"A1:{column_letter(tab['grid_columns'])}{tab['grid_rows']}")

    @staticmethod
    def _row(tab, number):
        return a1(tab["tab"], f"A{number}:{column_letter(tab['grid_columns'])}{number}")
