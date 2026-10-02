"""Comparing versions is arithmetic: paragraphs of A against paragraphs
of B, by sequence matching, each change carrying its page in each
file. Words the user cares about ("price", "delivery") put their
passages first."""

import difflib

from decentai_sdk.base import ToolBase

from .reading import load, paragraphs_of


class CompareTool(ToolBase):
    id = "compare"

    async def versions(self, call):
        try:
            a = await load(call, "source", str(call.inputs["file_ref_a"]))
            b = await load(call, "source", str(call.inputs["file_ref_b"]))
        except Exception as exc:
            return {"error": f"A file could not be read: {exc}"}, "error"
        for loaded, label in ((a, "first"), (b, "second")):
            if loaded.problem and not loaded.pages:
                return {"error": f"The {label} file: {loaded.problem}"}, "error"

        pa, pb = paragraphs_of(a), paragraphs_of(b)
        ta, tb = [p for _, p in pa], [p for _, p in pb]
        focus = [w.lower() for w in call.inputs.get("focus") or [] if str(w).strip()]
        changes = []
        matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                for k in range(max(i2 - i1, j2 - j1)):
                    before = pa[i1 + k] if i1 + k < i2 else None
                    after = pb[j1 + k] if j1 + k < j2 else None
                    changes.append(self._change(before, after))
            elif tag == "delete":
                changes.extend(self._change(pa[i], None) for i in range(i1, i2))
            elif tag == "insert":
                changes.extend(self._change(None, pb[j]) for j in range(j1, j2))
        for change in changes:
            text = (change["before"] + " " + change["after"]).lower()
            change["focus"] = any(word in text for word in focus)
        changes.sort(key=lambda c: (not c["focus"], c["page_a"] or c["page_b"]))
        limit = int(call.inputs.get("max_changes") or 60)
        counts = {kind: sum(1 for c in changes if c["kind"] == kind)
                  for kind in ("added", "removed", "changed")}
        return {
            "identical": not changes,
            "added": counts["added"], "removed": counts["removed"],
            "changed": counts["changed"],
            "truncated": len(changes) > limit,
            "focus_hits": sum(1 for c in changes if c["focus"]),
            "unreadable_a": a.unreadable, "unreadable_b": b.unreadable,
            "changes": changes[:limit],
        }, "success"

    @staticmethod
    def _change(before, after):
        if before is None:
            return {"kind": "added", "page_a": 0, "page_b": after[0],
                    "before": "", "after": after[1]}
        if after is None:
            return {"kind": "removed", "page_a": before[0], "page_b": 0,
                    "before": before[1], "after": ""}
        return {"kind": "changed", "page_a": before[0], "page_b": after[0],
                "before": before[1], "after": after[1]}
