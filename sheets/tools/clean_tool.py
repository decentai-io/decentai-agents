from decentai_sdk.base import ToolBase

from .tables import key_of, load
from .workbook import build, deliver, text_rows


class CleanTool(ToolBase):
    id = "clean"

    async def dedupe(self, call):
        inputs = call.inputs
        try:
            book = await load(call, "source", str(inputs["file_ref"]))
            sheet = book.sheet(str(inputs.get("sheet") or ""))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        keys = [str(c) for c in inputs["key_columns"]]
        how = str(inputs.get("normalize") or "lowercase")
        try:
            for name in keys:
                sheet.column(name)
        except KeyError as exc:
            return {"error": str(exc)}, "error"

        seen = {}
        kept, removed, log = [], [], []
        for index, row in enumerate(sheet.rows):
            key = key_of(sheet, row, keys, how)
            if any(key) and key in seen:
                removed.append(index)
                log.append((sheet.row_number(index), "removed",
                            f"duplicate of row {sheet.row_number(seen[key])} on "
                            f"{', '.join(keys)}: {' | '.join(key)}"))
            else:
                seen.setdefault(key, index)
                kept.append(index)
        log.insert(0, ("", "rules", f"first row kept per {', '.join(keys)} ({how})"))

        cols_k, rows_k = text_rows(sheet, [sheet.rows[i] for i in kept])
        cols_r, rows_r = text_rows(sheet, [sheet.rows[i] for i in removed],
                                   {"Original row": [sheet.row_number(i) for i in removed]})
        raw = build([("Clean", cols_k, rows_k), ("Removed", cols_r, rows_r)], log)
        try:
            file_ref, verified, check = await deliver(call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        job = await call.resources.create_data("job", {
            "kind": "dedupe", "source_a": book.file_ref, "output_ref": file_ref,
            "summary": f"{len(kept)} kept, {len(removed)} removed by {', '.join(keys)}"})
        return {"job_ref": job["resource_ref"], "file_ref": file_ref,
                "filename": check["filename"], "kept": len(kept), "removed": len(removed),
                "removed_rows": [sheet.row_number(i) for i in removed][:500],
                "verified": verified}, "success"
