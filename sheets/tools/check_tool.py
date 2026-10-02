from collections import defaultdict

from decentai_sdk.base import ToolBase

from .tables import key_of, load, normalize


class CheckTool(ToolBase):
    id = "check"

    async def quality(self, call):
        try:
            book = await load(call, "source", str(call.inputs["file_ref"]))
            sheet = book.sheet(str(call.inputs.get("sheet") or ""))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        keys = [str(c) for c in call.inputs.get("key_columns") or []]
        required = [str(c) for c in call.inputs.get("required_columns") or []]
        categorical = [str(c) for c in call.inputs.get("categorical_columns") or []]
        try:
            for name in keys + required + categorical:
                sheet.column(name)
        except KeyError as exc:
            return {"error": str(exc)}, "error"
        limit = int(call.inputs.get("max_findings") or 100)

        groups = defaultdict(list)
        if keys:
            phone_like = {c for c in keys if "phone" in c.lower() or "mobile" in c.lower()}
            for index, row in enumerate(sheet.rows):
                key = tuple(normalize(sheet.cell(row, c).text,
                                      "digits" if c in phone_like else "lowercase")
                            for c in keys)
                if any(key):
                    groups[key].append(sheet.row_number(index))
        duplicate_groups = [{"key": " | ".join(k), "rows": rows}
                            for k, rows in groups.items() if len(rows) > 1]

        missing = []
        for name in required:
            rows = [sheet.row_number(i) for i, row in enumerate(sheet.rows)
                    if sheet.cell(row, name).blank]
            if rows:
                missing.append({"column": name, "rows": rows[:limit]})

        inconsistent = []
        for name in categorical:
            variants = defaultdict(set)
            for row in sheet.rows:
                text = sheet.cell(row, name).text
                if text.strip():
                    variants[normalize(text, "lowercase")].add(text)
            for seen in variants.values():
                if len(seen) > 1:
                    inconsistent.append({"column": name, "variants": sorted(seen)})

        truncated = len(duplicate_groups) > limit
        summary = (f"{len(sheet.rows)} rows; {len(duplicate_groups)} duplicate group(s) "
                   f"by {', '.join(keys) or 'no key'}; "
                   f"{sum(len(m['rows']) for m in missing)} blank required cell(s); "
                   f"{len(inconsistent)} inconsistent value set(s)")
        job = await call.resources.create_data("job", {
            "kind": "quality", "source_a": book.file_ref, "summary": summary[:500]})
        return {"job_ref": job["resource_ref"], "rows": len(sheet.rows),
                "duplicate_groups": duplicate_groups[:limit], "missing": missing,
                "inconsistent": inconsistent[:limit], "truncated": truncated}, "success"
