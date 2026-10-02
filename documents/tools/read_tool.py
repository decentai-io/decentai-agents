import json
import re

from decentai_sdk.base import ToolBase

from .reading import load

EXTRACT_SYSTEM = (
    "You read one business document and answer ONLY with a JSON object. "
    "For every field asked, return {\"found\": true, \"value\": <the value as "
    "written>, \"quote\": <the exact passage, copied verbatim, that states "
    "it>, \"page\": <page number from the [page N] markers>} or "
    "{\"found\": false}. Never guess, infer, calculate or fill in a field the "
    "document does not state. The quote must be copied exactly."
)


class ReadTool(ToolBase):
    id = "read"

    async def inspect(self, call):
        try:
            loaded = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        result = {
            "file_ref": loaded.file_ref, "filename": loaded.filename,
            "kind": loaded.kind, "pages": len(loaded.pages),
            "readable_pages": loaded.readable,
            "unreadable_pages": loaded.unreadable,
            "characters": loaded.characters,
            "headings": loaded.headings[:50],
        }
        if loaded.problem:
            result["problem"] = loaded.problem
        return result, "success"

    async def text(self, call):
        try:
            loaded = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if loaded.problem and not loaded.pages:
            return {"error": loaded.problem}, "error"
        start = int(call.inputs.get("from_page") or 1)
        budget = int(call.inputs.get("max_chars") or 20000)
        pages, used, truncated, next_page = [], 0, False, 0
        for number in range(start, len(loaded.pages) + 1):
            text = loaded.pages[number - 1]
            if used + len(text) > budget and pages:
                truncated, next_page = True, number
                break
            if used + len(text) > budget:
                text = text[: budget - used]
                truncated, next_page = True, number + 1
            pages.append({"page": number, "text": text})
            used += len(text)
            if truncated:
                break
        result = {"file_ref": loaded.file_ref, "filename": loaded.filename,
                  "pages": pages, "truncated": truncated,
                  "unreadable_pages": loaded.unreadable}
        if truncated and next_page <= len(loaded.pages):
            result["next_page"] = next_page
        return result, "success"

    async def extract(self, call):
        try:
            loaded = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if not loaded.readable:
            return {"error": loaded.problem or "No page of this document has "
                             "readable text; nothing can be extracted."}, "error"
        wanted = [{"name": str(f["name"]), "hint": str(f.get("hint") or "")}
                  for f in call.inputs["fields"]]
        budget = int(call.inputs.get("max_chars") or 30000)
        text = loaded.text()
        truncated = len(text) > budget
        text = text[:budget]

        asked = "\n".join(f"- {f['name']}" + (f": {f['hint']}" if f["hint"] else "")
                          for f in wanted)
        prompt = (f"DOCUMENT ({loaded.filename}):\n{text}\n\nFIELDS:\n{asked}\n\n"
                  f"Answer with one JSON object keyed by field name.")
        await call.progress(f"Reading {loaded.filename} for {len(wanted)} field(s)")
        try:
            answer = await call.llm(prompt, system=EXTRACT_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = self._json(answer)
        if not isinstance(parsed, dict):
            return {"error": "The model's answer was not a JSON object; "
                             "try again with fewer fields."}, "error"

        fields, found, missing = [], 0, 0
        for want in wanted:
            item = parsed.get(want["name"])
            entry = {"name": want["name"], "found": False}
            if isinstance(item, dict) and item.get("found") is True and item.get("value") is not None:
                quote = str(item.get("quote") or "").strip()
                page, verified = self._locate(loaded, quote)
                entry.update({"found": True, "value": str(item.get("value")),
                              "quote": quote, "verified": verified})
                if page:
                    entry["page"] = page
                elif isinstance(item.get("page"), int):
                    entry["page"] = int(item["page"])
                found += 1
            else:
                missing += 1
            fields.append(entry)

        record = await call.resources.create_data("extract", {
            "file_ref": loaded.file_ref, "filename": loaded.filename,
            "fields": ", ".join(f["name"] for f in wanted)[:500],
            "found": found, "missing": missing, "result": {"fields": fields},
        })
        return {"extract_ref": record["resource_ref"], "file_ref": loaded.file_ref,
                "fields": fields, "found": found, "missing": missing,
                "truncated": truncated}, "success"

    @staticmethod
    def _json(answer: str):
        text = str(answer or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None

    @staticmethod
    def _locate(loaded, quote: str):
        """The page a quote appears on verbatim (spacing normalised), or
        0 and unverified when it is nowhere in the document."""
        needle = " ".join(quote.split()).lower()
        if len(needle) < 4:
            return 0, False
        for number, page in enumerate(loaded.pages, 1):
            if needle in " ".join(page.split()).lower():
                return number, True
        return 0, False
