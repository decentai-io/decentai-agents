"""Finding a form, and seeing what it asks.

The Forms API has no list of forms; Drive does, so find() asks Drive for
files of the form type, newest first. get() is the map the assistant
reads before responses: each question's id, title, kind and options.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .forms_api import GoogleError, Questions

#: A form may hold hundreds of questions; the model is shown the first
#: hundred and told how many there are.
MAX_QUESTIONS = 100
OPTION_LIMIT = 50


class FormsTool(ToolBase):
    id = "forms"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            answer = client.find(str(call.inputs.get("name") or "").strip(),
                                 int(call.inputs.get("max_results") or 10),
                                 str(call.inputs.get("page_token") or ""))
        except GoogleError as exc:
            return failure(exc)
        rows = [{"form_id": str(f.get("id") or ""),
                 "name": str(f.get("name") or ""),
                 "modified": str(f.get("modifiedTime") or ""),
                 "owner": str(((f.get("owners") or [{}])[0]).get("emailAddress") or ""),
                 "link": str(f.get("webViewLink") or "")}
                for f in answer.get("files") or []]
        result = {"forms": rows}
        if answer.get("nextPageToken"):
            result["next_page_token"] = str(answer["nextPageToken"])
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            form = client.form(str(call.inputs["form_id"]))
        except GoogleError as exc:
            return failure(exc)
        info = form.get("info") or {}
        questions = Questions(form)
        rows = [{**q, "title": questions.titles[q["question_id"]],
                 "options": q["options"][:OPTION_LIMIT]} for q in questions.rows]
        return {"form_id": str(form.get("formId") or ""),
                "title": str(info.get("title") or info.get("documentTitle") or ""),
                "description": str(info.get("description") or "")[:1000],
                "responder_link": str(form.get("responderUri") or ""),
                "linked_sheet_id": str(form.get("linkedSheetId") or ""),
                "question_count": len(rows),
                "questions": rows[:MAX_QUESTIONS]}, "success"
