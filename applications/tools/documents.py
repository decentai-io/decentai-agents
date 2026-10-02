"""A document attached to a step: checked in code for what code can
check — type, size, pages — and, where the form says what to read from
it, read by the model: a PDF by its text, a photo by its pixels. What
the model read is held to the text where there is text."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
from typing import Any, Dict, List, Optional, Tuple

TEXT_SHOWN = 12000
IMAGE_TYPES = ("image/png", "image/jpeg")


class Inspected:
    def __init__(self, file_ref: str, filename: str, mime: str, size: int,
                 text: str = "", pages: Optional[int] = None):
        self.file_ref = file_ref
        self.filename = filename
        self.mime = mime
        self.size = size
        self.text = text
        self.pages = pages

    @property
    def is_image(self) -> bool:
        return self.mime in IMAGE_TYPES


def inspect(record: Dict[str, Any], file_ref: str,
            step: Dict[str, Any]) -> Tuple[Optional[Inspected], List[str]]:
    """The file as this step may take it, or the problems. A PDF's text
    is extracted here; an image carries none."""
    filename = str(record.get("filename") or file_ref)
    mime = str(record.get("file_type") or "")
    if not mime:
        # A store that kept no type: the name says what it is, or nothing.
        mime = mimetypes.guess_type(filename)[0] or ""
    raw = base64.b64decode(str(record.get("content_base64") or ""))
    size = int(record.get("file_size") or len(raw))
    problems: List[str] = []
    accept = [str(a) for a in step.get("accept") or []]
    if mime not in accept:
        problems.append(f"{filename} is {mime or 'of no known type'}; this step takes "
                        + ", ".join(accept) + ".")
    limit = float(step.get("max_mb") or 25)
    if size > limit * 1024 * 1024:
        problems.append(f"{filename} is {size:,} bytes; this step takes at most "
                        f"{limit:g} MB.")
    if not raw:
        problems.append(f"{filename} is empty.")
    if problems:
        return None, problems
    text, pages = "", None
    if mime == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            pages = len(reader.pages)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            return None, [f"{filename} could not be opened as a PDF: {exc}"]
        if pages and int(step.get("max_pages") or 0) and pages > int(step["max_pages"]):
            return None, [f"{filename} has {pages} pages; this step takes at most "
                          f"{step['max_pages']}."]
    return Inspected(file_ref, filename, mime, size, text, pages), []


async def read_fields(call, step: Dict[str, Any], document: Inspected
                      ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """What the form asked to read from the document, as the model read
    it: ``{field: {value, status}}`` where status is ``verified`` (the
    text carries the value), ``assumed`` (a reading the text could not
    confirm, or a photo), or ``missing``. A photo is shown to the model;
    a scan with no text layer is treated as one."""
    wanted: Dict[str, str] = dict(step.get("read") or {})
    if not wanted:
        return {}, []
    keys = json.dumps(wanted, ensure_ascii=False)
    ask = (f"QUESTIONS\nAnswer as JSON with exactly these keys — the value is the "
           f"text as it appears in the document, or \"\" when it is not there. "
           f"Dates as YYYY-MM-DD. {keys}")
    try:
        if document.is_image or not document.text.strip():
            prompt = f"DOCUMENT ({document.filename}) — a picture.\n{ask}"
            answer = await call.llm(prompt, images=[
                {"resource_id": "document", "ref": document.file_ref}])
            by_text = False
        else:
            prompt = (f"DOCUMENT ({document.filename})\n"
                      f"{document.text[:TEXT_SHOWN]}\n{ask}")
            answer = await call.llm(prompt)
            by_text = True
    except Exception as exc:
        return {}, [f"The document could not be read by the model: {exc}"]
    parsed = _json_object(answer)
    read: Dict[str, Dict[str, Any]] = {}
    for field in wanted:
        value = str(parsed.get(field) or "").strip()
        if not value:
            read[field] = {"value": "", "status": "missing"}
        elif by_text and _in_text(value, document.text):
            read[field] = {"value": value, "status": "verified"}
        else:
            read[field] = {"value": value, "status": "assumed"}
    return read, []


def _json_object(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm(text: str) -> str:
    return re.sub(r"[\s\-/.:]", "", str(text or "")).lower()


def _in_text(value: str, text: str) -> bool:
    return bool(_norm(value)) and _norm(value) in _norm(text)
