"""A small Google Docs (v1) and Drive (v3) client over the connected account.

Two APIs, because Google splits a document in two: the Docs API holds
its content and applies edits, and the Drive API finds documents and
holds their comments (``files/{id}/comments``). Both answer to the same
access token, so one client speaks to both.

The same failure kinds as the Google Drive agent's client — ``auth``
(reconnect), ``http`` (Google refused), ``unknown`` (a write got no
answer; never retried), ``not_found`` — plus ``conflict``: an edit sent
with a required revision that is no longer the document's latest, which
Google refuses without changing anything. Reads are retried once on a
dropped connection; writes never are.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

import requests

DOCS_BASE_URL = "https://docs.googleapis.com"
DRIVE_BASE_URL = "https://www.googleapis.com"
READ_TIMEOUT = 20
WRITE_TIMEOUT = 30

DOCUMENT = "application/vnd.google-apps.document"
FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,lastModifyingUser(displayName,emailAddress)"
# Drive answers a comments request with nothing useful unless it is told
# which fields to send — ``fields`` is required in practice, not optional.
AUTHOR = "author(displayName,emailAddress,me)"
REPLY_FIELDS = f"id,content,{AUTHOR},createdTime,modifiedTime,action,deleted"
COMMENT_FIELDS = (f"id,content,quotedFileContent,{AUTHOR},createdTime,modifiedTime,"
                  f"resolved,deleted,replies({REPLY_FIELDS})")


class GoogleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "auth" | "http" | "unknown" | "not_found" | "conflict"
        self.message = message


def literal(text: str) -> str:
    """A value inside a Drive query: quoted, with quote and backslash escaped."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def quoted(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


def doc_link(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


class DocsClient:
    def __init__(self, secret: Dict[str, Any]):
        # A connected account: the platform ran the consent, keeps the
        # refresh token, and hands this agent an access token that is
        # still good at the moment of the call. Nothing here refreshes.
        self.email = str(secret.get("account") or "")
        self.access_token = str(secret.get("access_token") or "")
        # In tests one loopback server stands in for both Google hosts;
        # the paths (v1/documents, drive/v3/files) keep them apart.
        override = str(secret.get("api_base_url") or "").rstrip("/")
        self.docs_base_url = override or DOCS_BASE_URL
        self.drive_base_url = override or DRIVE_BASE_URL

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if not self.access_token:
            raise GoogleError("auth", "The Google account is not connected — "
                                      "connect it from the agent's Credentials "
                                      "tab.")
        return self.access_token

    @staticmethod
    def _error_body(response: requests.Response) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {"message": (response.text or "").strip()[:300]}
        error = body.get("error")
        if isinstance(error, dict):
            return error
        return {"message": str(error or body)[:300]}

    # -- one request -----------------------------------------------------
    def _request(self, method: str, base: str, path: str, *, params=None,
                 json_body=None, write: bool = False):
        url = f"{base}/{path.lstrip('/')}"
        attempts = 1 if write else 2
        last: Optional[Exception] = None
        for _ in range(attempts):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = requests.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=WRITE_TIMEOUT if write else READ_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if write:
                    raise GoogleError(
                        "unknown", f"No answer from Google for {method} {path}: "
                                   f"the outcome is unknown ({exc}).")
                continue
            if response.status_code == 401:
                raise GoogleError("auth", "The Google connection has expired or "
                                          "was revoked — reconnect the account "
                                          "from its Credentials page.")
            if response.status_code == 404:
                raise GoogleError("not_found", "Google has no such document or comment.")
            if response.status_code >= 400:
                error = self._error_body(response)
                message = str(error.get("message") or error.get("status") or "")[:300]
                # A write whose required revision is stale comes back as a
                # 400; it is the one refusal the edit tool must tell apart.
                if (write and response.status_code == 400
                        and ("revision" in message.lower()
                             or error.get("status") == "FAILED_PRECONDITION")):
                    raise GoogleError("conflict", f"Google refused the edit because the "
                                                  f"document has changed: {message}")
                raise GoogleError("http", f"Google refused {method} {path}: {message}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GoogleError("http", f"Google could not be reached: {last}")

    def _docs(self, method, path, **kwargs):
        return self._request(method, self.docs_base_url, path, **kwargs)

    def _drive(self, method, path, **kwargs):
        return self._request(method, self.drive_base_url, path, **kwargs)

    # -- Drive: finding documents and who the account is -----------------
    def about(self) -> Dict[str, Any]:
        return self._drive("GET", "drive/v3/about", params={"fields": "user(emailAddress,displayName)"})

    def find_documents(self, text: str, page_size: int) -> List[Dict[str, Any]]:
        value = literal(text)
        # Drive refuses orderBy on a fullText query, so newest-first is
        # sorted here, over the page Drive returned.
        answer = self._drive("GET", "drive/v3/files", params={
            "q": f"mimeType = '{DOCUMENT}' and trashed = false and "
                 f"(name contains {value} or fullText contains {value})",
            "pageSize": page_size, "fields": f"files({FILE_FIELDS})"})
        files = answer.get("files") or []
        return sorted(files, key=lambda f: str(f.get("modifiedTime") or ""), reverse=True)

    def file(self, file_id: str) -> Dict[str, Any]:
        return self._drive("GET", "drive/v3/files/" + quoted(file_id), params={"fields": FILE_FIELDS})

    # -- Docs: content and edits -----------------------------------------
    def document(self, document_id: str) -> Dict[str, Any]:
        return self._docs("GET", "v1/documents/" + quoted(document_id))

    def batch_update(self, document_id: str, requests_: List[Dict[str, Any]],
                     required_revision_id: str) -> Dict[str, Any]:
        return self._docs("POST", f"v1/documents/{quoted(document_id)}:batchUpdate", write=True,
                          json_body={"requests": requests_,
                                     "writeControl": {"requiredRevisionId": required_revision_id}})

    # -- Drive: comments --------------------------------------------------
    def comments(self, file_id: str, page_size: int = 100, page_token: str = "",
                 start_modified: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size, "includeDeleted": "false",
                                  "fields": f"nextPageToken,comments({COMMENT_FIELDS})"}
        if page_token:
            params["pageToken"] = page_token
        if start_modified:
            params["startModifiedTime"] = start_modified
        return self._drive("GET", f"drive/v3/files/{quoted(file_id)}/comments", params=params)

    def comment(self, file_id: str, comment_id: str) -> Dict[str, Any]:
        return self._drive("GET", f"drive/v3/files/{quoted(file_id)}/comments/{quoted(comment_id)}",
                           params={"fields": COMMENT_FIELDS})

    def add_comment(self, file_id: str, content: str, quote: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"content": content}
        if quote:
            body["quotedFileContent"] = {"mimeType": "text/plain", "value": quote}
        return self._drive("POST", f"drive/v3/files/{quoted(file_id)}/comments",
                           params={"fields": COMMENT_FIELDS}, json_body=body, write=True)

    def add_reply(self, file_id: str, comment_id: str, content: str,
                  action: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if content:
            body["content"] = content
        if action:
            body["action"] = action
        return self._drive("POST", f"drive/v3/files/{quoted(file_id)}/comments/"
                                   f"{quoted(comment_id)}/replies",
                           params={"fields": REPLY_FIELDS}, json_body=body, write=True)


# -- reading a document's body ------------------------------------------

class Paragraph:
    """One paragraph of the body as the agent numbers it, with the range
    it occupies — Google's own indexes, never counted from the text,
    because Google counts in UTF-16 units and includes objects."""

    def __init__(self, number: int, element: Dict[str, Any], in_table: bool):
        paragraph = element.get("paragraph") or {}
        parts = paragraph.get("elements") or []
        text = "".join(str((p.get("textRun") or {}).get("content") or "") for p in parts)
        self.number = number
        self.text = text[:-1] if text.endswith("\n") else text
        self.style = str((paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "NORMAL_TEXT")
        self.start = int(element.get("startIndex") or 0)
        self.end = int(element.get("endIndex") or 0)
        self.in_table = in_table
        # An image, a person chip, a footnote reference: replacing the
        # text range would delete it too, so such a paragraph is not
        # rewritten here.
        self.plain = all("textRun" in p for p in parts)

    def row(self) -> Dict[str, Any]:
        return {"index": self.number, "style": self.style, "text": self.text,
                "in_table": self.in_table}


def paragraphs(document: Dict[str, Any]) -> List[Paragraph]:
    """Every paragraph of the body in reading order, table cells
    included, numbered from 1. A table of contents is generated by Docs
    and left out."""
    found: List[Paragraph] = []

    def walk(content, in_table):
        for element in content or []:
            if "paragraph" in element:
                found.append(Paragraph(len(found) + 1, element, in_table))
            elif "table" in element:
                for row in (element["table"].get("tableRows") or []):
                    for cell in row.get("tableCells") or []:
                        walk(cell.get("content"), True)

    walk((document.get("body") or {}).get("content"), False)
    return found


def person(user: Any) -> str:
    user = user or {}
    return str(user.get("displayName") or user.get("emailAddress") or "")
