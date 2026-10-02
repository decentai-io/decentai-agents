from decentai_sdk.base import ToolBase

from .tables import load, row_dict


class ReadTool(ToolBase):
    id = "read"

    async def inspect(self, call):
        try:
            book = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        result = {"file_ref": book.file_ref, "filename": book.filename,
                  "kind": book.kind,
                  "sheets": [{"name": s.name, "rows": len(s.rows)} for s in book.sheets],
                  "columns": [{"sheet": s.name, **column}
                              for s in book.sheets for column in s.profile()]}
        if book.problem:
            result["problem"] = book.problem
        return result, "success"

    async def rows(self, call):
        try:
            book = await load(call, "source", str(call.inputs["file_ref"]))
            sheet = book.sheet(str(call.inputs.get("sheet") or ""))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if book.problem and not book.sheets:
            return {"error": book.problem}, "error"
        wanted = [str(c) for c in call.inputs.get("columns") or []]
        unknown = [c for c in wanted if c.strip().lower() not in
                   {x.strip().lower() for x in sheet.columns}]
        if unknown:
            return {"error": f"No such column(s): {', '.join(unknown)}. Columns: "
                             f"{', '.join(sheet.columns)}"}, "error"
        start = int(call.inputs.get("from_row") or 1) - 1
        limit = int(call.inputs.get("max_rows") or 100)
        formulas = bool(call.inputs.get("formulas"))
        chosen = sheet.rows[start: start + limit]
        # The same trimmed, case-insensitive match the unknown-column
        # guard above used to let names through by.
        asked = {c.strip().lower() for c in wanted}
        result = {"sheet": sheet.name,
                  "columns": [c for c in sheet.columns
                              if not asked or c.strip().lower() in asked],
                  "rows": [row_dict(sheet, r, wanted or None, formulas) for r in chosen],
                  "total_rows": len(sheet.rows),
                  "truncated": start + limit < len(sheet.rows)}
        if result["truncated"]:
            result["next_row"] = start + limit + 1
        # An empty page of a sheet that HAS rows is a question about
        # from_row, not an empty sheet. Which it is should not have to be
        # guessed from a zero.
        if not chosen and sheet.rows:
            result["note"] = (f"No rows at from_row {start + 1}: this sheet "
                              f"has {len(sheet.rows)}.")
        return result, "success"
