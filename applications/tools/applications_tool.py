"""Applications: started on a form, driven one step at a time, reviewed,
submitted. The records are the truth — an application, one answer
record per step answered, one attachment record per document — and
every accepted answer is written at once, so a conversation that folds,
a person who comes back tomorrow, or a page that opens the records cold
all see the same application."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from decentai_sdk.base import ToolBase

from .documents import inspect, read_fields
from .forms import (FormError, check_answer, is_required, load_forms,
                    public_step, run_checks)

SKIP_WORDS = ("skip", "none", "n/a", "-")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """One application as the functions see it: its record, its form,
    the answers so far (field -> value, None where skipped) and the
    documents so far (field -> what was kept)."""

    def __init__(self, ref: str, record: dict, form: dict,
                 answers: Dict[str, Any], answer_refs: Dict[str, str],
                 documents: Dict[str, dict], document_refs: Dict[str, str]):
        self.ref = ref
        self.keys = dict(record.get("keys") or {})
        self.form = form
        self.answers = answers
        self.answer_refs = answer_refs
        self.documents = documents
        self.document_refs = document_refs

    @property
    def status(self) -> str:
        return str(self.keys.get("status") or "draft")

    def pending(self) -> List[dict]:
        """Steps still to ask, in order. A step is done when answered
        (a skipped optional step counts) or when its document is in."""
        return [s for s in self.form["steps"]
                if not (s["field"] in self.documents or s["field"] in self.answers)]

    def next(self) -> Dict[str, Any]:
        pending = self.pending()
        if not pending:
            return {"done": True, "remaining": 0}
        return {"done": False, "remaining": len(pending),
                "step": public_step(pending[0], len(pending))}

    def progress(self) -> str:
        total = len(self.form["steps"])
        return f"{total - len(self.pending())} of {total}"

    def missing(self) -> List[str]:
        return [s["field"] for s in self.form["steps"] if is_required(s) and (
            (s["kind"] == "file" and s["field"] not in self.documents)
            or (s["kind"] != "file" and self.answers.get(s["field"]) in (None, "")))]

    def closed(self) -> Optional[dict]:
        if self.status != "draft":
            return {"error": f"This application is {self.status}; it cannot be changed.",
                    "kind": "closed"}
        return None


class ApplicationsTool(ToolBase):
    id = "applications"

    # -- reading the state --------------------------------------------------
    async def _load(self, call, ref: str) -> Tuple[Optional[State], Optional[dict]]:
        try:
            forms = load_forms()
        except FormError as exc:
            return None, {"error": f"A form this agent carries is broken: {exc}",
                          "kind": "form"}
        try:
            record = await call.resources.read_data("application", ref)
        except Exception:
            return None, {"error": f"No application '{ref}'.", "kind": "not_found"}
        form = forms.get(str((record.get("keys") or {}).get("form") or ""))
        if form is None:
            return None, {"error": "The application's form is no longer carried by "
                                   "this agent.", "kind": "form"}
        answers, answer_refs = {}, {}
        for row in await call.resources.list_data("answer", {"application": ref}):
            keys = row.get("keys") or {}
            field = str(keys.get("field") or "")
            answers[field] = None if keys.get("skipped") == "yes" else keys.get("value")
            answer_refs[field] = row["resource_ref"]
        documents, document_refs = {}, {}
        for row in await call.resources.list_data("attachment", {"application": ref}):
            keys = dict(row.get("keys") or {})
            field = str(keys.get("field") or "")
            try:
                keys["read"] = json.loads(keys.get("read") or "{}")
            except json.JSONDecodeError:
                keys["read"] = {}
            documents[field] = {k: v for k, v in keys.items() if k not in ("application",)}
            document_refs[field] = row["resource_ref"]
        return State(ref, record, form, answers, answer_refs, documents, document_refs), None

    # -- functions ----------------------------------------------------------
    async def list(self, call):
        wanted = str(call.inputs.get("status") or "")
        rows = await call.resources.list_data(
            "application", {"status": wanted} if wanted else None)
        return {"applications": [{
            "application_ref": r["resource_ref"],
            **{k: (r.get("keys") or {}).get(k, "")
               for k in ("form", "title", "status", "progress", "reference")},
        } for r in rows]}, "success"

    async def start(self, call):
        try:
            forms = load_forms()
        except FormError as exc:
            return {"error": f"A form this agent carries is broken: {exc}",
                    "kind": "form"}, "error"
        form = forms.get(str(call.inputs.get("form") or "").strip())
        if form is None:
            return {"error": f"No form '{call.inputs.get('form')}'. Forms: "
                             + ", ".join(forms) + ".", "kind": "not_found"}, "error"
        total = len(form["steps"])
        record = await call.resources.create_data("application", {
            "form": form["id"], "title": form["title"], "status": "draft",
            "progress": f"0 of {total}", "reference": "", "submitted_at": "",
        })
        state = State(record["resource_ref"], {"keys": {"status": "draft"}}, form,
                      {}, {}, {}, {})
        return {"application_ref": record["resource_ref"], "title": form["title"],
                "next": state.next()}, "success"

    async def next(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        return state.next(), "success"

    async def answer(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        step = next((s for s in state.form["steps"] if s["field"] == call.inputs["field"]), None)
        if step is None:
            return {"error": f"The form has no step '{call.inputs['field']}'.",
                    "kind": "not_found"}, "error"
        return await self._record_answer(call, state, step, call.inputs.get("value")), "success"

    async def attach(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        step = next((s for s in state.form["steps"] if s["field"] == call.inputs["field"]), None)
        if step is None or step["kind"] != "file":
            return {"error": f"'{call.inputs['field']}' is not a document step of this form.",
                    "kind": "not_found"}, "error"
        return await self._record_document(call, state, step,
                                           str(call.inputs["file_ref"])), "success"

    async def step(self, call):
        """Ask the next step ourselves, then record exactly as answer or
        attach would. One question per call: a call is short-lived, and
        a restart between two questions loses nothing, because every
        accepted answer is already a record."""
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        pending = state.pending()
        if not pending:
            return {"done": True, "accepted": True, "problems": [], "remaining": 0}, "success"
        step = pending[0]
        question = self._question(step)
        if step["kind"] == "file":
            if not is_required(step):
                # A document card has no way to say "I have none": ask
                # that first, in words, and only then for the file.
                have = await call.ask(f"{question} Do you have it now?",
                                      choices=["Attach it", "Skip"])
                if have is None:
                    return {"error": "Nobody could be asked here — this step runs "
                                     "in a chat with a person.", "kind": "no_audience"}, "error"
                if str(have).strip().lower() in ("skip", *SKIP_WORDS):
                    outcome = await self._record_answer(call, state, step, "")
                    following = outcome.get("next") or {}
                    return {"done": bool(following.get("done")), "field": step["field"],
                            "accepted": True, "problems": [],
                            "remaining": int(following.get("remaining") or 0),
                            "next": following}, "success"
            answer = await call.ask(question, expects="file")
        else:
            choices = list(step["choices"]) if step["kind"] == "choice" else []
            if choices and not is_required(step):
                choices.append("Skip")
            answer = await call.ask(question, choices=choices or None)
        if answer is None:
            return {"error": "Nobody could be asked here — this step runs in a chat "
                             "with a person.", "kind": "no_audience"}, "error"
        answer = str(answer).strip()
        if step["kind"] == "file":
            outcome = await self._record_document(call, state, step, answer)
        else:
            skipped = not is_required(step) and answer.lower() in SKIP_WORDS
            outcome = await self._record_answer(call, state, step, "" if skipped else answer)
        following = outcome.get("next") or {}
        return {"done": bool(following.get("done")), "field": step["field"],
                "accepted": outcome["accepted"], "problems": outcome["problems"],
                "remaining": int(following.get("remaining") or 0),
                "next": following}, "success"

    async def review(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        answers = [{"field": s["field"], "label": s["label"],
                    "value": state.answers.get(s["field"]),
                    "skipped": s["field"] in state.answers
                    and state.answers[s["field"]] in (None, "")}
                   for s in state.form["steps"] if s["kind"] != "file"]
        documents = [{"field": s["field"], "label": s["label"],
                      **(state.documents.get(s["field"]) or {"attached": False})}
                     for s in state.form["steps"] if s["kind"] == "file"]
        missing = state.missing()
        return {"ready": not missing and state.status == "draft",
                "status": state.status, "title": state.keys.get("title", ""),
                "answers": answers, "documents": documents, "missing": missing,
                "reference": state.keys.get("reference", "")}, "success"

    async def submit(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        missing = state.missing()
        if missing:
            return {"error": "Not ready to submit; still missing: " + ", ".join(missing) + ".",
                    "kind": "incomplete", "missing": missing}, "error"
        stamp = now_iso()
        reference = f"APP-{stamp[:10].replace('-', '')}-{secrets.token_hex(3).upper()}"
        await call.resources.update_data("application", state.ref, {
            "status": "submitted", "reference": reference, "submitted_at": stamp})
        return {"reference": reference, "submitted_at": stamp}, "success"

    async def withdraw(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        await call.resources.update_data("application", state.ref, {"status": "withdrawn"})
        return {"status": "withdrawn"}, "success"

    # -- recording --------------------------------------------------------
    @staticmethod
    def _question(step: dict) -> str:
        text = str(step["label"]).strip()
        if step.get("help"):
            text += f" {str(step['help']).strip()}"
        if step["kind"] == "date":
            text += " (YYYY-MM-DD)"
        if step["kind"] == "file":
            names = {"application/pdf": "a PDF", "image/png": "a PNG photo",
                     "image/jpeg": "a JPEG photo"}
            text += " — attach " + " or ".join(names.get(a, a) for a in step["accept"]) + "."
        if not is_required(step) and step["kind"] != "choice":
            text += " Say skip to leave it out."
        return text

    async def _record_answer(self, call, state: State, step: dict, value) -> dict:
        stored, problems = check_answer(step, value, state.answers)
        if problems:
            return {"accepted": False, "problems": problems, "next": state.next()}
        fields = {"application": state.ref, "field": step["field"], "label": step["label"],
                  "value": "" if stored is None else str(stored),
                  "skipped": "yes" if stored is None else "no"}
        existing = state.answer_refs.get(step["field"])
        if existing:
            await call.resources.update_data("answer", existing, fields)
        else:
            created = await call.resources.create_data("answer", fields)
            state.answer_refs[step["field"]] = created["resource_ref"]
        state.answers[step["field"]] = stored
        await call.resources.update_data("application", state.ref,
                                         {"progress": state.progress()})
        return {"accepted": True, "problems": [], "value": fields["value"],
                "next": state.next()}

    async def _record_document(self, call, state: State, step: dict, file_ref: str) -> dict:
        try:
            record = await call.resources.read_file("document", file_ref)
        except Exception as exc:
            return {"accepted": False, "problems": [f"The file could not be read: {exc}"],
                    "next": state.next()}
        document, problems = inspect(record, file_ref, step)
        if problems:
            return {"accepted": False, "problems": problems, "next": state.next()}
        read, problems = await read_fields(call, step, document)
        if not problems:
            problems = run_checks(step, read, state.answers)
        shown = {"attached": True, "file_ref": file_ref, "filename": document.filename,
                 "type": document.mime, "size": document.size,
                 **({"pages": document.pages} if document.pages is not None else {}),
                 "read": read}
        if problems:
            return {"accepted": False, "problems": problems, "document": shown,
                    "next": state.next()}
        fields = {"application": state.ref, "field": step["field"], "label": step["label"],
                  "file_ref": file_ref, "filename": document.filename,
                  "type": document.mime, "size": document.size,
                  **({"pages": document.pages} if document.pages is not None else {}),
                  "read": json.dumps(read, ensure_ascii=False)}
        existing = state.document_refs.get(step["field"])
        if existing:
            await call.resources.update_data("attachment", existing, fields)
        else:
            created = await call.resources.create_data("attachment", fields)
            state.document_refs[step["field"]] = created["resource_ref"]
        state.documents[step["field"]] = shown
        await call.resources.update_data("application", state.ref,
                                         {"progress": state.progress()})
        return {"accepted": True, "problems": [], "document": shown, "next": state.next()}
