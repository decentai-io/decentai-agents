"""Two lists, matched by the rules the user gave and nothing else.

Exact on every key: matched. Exact on every key but one: uncertain —
shown side by side, never merged. Otherwise only in A or only in B.
Matched rows are diffed on the compare columns. Everything lands in a
workbook with a change log; the inputs are untouched."""

from collections import defaultdict

from decentai_sdk.base import ToolBase

from .tables import Cell, load, normalize
from .workbook import build, deliver, text_rows


class ReconcileTool(ToolBase):
    id = "reconcile"

    async def match(self, call):
        inputs = call.inputs
        try:
            book_a = await load(call, "source", str(inputs["file_ref_a"]))
            book_b = await load(call, "source", str(inputs["file_ref_b"]))
            sheet_a = book_a.sheet(str(inputs.get("sheet_a") or ""))
            sheet_b = book_b.sheet(str(inputs.get("sheet_b") or ""))
        except Exception as exc:
            return {"error": f"A file could not be read: {exc}"}, "error"
        keys = [(str(k["a"]), str(k["b"]), str(k.get("normalize") or "lowercase"))
                for k in inputs["keys"]]
        compare = [(str(c["a"]), str(c["b"])) for c in inputs.get("compare") or []]
        try:
            for a, b, _ in keys:
                sheet_a.column(a), sheet_b.column(b)
            for a, b in compare:
                sheet_a.column(a), sheet_b.column(b)
        except KeyError as exc:
            return {"error": str(exc)}, "error"

        def key(sheet, row, side):
            return tuple(normalize(sheet.cell(row, a if side == "a" else b).text, how)
                         for a, b, how in keys)

        index_b = defaultdict(list)
        for i, row in enumerate(sheet_b.rows):
            index_b[key(sheet_b, row, "b")].append(i)
        used_b = set()
        matched, uncertain, only_a = [], [], []
        for i, row in enumerate(sheet_a.rows):
            k = key(sheet_a, row, "a")
            candidates = [j for j in index_b.get(k, []) if j not in used_b]
            if candidates:
                j = candidates[0]
                used_b.add(j)
                matched.append((i, j))
                continue
            near = self._near(k, sheet_b, index_b, used_b, keys) if len(keys) > 1 else None
            if near is not None:
                uncertain.append((i, near[0], near[1]))
            else:
                only_a.append(i)
        only_b = [j for j in range(len(sheet_b.rows)) if j not in used_b
                  and j not in {u[1] for u in uncertain}]

        differences = []
        for i, j in matched:
            for a, b in compare:
                left, right = sheet_a.cell(sheet_a.rows[i], a).text, sheet_b.cell(sheet_b.rows[j], b).text
                if normalize(left, "lowercase") != normalize(right, "lowercase"):
                    differences.append((i, j, a, left, right))

        # The workbook.
        log = []
        cols_a, rows_a = text_rows(sheet_a, [sheet_a.rows[i] for i, _ in matched],
                                   {"Row in A": [sheet_a.row_number(i) for i, _ in matched],
                                    "Row in B": [sheet_b.row_number(j) for _, j in matched]})
        cols_oa, rows_oa = text_rows(sheet_a, [sheet_a.rows[i] for i in only_a],
                                     {"Row in A": [sheet_a.row_number(i) for i in only_a]})
        cols_ob, rows_ob = text_rows(sheet_b, [sheet_b.rows[j] for j in only_b],
                                     {"Row in B": [sheet_b.row_number(j) for j in only_b]})
        unc_rows = []
        unc_pairs = []
        for i, j, differ in uncertain:
            agree = ", ".join(a for a, _, _ in keys if a != differ)
            unc_pairs.append({"row_a": sheet_a.row_number(i), "row_b": sheet_b.row_number(j),
                              "agree": agree, "differ": differ})
            unc_rows.append([Cell(sheet_a.row_number(i)), Cell(sheet_b.row_number(j)),
                             Cell(agree), Cell(differ),
                             Cell(sheet_a.cell(sheet_a.rows[i], differ).text),
                             Cell(sheet_b.cell(sheet_b.rows[j], next(b for a, b, _ in keys if a == differ)).text)])
            log.append((sheet_a.row_number(i), "uncertain",
                        f"matches row {sheet_b.row_number(j)} of B on {agree} but not {differ}; not merged"))
        diff_rows = [[Cell(sheet_a.row_number(i)), Cell(sheet_b.row_number(j)), Cell(col), Cell(l), Cell(r)]
                     for i, j, col, l, r in differences]
        for i, j, col, l, r in differences:
            log.append((sheet_a.row_number(i), "differs", f"{col}: A '{l}' vs B '{r}' (row {sheet_b.row_number(j)})"))
        for i in only_a:
            log.append((sheet_a.row_number(i), "only in A", "no row in B matches on the keys"))
        for j in only_b:
            log.append((sheet_b.row_number(j), "only in B", "no row in A matches on the keys"))
        log.insert(0, ("", "rules", "keys: " + "; ".join(f"A.{a} = B.{b} ({how})" for a, b, how in keys)
                       + (" | compare: " + ", ".join(f"A.{a} vs B.{b}" for a, b in compare) if compare else "")))
        raw = build([
            ("Matched", cols_a, rows_a),
            ("Only in A", cols_oa, rows_oa),
            ("Only in B", cols_ob, rows_ob),
            ("Uncertain", ["Row in A", "Row in B", "Agree on", "Differ on", "A value", "B value"], unc_rows),
            ("Differences", ["Row in A", "Row in B", "Column", "A value", "B value"], diff_rows),
        ], log)
        try:
            file_ref, verified, _ = await deliver(call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        summary = (f"{len(matched)} matched, {len(only_a)} only in A, {len(only_b)} only in B, "
                   f"{len(uncertain)} uncertain, {len(differences)} differences")
        job = await call.resources.create_data("job", {
            "kind": "reconcile", "source_a": book_a.file_ref, "source_b": book_b.file_ref,
            "output_ref": file_ref, "summary": summary})
        filename = str(inputs["filename"])
        return {"job_ref": job["resource_ref"], "file_ref": file_ref,
                "filename": filename if filename.lower().endswith(".xlsx") else filename + ".xlsx",
                "matched": len(matched), "only_in_a": len(only_a), "only_in_b": len(only_b),
                "uncertain": len(uncertain), "differences": len(differences),
                "uncertain_pairs": unc_pairs[:100], "verified": verified}, "success"

    @staticmethod
    def _near(k, sheet_b, index_b, used_b, keys):
        """A B row equal on every key but one — (index, the key that
        differs) — or None. Only when exactly one such row exists: two
        candidates is a question for a person, not a match."""
        found = []
        for other, indexes in index_b.items():
            differ = [n for n, (x, y) in enumerate(zip(k, other)) if x != y]
            if len(differ) == 1 and any(k):
                for j in indexes:
                    if j not in used_b:
                        found.append((j, keys[differ[0]][0]))
        return found[0] if len(found) == 1 else None
