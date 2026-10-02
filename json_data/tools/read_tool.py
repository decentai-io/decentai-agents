from decentai_sdk.base import ToolBase

from .jsondoc import key_map, load, parse_path, preview, type_of
from .jsondoc import select as select_path

#: How many matches a path walk collects before it stops counting. A
#: caller asking "how many orders" gets a real number up to here; past
#: it the answer is "more than this", which is honest.
WALK_LIMIT = 1000


class ReadTool(ToolBase):
    id = "read"

    async def inspect(self, call):
        try:
            doc = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"

        result = {
            "file_ref": doc.file_ref, "filename": doc.filename,
            "kind": doc.kind, "valid": doc.valid, "top_level": doc.top_level,
            "records": len(doc.records), "bytes": doc.size, "keys": [],
        }
        if not doc.valid:
            result["problem"] = doc.problem
            if doc.line:
                result["error_line"] = doc.line
            if doc.column:
                result["error_column"] = doc.column
            return result, "success"

        rows, truncated, scanned, depth = key_map(
            doc.records, int(call.inputs.get("max_records") or 5000))
        result.update({"keys": rows, "keys_truncated": truncated,
                       "scanned": scanned, "depth": depth})
        return result, "success"

    async def select(self, call):
        try:
            doc = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if not doc.valid:
            return {"error": doc.problem}, "error"

        path = str(call.inputs["path"])
        try:
            steps = parse_path(path)
        except ValueError as exc:
            return {"error": str(exc)}, "error"

        wanted = int(call.inputs.get("max_results") or 50)
        room = int(call.inputs.get("max_value_chars") or 2000)
        found = select_path(doc.data, steps, limit=WALK_LIMIT)

        results = []
        for where, value in found[:wanted]:
            entry = {"path": where, "type": type_of(value)}
            text = preview(value, room)
            if text.endswith("…"):
                entry.update({"preview": text, "truncated": True})
            else:
                entry.update({"value": value, "truncated": False})
            results.append(entry)

        return {"file_ref": doc.file_ref, "path": path,
                "matches": len(found), "truncated": len(found) > wanted,
                "results": results}, "success"
