"""The note tool — data resources, the schedulable read,
and the one function that thinks with the chat's model.

Stateless: every note flows through ``call.resources``, scoped by the
platform to what each function declared. The two dependencies the
manifest names are used right here, which is the point of naming them.
"""

import humanize
from decentai_sdk.base import ToolBase
from titlecase import titlecase


class NoteTool(ToolBase):
    id = "note"

    async def save(self, call):
        notebook = str(call.inputs["notebook"]).lower()
        note_ref = call.inputs.get("note_ref")

        title = str(call.inputs["title"])
        # A dependency, used: a title typed in all lowercase is cased for
        # reading; one the person capitalised themselves is left alone.
        if title == title.lower():
            title = titlecase(title)
        fields = {"notebook": notebook, "title": title}
        if call.inputs.get("priority") is not None:
            fields["priority"] = int(call.inputs["priority"])
        if "content" in call.inputs:
            fields["content"] = call.inputs["content"]

        await call.progress(f"Saving note in '{notebook}'")

        if note_ref is None:
            record = await call.resources.create_data("note", fields)
            return {"note_ref": record["resource_ref"], "created": True}, "success"

        try:
            record = await call.resources.update_data("note", note_ref, fields)
        except Exception:
            return {"error": f"Unknown note_ref '{note_ref}'"}, "error"
        return {"note_ref": record["resource_ref"], "created": False}, "success"

    async def get(self, call):
        note_ref = str(call.inputs["note_ref"])
        try:
            record = await call.resources.read_data("note", note_ref)
        except Exception:
            record = {}
        if not record.get("resource_ref"):
            return {"error": f"No note with reference '{note_ref}'."}, "error"
        keys = record.get("keys") or {}
        values = record.get("values") or {}
        note = {
            "note_ref": record["resource_ref"],
            "title": str(keys.get("title") or ""),
        }
        # Optional fields travel only when they exist: the manifest types
        # them, and a null where a string is promised fails the output
        # check — correctly.
        for name in ("notebook", "priority"):
            if keys.get(name) is not None:
                note[name] = keys[name]
        content = values.get("content", keys.get("content"))
        if isinstance(content, dict):
            note["content"] = content
        return note, "success"

    async def find(self, call):
        """Schedulable: no model, no approval, and a result whose "notes"
        field a schedule can wake the assistant on."""
        notebook = str(call.inputs.get("notebook") or "").lower()
        query = str(call.inputs.get("query") or "").lower()
        limit = int(call.inputs.get("limit") or 25)

        filters = {"notebook": notebook} if notebook else {}
        records = await call.resources.list_data("note", filters)

        matches = [
            {
                "note_ref": record["resource_ref"],
                "title": str(record["keys"].get("title") or ""),
                "notebook": record["keys"].get("notebook"),
                "priority": record["keys"].get("priority"),
            }
            for record in records
            if not query or query in str(record["keys"].get("title") or "").lower()
        ]
        await call.progress(
            f"Found {humanize.intcomma(len(matches))} note(s)"
            + (f" in '{notebook}'" if notebook else ""))
        return {"notes": matches[:limit], "total": len(matches)}, "success"

    async def summarize(self, call):
        """The chat's model, through the platform: ``call.llm`` is the
        only way agent code ever thinks, and the key behind it never
        reaches this process."""
        notebook = str(call.inputs["notebook"]).lower()
        focus = str(call.inputs.get("focus") or "").strip()
        max_words = int(call.inputs.get("max_words") or 120)

        records = await call.resources.list_data("note", {"notebook": notebook})
        if not records:
            return {"summary": "", "note_count": 0}, "success"

        lines = []
        for record in records:
            keys = record.get("keys") or {}
            values = record.get("values") or {}
            content = values.get("content", keys.get("content"))
            line = f"- {keys.get('title') or 'Untitled'}"
            if keys.get("priority") is not None:
                line += f" (priority {keys['priority']})"
            if content:
                line += f": {content}"
            lines.append(line)

        await call.progress(
            f"Summarizing {humanize.intcomma(len(records))} note(s) in '{notebook}'")
        system = (
            "You summarize a person's notes faithfully. Never invent notes, "
            "facts or intentions; when the notes are thin, say so. Plain "
            "prose, no preamble."
        )
        prompt = (
            f"Summarize these notes in at most {max_words} words"
            + (f", focusing on {focus}" if focus else "")
            + ":\n\n" + "\n".join(lines)
        )
        summary = await call.llm(prompt, system=system, max_tokens=max_words * 4)
        return {"summary": str(summary).strip(), "note_count": len(records)}, "success"
