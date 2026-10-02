"""Turning JSON into a table and a table back into JSON.

Two rules make the round trip safe. A nested value becomes a dotted
column and comes back nested. An identifier stays text: a value with a
leading zero, or longer than fifteen digits, is a reference number, not
a quantity, and turning it into a number loses it.
"""

import base64
import json
import re

from decentai_sdk.base import ToolBase

from .jsondoc import (cell, flatten, load, parse_path, read_csv, read_text,
                      write_csv)
from .jsondoc import select as select_path

IDENTIFIER = re.compile(r"^0\d+$")


class ShapeTool(ToolBase):
    id = "shape"

    async def to_table(self, call):
        try:
            doc = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if not doc.valid:
            return {"error": doc.problem}, "error"

        path = str(call.inputs.get("record_path") or "")
        if path:
            try:
                steps = parse_path(path)
            except ValueError as exc:
                return {"error": str(exc)}, "error"
            found = select_path(doc.data, steps, limit=100000)
            if not found:
                return {"error": f"No record matched '{path}'."}, "error"
            if len(found) == 1 and isinstance(found[0][1], list):
                records = found[0][1]
            else:
                records = [value for _, value in found]
        else:
            records = doc.records

        cap = int(call.inputs.get("max_rows") or 10000)
        truncated = len(records) > cap
        records = records[:cap]

        wanted = [str(c) for c in call.inputs.get("columns") or []]
        columns, rows, skipped = [], [], 0
        flattened = []
        for record in records:
            if not isinstance(record, dict):
                if wanted:
                    skipped += 1
                    continue
                record = {"value": record}
            flat = flatten(record)
            flattened.append(flat)
            if not wanted:
                for name in flat:
                    if name not in columns:
                        columns.append(name)
        if wanted:
            columns = wanted
        if not columns:
            return {"error": "The records have no fields to make columns "
                             "from."}, "error"

        for flat in flattened:
            rows.append([cell(flat.get(name)) for name in columns])

        filename = self._named(str(call.inputs["filename"]), ".csv")
        raw = write_csv(columns, rows)
        file_ref, output_ref = await self._deliver(
            call, "csv", filename, raw, doc.filename, len(rows))
        return {"file_ref": file_ref, "filename": filename,
                "output_ref": output_ref, "rows": len(rows),
                "columns": columns, "truncated": truncated,
                "skipped": skipped}, "success"

    async def to_json(self, call):
        try:
            _, _, text = await read_text(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"

        cap = int(call.inputs.get("max_rows") or 10000)
        header, rows, truncated = read_csv(text, cap)
        if not header:
            return {"error": "The file has no header row, so there is "
                             "nothing to key the records by."}, "error"

        infer = call.inputs.get("infer_types")
        infer = True if infer is None else bool(infer)
        expand = call.inputs.get("expand_dots")
        expand = True if expand is None else bool(expand)

        records = []
        for row in rows:
            record = {}
            for index, name in enumerate(header):
                if not name:
                    continue
                raw = row[index] if index < len(row) else ""
                value = self._value(raw) if infer else raw
                if expand and "." in name:
                    self._nest(record, name.split("."), value)
                else:
                    record[name] = value
            records.append(record)

        filename = self._named(str(call.inputs["filename"]), ".json")
        body = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        file_ref, output_ref = await self._deliver(
            call, "json", filename, body, "", len(records))
        return {"file_ref": file_ref, "filename": filename,
                "output_ref": output_ref, "records": len(records),
                "columns": [h for h in header if h],
                "truncated": truncated}, "success"

    # ------------------------------------------------------------------
    @staticmethod
    def _named(filename: str, suffix: str) -> str:
        name = re.sub(r"[^\w.\- ]", "_", filename).strip() or "data"
        return name if name.lower().endswith(suffix) else name + suffix

    @staticmethod
    def _value(raw: str):
        text = str(raw).strip()
        if text == "":
            return None
        lowered = text.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        if lowered == "null":
            return None
        digits = text[1:] if text[:1] in "+-" else text
        if IDENTIFIER.match(digits) or (digits.isdigit() and len(digits) > 15):
            return text            # a reference number, not a quantity
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text

    @staticmethod
    def _nest(record: dict, parts, value) -> None:
        cursor = record
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value

    async def _deliver(self, call, kind, filename, raw: bytes, source, rows):
        saved = await call.resources.create_file(
            "result", filename,
            content_base64=base64.b64encode(raw).decode("ascii"))
        file_ref = saved["resource_ref"]
        record = await call.resources.create_data("output", {
            "kind": kind, "filename": filename, "file_ref": file_ref,
            "source": str(source)[:200], "rows": rows})
        return file_ref, record["resource_ref"]
