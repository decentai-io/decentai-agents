"""Comparing two versions of a document, by arithmetic over their
paths. Every scalar in each file is reduced to path and value; what
changed is then set difference and equality, with nothing judged."""

from decentai_sdk.base import ToolBase

from .jsondoc import leaves, load, preview


class CompareTool(ToolBase):
    id = "compare"

    async def versions(self, call):
        docs = []
        for key in ("file_ref_a", "file_ref_b"):
            try:
                doc = await load(call, "source", str(call.inputs[key]))
            except Exception as exc:
                return {"error": f"The file could not be read: {exc}"}, "error"
            if not doc.valid:
                return {"error": f"{doc.filename or key}: {doc.problem}"}, "error"
            docs.append(doc)

        before, after = leaves(docs[0].data), leaves(docs[1].data)
        cap = int(call.inputs.get("max_changes") or 100)

        changes, added, removed, changed = [], 0, 0, 0
        for path in sorted(set(before) | set(after)):
            in_a, in_b = path in before, path in after
            if in_a and in_b:
                if before[path] == after[path]:
                    continue
                changed += 1
                kind, pair = "changed", (before[path], after[path])
            elif in_b:
                added += 1
                kind, pair = "added", (None, after[path])
            else:
                removed += 1
                kind, pair = "removed", (before[path], None)
            if len(changes) < cap:
                entry = {"kind": kind, "path": path}
                if kind != "added":
                    entry["before"] = preview(pair[0], 200)
                if kind != "removed":
                    entry["after"] = preview(pair[1], 200)
                changes.append(entry)

        total = added + removed + changed
        return {"identical": total == 0, "added": added, "removed": removed,
                "changed": changed, "truncated": total > len(changes),
                "changes": changes}, "success"
