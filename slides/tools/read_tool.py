from decentai_sdk.base import ToolBase

from .reading import load


class ReadTool(ToolBase):
    id = "read"

    async def inspect(self, call):
        try:
            deck = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        result = {
            "file_ref": deck.file_ref, "filename": deck.filename,
            "kind": deck.kind, "slides": len(deck.slides),
            "titles": [{"slide": s.number, "title": s.title}
                       for s in deck.slides],
            "empty_slides": deck.empty_slides,
            "characters": deck.characters,
            "has_notes": deck.has_notes,
            "layouts": deck.layouts[:20],
        }
        if deck.problem:
            result["problem"] = deck.problem
        return result, "success"

    async def text(self, call):
        try:
            deck = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if deck.problem and not deck.slides:
            return {"error": deck.problem}, "error"

        start = int(call.inputs.get("from_slide") or 1)
        budget = int(call.inputs.get("max_chars") or 20000)
        with_notes = call.inputs.get("with_notes")
        with_notes = True if with_notes is None else bool(with_notes)

        slides, used, truncated, next_slide = [], 0, False, 0
        for slide in deck.slides:
            if slide.number < start:
                continue
            cost = slide.characters(with_notes)
            if used + cost > budget and slides:
                truncated, next_slide = True, slide.number
                break
            entry = {"slide": slide.number, "title": slide.title,
                     "text": slide.text}
            if with_notes and slide.notes:
                entry["notes"] = slide.notes
            slides.append(entry)
            used += cost

        result = {"file_ref": deck.file_ref, "filename": deck.filename,
                  "slides": slides, "truncated": truncated,
                  "empty_slides": deck.empty_slides}
        if truncated and next_slide <= len(deck.slides):
            result["next_slide"] = next_slide
        return result, "success"
