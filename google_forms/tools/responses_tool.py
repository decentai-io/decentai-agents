"""Responses: read on request, and handed on as they arrive.

A response comes back as a row keyed by question title — the answer's
text, choices joined, an uploaded file named but never fetched.

A RESPONSES watch remembers how far a form has been read: the submit
time of the newest response handed on (Google's clock, UTC), plus the
ids that sit exactly on that time, so two responses in the same instant
are neither lost nor shown twice. new() asks Google only for responses
at or after that time, hands on what it has not handed on before, and
moves the cursor; its ``responses`` list is what a schedule wakes the
assistant on, so a check that finds nothing costs no model call.
"""

from datetime import datetime, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .forms_api import GoogleError, Questions, moment

#: How much of one answer the model is shown; a paragraph answer can be
#: a letter.
ANSWER_LIMIT = 500


def clip(text: str) -> str:
    return text if len(text) <= ANSWER_LIMIT else text[:ANSWER_LIMIT] + "…"


def answer_text(answer) -> str:
    texts = [str(a.get("value") or "") for a in (answer.get("textAnswers") or {}).get("answers") or []]
    files = [f"file: {f.get('fileName') or f.get('fileId') or 'unnamed'}"
             for f in (answer.get("fileUploadAnswers") or {}).get("answers") or []]
    return clip(", ".join(texts + files))


def response_row(response, questions: Questions):
    """One response as the assistant reads it, answers in the form's
    question order; an answer to a question since removed from the form
    keeps its id as its name."""
    answers = response.get("answers") or {}
    shaped = {}
    for question in questions.rows:
        answer = answers.get(question["question_id"])
        if answer:
            shaped[questions.titles[question["question_id"]]] = answer_text(answer)
    for question_id, answer in answers.items():
        if question_id not in questions.titles:
            shaped[f"question {question_id}"] = answer_text(answer)
    return {"response_id": str(response.get("responseId") or ""),
            "submitted": str(response.get("lastSubmittedTime") or ""),
            "email": str(response.get("respondentEmail") or ""),
            "answers": shaped}


def utc(text: str) -> str:
    """An ISO 8601 date-time as RFC 3339 UTC ("Zulu"), the only form the
    Forms filter accepts — or "" when it is not one."""
    try:
        parsed = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def form_title(form) -> str:
    info = form.get("info") or {}
    return str(info.get("title") or info.get("documentTitle") or "")


class ResponsesTool(ToolBase):
    id = "responses"

    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        since = ""
        if inputs.get("since"):
            since = utc(inputs["since"])
            if not since:
                return {"error": f"since must be an ISO 8601 date-time with a time "
                                 f"zone, not {inputs['since']!r}.", "kind": "invalid"}, "error"
        most = int(inputs.get("max_results") or 10)
        token = str(inputs.get("page_token") or "0")
        if not token.isdigit():
            return {"error": "page_token comes from a previous responses.list.",
                    "kind": "invalid"}, "error"
        start = int(token)
        try:
            form = client.form(str(inputs["form_id"]))
            found, cut_short = client.responses(str(inputs["form_id"]), since)
        except GoogleError as exc:
            return failure(exc)
        # Google promises no order; newest first is put here.
        found.sort(key=lambda r: (moment(r.get("lastSubmittedTime")), str(r.get("responseId") or "")),
                   reverse=True)
        questions = Questions(form)
        page = found[start:start + most]
        result = {"form": form_title(form), "total": len(found), "cut_short": cut_short,
                  "responses": [response_row(r, questions) for r in page]}
        if start + most < len(found):
            result["next_page_token"] = str(start + most)
        return result, "success"

    # -- the watch -------------------------------------------------------
    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        form_id = str(call.inputs["form_id"])
        try:
            form = client.form(form_id)
            found, _ = client.responses(form_id)
        except GoogleError as exc:
            return failure(exc)
        # From now — and "now" is the form's newest response where there
        # is one: that response marks the spot and is not news itself.
        stamped = [r for r in found if moment(r.get("lastSubmittedTime"))]
        if stamped:
            newest = max(stamped, key=lambda r: moment(r.get("lastSubmittedTime")))
            since = str(newest["lastSubmittedTime"])
            ids = sorted(str(r.get("responseId") or "") for r in stamped
                         if moment(r.get("lastSubmittedTime")) == moment(since))
        else:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ids = []
        record = await call.resources.create_data("watch", {
            "form_id": form_id, "form": form_title(form), "since": since,
            "cursor_ids": ",".join(ids), "status": "watching",
            "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": record["resource_ref"], "form": form_title(form),
                "since": since, "responses_so_far": len(found)}, "success"

    async def new(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        if wanted:
            watches = [await call.resources.read_data("watch", wanted)]
        else:
            watches = await call.resources.list_data("watch", {"status": "watching"})
        rows, closed, checked, more = [], [], 0, False
        for watch in watches:
            keys = watch.get("keys") or {}
            if keys.get("status") != "watching":
                continue
            checked += 1
            ref = str(watch.get("resource_ref") or "")
            form_id = str(keys.get("form_id") or "")
            since = str(keys.get("since") or "")
            seen = {i for i in str(keys.get("cursor_ids") or "").split(",") if i}
            try:
                form = client.form(form_id)
                found, _ = client.responses(form_id, since)
            except GoogleError as exc:
                if exc.kind != "not_found":
                    return failure(exc)
                await call.resources.update_data("watch", ref, {"status": "closed"})
                closed.append({"watch_ref": ref, "form": str(keys.get("form") or ""),
                               "message": "The form is gone, or this account can no "
                                          "longer open it; the watch was closed."})
                continue
            # Oldest first. A response already handed on sits exactly on
            # the cursor's time; one edited since has a later time and is
            # news again.
            fresh = sorted((r for r in found
                            if not (str(r.get("responseId") or "") in seen
                                    and moment(r.get("lastSubmittedTime")) == moment(since))),
                           key=lambda r: (moment(r.get("lastSubmittedTime")),
                                          str(r.get("responseId") or "")))
            if len(fresh) > most:
                more = True
                fresh = fresh[:most]
            questions = Questions(form)
            for response in fresh:
                rows.append({**response_row(response, questions),
                             "watch_ref": ref, "form": form_title(form)})
            if fresh:
                newest = str(fresh[-1].get("lastSubmittedTime") or "")
                at_newest = {str(r.get("responseId") or "") for r in fresh
                             if moment(r.get("lastSubmittedTime")) == moment(newest)}
                if moment(newest) == moment(since):
                    at_newest |= seen
                await call.resources.update_data("watch", ref, {
                    "since": newest, "cursor_ids": ",".join(sorted(at_newest))})
        return {"checked": checked, "responses": rows, "closed": closed,
                "more": more}, "success"
