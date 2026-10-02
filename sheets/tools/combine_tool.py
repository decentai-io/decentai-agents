import re
from decimal import Decimal, ROUND_HALF_UP

from decentai_sdk.base import ToolBase

from .tables import Cell, _LEADING_ZERO, load
from .workbook import build, deliver

CENT = Decimal("0.01")
_NOT_IN_A_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")
#: Past this many digits a number is an identifier — a phone, a card, an
#: order — and Excel would show it as 9.71E+11.
LONGEST_NUMBER = 11


class CombineTool(ToolBase):
    id = "combine"

    async def files(self, call):
        inputs = call.inputs
        total_column = str(inputs.get("total_column") or "").strip()

        parts, summary, log, taken = [], [], [], set()
        for entry in inputs["files"]:
            ref = str(entry["file_ref"])
            try:
                book = await load(call, "source", ref)
                sheet = book.sheet()
                problem = book.problem
            except Exception as exc:
                book, sheet, problem = None, None, f"could not be read: {exc}"
            filename = book.filename if book is not None else ref
            name = self._sheet_name(str(entry.get("sheet_name") or "") or filename, taken)
            if sheet is None or not sheet.columns:
                summary.append({"source": name, "filename": filename, "rows": 0, "total": None,
                                "not_a_number": 0, "problem": problem or "the file is empty"})
                log.append(("", "left out", f"{filename}: {problem or 'the file is empty'}"))
                continue
            parts.append((name, filename, sheet))

        if not parts:
            return {"error": "None of the files could be read.", "files": summary}, "error"

        # The columns of the first file are what the others are held to:
        # a branch that renamed or dropped one is said, not smoothed over.
        expected = [c.strip().lower() for c in parts[0][2].columns]
        union = []
        for _, _, sheet in parts:
            for column in sheet.columns:
                if column.strip().lower() not in [u.strip().lower() for u in union]:
                    union.append(column)

        sheets, all_rows, grand = [], [], Decimal("0")
        for name, filename, sheet in parts:
            rows = [[self._typed(c) for c in row] for row in sheet.rows]
            sheets.append((name, list(sheet.columns), rows))
            problems = self._column_problems(sheet, expected, parts[0][2].columns)

            total, skipped = None, 0
            if total_column:
                try:
                    index = sheet.column(total_column)
                    total = Decimal("0")
                    for row in sheet.rows:
                        cell = row[index] if index < len(row) else Cell(None)
                        number = cell.number()
                        if number is None:
                            skipped += 0 if cell.blank else 1
                        else:
                            total += number
                    grand += total
                    if skipped:
                        problems.append(f"{skipped} value(s) in '{total_column}' are not numbers")
                except KeyError:
                    problems.append(f"no column '{total_column}'")

            for row in rows:
                by_name = {c.strip().lower(): row[i] if i < len(row) else Cell(None)
                           for i, c in enumerate(sheet.columns)}
                all_rows.append([Cell(name)] + [by_name.get(u.strip().lower(), Cell(None)) for u in union])
            summary.append({"source": name, "filename": filename, "rows": len(rows),
                            "total": self._money(total), "not_a_number": skipped,
                            "problem": "; ".join(problems)})
            log.append(("", "combined", f"{filename} → sheet '{name}', {len(rows)} row(s)"))

        total_label = f"Total {total_column}" if total_column else "Total"
        summary_rows = [[Cell(s["source"]), Cell(s["filename"]), Cell(s["rows"]), Cell(s["total"]),
                         Cell(s["not_a_number"]), Cell(s["problem"] or None)] for s in summary]
        summary_rows.append([Cell("All files"), Cell(None), Cell(len(all_rows)),
                             Cell(self._money(grand) if total_column else None),
                             Cell(sum(s["not_a_number"] for s in summary)), Cell(None)])
        log.insert(0, ("", "rules", "one sheet per file, as it arrived; sheet All holds every row "
                                    "with its source; totals are added in code"))
        raw = build([("Summary", ["Source", "File", "Rows", total_label, "Not a number", "Problem"],
                      summary_rows),
                     ("All", ["Source"] + union, all_rows)] + sheets, log)
        try:
            file_ref, verified, check = await deliver(call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        job = await call.resources.create_data("job", {
            "kind": "combine", "source_a": str(inputs["files"][0]["file_ref"]), "output_ref": file_ref,
            "summary": f"{len(parts)} of {len(inputs['files'])} file(s) combined, {len(all_rows)} row(s)"})
        return {"job_ref": job["resource_ref"], "file_ref": file_ref, "filename": check["filename"],
                "combined": len(parts), "left_out": len(inputs["files"]) - len(parts),
                "rows": len(all_rows), "sheets": check["sheets"],
                "grand_total": self._money(grand) if total_column else None,
                "files": summary, "verified": verified}, "success"

    @staticmethod
    def _sheet_name(wanted: str, taken: set) -> str:
        """What Excel accepts: 31 characters, none of []:*?/\\, no two alike."""
        stem = wanted.rsplit(".", 1)[0] if wanted.lower().endswith((".csv", ".xlsx")) else wanted
        base = _NOT_IN_A_SHEET_NAME.sub(" ", stem).strip()[:31] or "Sheet"
        name, n = base, 2
        while name.lower() in taken or name.lower() in ("summary", "all", "change log"):
            suffix = f" ({n})"
            name, n = base[:31 - len(suffix)] + suffix, n + 1
        taken.add(name.lower())
        return name

    @staticmethod
    def _column_problems(sheet, expected, first_columns):
        have = [c.strip().lower() for c in sheet.columns]
        missing = [c for c in first_columns if c.strip().lower() not in have]
        extra = [c for c in sheet.columns if c.strip().lower() not in expected]
        problems = []
        if missing:
            problems.append("missing column(s): " + ", ".join(missing))
        if extra:
            problems.append("extra column(s): " + ", ".join(extra))
        return problems

    @staticmethod
    def _typed(cell: Cell) -> Cell:
        """A CSV holds text only. A number goes into the workbook as a
        number, so Excel can add it up; an identifier stays text."""
        if cell.formula or not isinstance(cell.value, str):
            return cell
        text = cell.value.strip()
        digits = re.sub(r"\D", "", text)
        if _LEADING_ZERO.match(text) or len(digits) > LONGEST_NUMBER:
            return cell
        number = cell.number()
        if number is None:
            return cell
        return Cell(int(number) if number == number.to_integral_value() else float(number))

    @staticmethod
    def _money(value):
        return None if value is None else float(value.quantize(CENT, rounding=ROUND_HALF_UP))
