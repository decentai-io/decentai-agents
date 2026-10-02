"""Proposals out of notes. The model reads; the code checks — every
quote against the notes verbatim, every owner against the names the
notes actually contain, every date through the deterministic parser.
What fails a check is not dropped silently: the quote is marked
unverified, the owner is cleared, the date stays as written."""

import json

from decentai_sdk.base import ToolBase

from .dates import parse_iso, resolve

PROPOSE_SYSTEM = (
    "You read meeting notes and list the action items in them. Answer ONLY "
    "with a JSON object {\"items\": [...]}. Each item: {\"title\": <short "
    "imperative phrase>, \"kind\": \"commitment\" when a named person said "
    "they will do it, else \"suggestion\", \"owner\": <the person's name "
    "exactly as written, or \"\" when the notes name nobody>, \"due\": <the "
    "deadline exactly as written, or \"\">, \"quote\": <the exact passage, "
    "copied verbatim, that states it>}. Never invent an owner or a date; "
    "never merge two actions into one."
)


class ExtractTool(ToolBase):
    id = "extract"

    async def propose(self, call):
        notes = str(call.inputs["notes"])
        source = str(call.inputs.get("source") or "")
        anchor = parse_iso(str(call.inputs.get("meeting_date") or ""))
        people = [str(p).strip() for p in call.inputs.get("people") or [] if str(p).strip()]
        prompt = (f"MEETING DATE: {anchor.isoformat() if anchor else 'unknown'}\n"
                  + (f"PEOPLE PRESENT: {', '.join(people)}\n" if people else "")
                  + f"\nNOTES:\n{notes}\n\nList the action items as JSON.")
        await call.progress("Reading the notes for action items")
        try:
            answer = await call.llm(prompt, system=PROPOSE_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = self._json(answer)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return {"error": "The model's answer was not a JSON object with "
                             "an items list; try again."}, "error"

        flat = " ".join(notes.split()).lower()
        proposals, commitments, suggestions, unassigned = [], 0, 0, 0
        for item in items[:50]:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            title = str(item["title"]).strip()[:200]
            kind = "commitment" if str(item.get("kind") or "").lower() == "commitment" else "suggestion"
            quote = " ".join(str(item.get("quote") or "").split())
            verified = len(quote) >= 8 and quote.lower() in flat
            owner = str(item.get("owner") or "").strip()
            if owner and not self._named(owner, notes, people):
                owner = ""                      # a name the notes never wrote
            due_text = str(item.get("due") or "").strip()[:80]
            due_date, _ = resolve(due_text, anchor)
            due = due_date.isoformat() if due_date else ""
            if kind == "commitment":
                commitments += 1
            else:
                suggestions += 1
            if not owner:
                unassigned += 1
            record = await call.resources.create_data("extraction", {
                "title": title, "kind": kind, "owner": owner, "due": due,
                "due_text": due_text, "quote": quote[:300], "source": source,
                "status": "proposed",
            })
            proposals.append({"extraction_ref": record["resource_ref"],
                              "title": title, "kind": kind, "owner": owner,
                              "due": due, "due_text": due_text,
                              "quote": quote[:300], "verified": verified})
        return {"proposals": proposals, "commitments": commitments,
                "suggestions": suggestions, "unassigned": unassigned}, "success"

    async def confirm(self, call):
        tasks, unassigned, skipped = [], [], []
        for item in call.inputs["items"]:
            ref = str(item["extraction_ref"])
            try:
                record = await call.resources.read_data("extraction", ref)
            except Exception:
                skipped.append({"extraction_ref": ref, "reason": "no such proposal"})
                continue
            keys = record.get("keys") or {}
            if keys.get("status") != "proposed":
                skipped.append({"extraction_ref": ref,
                                "reason": f"already {keys.get('status')}"})
                continue
            owner = str(item.get("owner") or keys.get("owner") or "").strip()
            due = str(item.get("due") or keys.get("due") or "").strip()
            if due and (parse_iso(due) is None or parse_iso(due).isoformat() != due):
                skipped.append({"extraction_ref": ref,
                                "reason": f"due must be YYYY-MM-DD, not '{due}'"})
                continue
            task = await call.resources.create_data("task", {
                "title": str(keys.get("title") or ""), "owner": owner, "due": due,
                "status": "open", "source": str(keys.get("source") or ""),
                "note": f"From notes: \"{keys.get('quote') or ''}\""[:500],
            })
            await call.resources.update_data("extraction", ref, {
                "status": "confirmed", "task_ref": task["resource_ref"],
                "owner": owner, "due": due})
            tasks.append({"task_ref": task["resource_ref"], "extraction_ref": ref,
                          "title": str(keys.get("title") or ""), "owner": owner,
                          "due": due})
            if not owner:
                unassigned.append(task["resource_ref"])
        return {"tasks": tasks, "unassigned": unassigned, "skipped": skipped}, "success"

    async def reject(self, call):
        ref = str(call.inputs["extraction_ref"])
        record = await call.resources.read_data("extraction", ref)
        if (record.get("keys") or {}).get("status") == "confirmed":
            return {"error": "That proposal was already confirmed into a task; "
                             "cancel the task instead."}, "error"
        await call.resources.update_data("extraction", ref, {"status": "rejected"})
        return {"rejected": True}, "success"

    @staticmethod
    def _named(owner: str, notes: str, people) -> bool:
        low = owner.lower()
        if any(low == p.lower() or low in p.lower() for p in people):
            return True
        return low in notes.lower()

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
