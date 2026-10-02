"""A loopback Google Forms: the v1 subset the Google Forms agent calls,
plus the Drive files list it finds forms with, over real HTTP, holding a
fictional customer feedback form for Sidra Office Supplies.

It behaves the way Forms does where the agent depends on it: a response
carries answers by question id, never by title; a grid is a question
group whose rows are questions; submit times are RFC 3339 with as many
fraction digits as Google cares to write; ``filter`` understands only
``timestamp >= N`` and ``timestamp > N``; responses come back in no
promised order (here, deliberately scrambled) and in pages.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

FORM = "application/vnd.google-apps.form"


def moment(stamp: str) -> str:
    match = re.match(r"^(.{19})(?:\.(\d+))?Z$", stamp)
    return f"{match.group(1)}.{(match.group(2) or '').ljust(9, '0')}Z" if match else ""


class GoogleFormsStub:
    ACCOUNT = "demo@sidra.example"
    #: Small pages, so the agent's paging is exercised on a handful.
    PAGE_CAP = 2

    def __init__(self):
        self.forms: Dict[str, Dict[str, Any]] = {}
        self.responses: Dict[str, List[Dict[str, Any]]] = {}
        self.filters: List[str] = []
        self._n = 0
        self._server: Optional[ThreadingHTTPServer] = None

    # -- fictional data ------------------------------------------------
    def add_form(self, title: str, items: List[Dict[str, Any]], description: str = "",
                 modified: str = "2026-09-01T10:00:00.000Z", linked_sheet_id: str = "") -> str:
        self._n += 1
        form_id = f"1FaIpQL{self._n:04d}Sidra"
        form = {"formId": form_id, "info": {"title": title, "documentTitle": title,
                                            "description": description},
                "items": items, "responderUri": f"https://docs.google.com/forms/d/e/{form_id}/viewform",
                "revisionId": "00000001", "_modified": modified}
        if linked_sheet_id:
            form["linkedSheetId"] = linked_sheet_id
        self.forms[form_id] = form
        self.responses[form_id] = []
        return form_id

    def respond(self, form_id: str, submitted: str, answers: Dict[str, Any],
                email: str = "") -> str:
        """Someone submits the form. answers: question id → text, a list of
        texts, or {"file": name}."""
        self._n += 1
        response_id = f"ACYDBN{self._n:04d}"
        shaped = {}
        for question_id, value in answers.items():
            if isinstance(value, dict):
                shaped[question_id] = {"questionId": question_id, "fileUploadAnswers": {"answers": [
                    {"fileId": f"1up{self._n}", "fileName": value["file"],
                     "mimeType": "application/pdf"}]}}
            else:
                values = value if isinstance(value, list) else [value]
                shaped[question_id] = {"questionId": question_id, "textAnswers": {
                    "answers": [{"value": v} for v in values]}}
        response = {"formId": form_id, "responseId": response_id, "createTime": submitted,
                    "lastSubmittedTime": submitted, "answers": shaped}
        if email:
            response["respondentEmail"] = email
        self.responses[form_id].append(response)
        return response_id

    def edit(self, form_id: str, response_id: str, submitted: str, question_id: str, text: str) -> None:
        response = next(r for r in self.responses[form_id] if r["responseId"] == response_id)
        response["lastSubmittedTime"] = submitted
        response["answers"][question_id] = {"questionId": question_id,
                                            "textAnswers": {"answers": [{"value": text}]}}

    # -- the server ------------------------------------------------------
    def start(self) -> "GoogleFormsStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _json(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"error": {"code": status, "message": message}})

            def do_GET(self):  # noqa: N802
                url = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(url.path)
                path = path[4:] if path.startswith("/api") else path
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                if self.headers.get("Authorization") != "Bearer at-1":
                    return self._error(401, "Request had invalid authentication credentials.")
                if path == "/drive/v3/about":
                    return self._json(200, {"user": {"emailAddress": stub.ACCOUNT}})
                if path == "/drive/v3/files":
                    q = query.get("q", "")
                    wanted = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
                    rows = [f for f in stub.forms.values()
                            if f"mimeType = '{FORM}'" in q
                            and (not wanted or re.sub(r"\\(.)", r"\1", wanted.group(1)).lower()
                                 in f["info"]["title"].lower())]
                    rows.sort(key=lambda f: f["_modified"], reverse=True)
                    size = int(query.get("pageSize") or 100)
                    return self._json(200, {"files": [
                        {"id": f["formId"], "name": f["info"]["title"], "modifiedTime": f["_modified"],
                         "webViewLink": f"https://docs.google.com/forms/d/{f['formId']}/edit",
                         "owners": [{"emailAddress": stub.ACCOUNT}]} for f in rows[:size]]})
                match = re.match(r"^/v1/forms/([^/]+)(/responses)?$", path)
                form = stub.forms.get(match.group(1)) if match else None
                if form is None:
                    return self._error(404, "Requested entity was not found.")
                if not match.group(2):
                    return self._json(200, {k: v for k, v in form.items() if not k.startswith("_")})
                rows = list(stub.responses[form["formId"]])
                if "filter" in query:
                    stub.filters.append(query["filter"])
                    parsed = re.match(r"^timestamp (>=|>) (\S+)$", query["filter"])
                    if not parsed or not moment(parsed.group(2)):
                        return self._error(400, f"Invalid filter: {query['filter']}")
                    bound = moment(parsed.group(2))
                    rows = [r for r in rows if (moment(r["lastSubmittedTime"]) >= bound
                                                if parsed.group(1) == ">="
                                                else moment(r["lastSubmittedTime"]) > bound)]
                # No promised order: newest and oldest interleaved.
                rows = rows[1::2] + rows[0::2]
                start = int(query.get("pageToken") or 0)
                size = min(int(query.get("pageSize") or 5000), stub.PAGE_CAP)
                answer: Dict[str, Any] = {}
                if rows[start:start + size]:
                    answer["responses"] = rows[start:start + size]
                if start + size < len(rows):
                    answer["nextPageToken"] = str(start + size)
                self._json(200, answer)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def secret(self, access_token: str = "at-1") -> Dict[str, Any]:
        """The credential as the platform hands it to the agent."""
        return {"account": self.ACCOUNT, "access_token": access_token,
                "api_base_url": self.url + "/api"}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
