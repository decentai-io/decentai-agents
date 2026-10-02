from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP

from decentai_sdk.base import ToolBase

from .tables import load

CENT = Decimal("0.01")


class CalcTool(ToolBase):
    id = "calc"

    async def aggregate(self, call):
        inputs = call.inputs
        try:
            book = await load(call, "source", str(inputs["file_ref"]))
            sheet = book.sheet(str(inputs.get("sheet") or ""))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        group_by = [str(c) for c in inputs.get("group_by") or []]
        measures = [(str(m["column"]), str(m["op"])) for m in inputs["measures"]]
        try:
            for name in group_by + [c for c, _ in measures]:
                sheet.column(name)
        except KeyError as exc:
            return {"error": str(exc)}, "error"

        groups = OrderedDict()
        skipped = 0
        for row in sheet.rows:
            key = tuple(sheet.cell(row, c).text for c in group_by)
            bucket = groups.setdefault(key, {(c, op): [] for c, op in measures})
            for column, op in measures:
                cell = sheet.cell(row, column)
                if op == "count":
                    if not cell.blank:
                        bucket[(column, op)].append(Decimal(1))
                    continue
                number = cell.number()
                if number is None:
                    if not cell.blank:
                        skipped += 1
                    continue
                bucket[(column, op)].append(number)

        rows = []
        for key, bucket in groups.items():
            out = {name: value for name, value in zip(group_by, key)}
            for (column, op), values in bucket.items():
                label = f"{op}({column})"
                if op == "count":
                    out[label] = len(values)
                elif not values:
                    out[label] = None
                elif op == "sum":
                    out[label] = float(sum(values).quantize(CENT, rounding=ROUND_HALF_UP))
                elif op == "avg":
                    out[label] = float((sum(values) / len(values)).quantize(CENT, rounding=ROUND_HALF_UP))
                elif op == "min":
                    out[label] = float(min(values))
                elif op == "max":
                    out[label] = float(max(values))
            rows.append(out)
        job = await call.resources.create_data("job", {
            "kind": "aggregate", "source_a": book.file_ref,
            "summary": (f"{len(rows)} group(s) by {', '.join(group_by) or 'nothing'}; "
                        f"{', '.join(f'{op}({c})' for c, op in measures)}; "
                        f"{skipped} non-numeric cell(s) skipped")[:500]})
        return {"job_ref": job["resource_ref"], "groups": rows,
                "rows_read": len(sheet.rows), "skipped_cells": skipped}, "success"
