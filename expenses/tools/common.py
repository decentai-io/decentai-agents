"""Reading a receipt or policy file as text, and the small deterministic
checks the model's answers are held to."""

from __future__ import annotations

import base64
import io
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

CENT = Decimal("0.01")
CATEGORIES = ("meals", "travel", "accommodation", "transport", "office", "other")
CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "AED": "AED",
                    "USD": "USD", "EUR": "EUR", "GBP": "GBP", "SAR": "SAR", "د.إ": "AED"}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


class Loaded:
    def __init__(self, file_ref: str, filename: str, kind: str, text: str, problem: str = ""):
        self.file_ref = file_ref
        self.filename = filename
        self.kind = kind          # pdf | text | image | unsupported
        self.text = text
        self.problem = problem


async def load(call, resource_id: str, file_ref: str) -> Loaded:
    record = await call.resources.read_file(resource_id, file_ref)
    filename = str(record.get("filename") or "")
    file_type = str(record.get("file_type") or record.get("mime_type") or "").lower()
    raw = base64.b64decode(record["content_base64"]) if record.get("content_base64") \
        else str(record.get("content") or "").encode("utf-8")
    name = filename.lower()
    if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or name.endswith((".jpg", ".jpeg", ".png")) \
            or file_type.startswith("image/"):
        return Loaded(file_ref, filename, "image", "",
                      "A photo has no text this agent can read; enter its details and confirm them.")
    if raw[:5] == b"%PDF-" or name.endswith(".pdf") or file_type == "application/pdf":
        return _pdf(file_ref, filename, raw)
    try:
        return Loaded(file_ref, filename, "text", raw.decode("utf-8"))
    except UnicodeDecodeError:
        return Loaded(file_ref, filename, "unsupported", "", "Not a PDF, text or image file.")


def _pdf(file_ref: str, filename: str, raw: bytes) -> Loaded:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        return Loaded(file_ref, filename, "pdf", "", f"The PDF could not be opened: {exc}")
    text = "\n".join(pages).strip()
    if len(text) < 20:
        return Loaded(file_ref, filename, "pdf", "",
                      "The PDF has no text layer (a scan); enter its details and confirm them.")
    return Loaded(file_ref, filename, "pdf", text)


def money(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    if not text or text in ("-", "."):
        return None
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def out(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def iso_date(text: Any) -> Optional[date]:
    text = str(text or "").strip()
    if len(text) != 10:
        return None
    try:
        found = date.fromisoformat(text)
    except ValueError:
        return None
    return found if found.isoformat() == text else None


def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(text: str) -> Optional[date]:
    """A date written the ways receipts write them; ambiguous day/month
    order (12/09/2026) is read day-first only when the first number
    cannot be a month."""
    text = str(text or "")
    found = iso_date(text.strip()) or (iso_date(_ISO.search(text).group(0)) if _ISO.search(text) else None)
    if found:
        return found
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        match = pattern.search(text)
        if match:
            day, month, year = ((match.group(1), match.group(2), match.group(3)) if order == "dm"
                                else (match.group(2), match.group(1), match.group(3)))
            number = MONTHS.get(month.lower())
            if number:
                try:
                    return date(int(year), number, int(day))
                except ValueError:
                    return None
    match = _DMY.search(text)
    if match:
        a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            day, month = b, a
        elif a <= 12 and b <= 12 and a == b:
            day, month = a, b
        else:
            return None                      # 03/04/2026: nobody can tell
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def amounts_in(text: str) -> List[Decimal]:
    """Every money-looking number on the receipt, to the cent."""
    found = []
    for match in re.finditer(r"(?<![\w.])-?\d{1,3}(?:,\d{3})*(?:\.\d{2})|(?<![\w.])-?\d+\.\d{2}", text):
        value = money(match.group(0))
        if value is not None:
            found.append(value)
    return found


def currencies_in(text: str) -> List[str]:
    codes = set()
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            codes.add(code)
    for match in re.finditer(r"\b(USD|EUR|GBP|AED|SAR|CAD|AUD|INR)\b", text.upper()):
        codes.add(match.group(1))
    return sorted(codes)


def normalize_merchant(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def parse_json(answer: str):
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


def keys_of(record: Dict[str, Any]) -> Dict[str, Any]:
    return record.get("keys") or {}


def receipt_row(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = keys_of(record)
    row = {"receipt_ref": str(record.get("resource_ref") or ""),
           "claim_ref": str(keys.get("claim_ref") or ""),
           "file_ref": str(keys.get("file_ref") or ""),
           "filename": str(keys.get("filename") or ""),
           "merchant": str(keys.get("merchant") or ""),
           "date": str(keys.get("date") or ""),
           "currency": str(keys.get("currency") or ""),
           "category": str(keys.get("category") or "unknown"),
           "status": str(keys.get("status") or "extracted"),
           "verified": str(keys.get("verified") or ""),
           "assumed": str(keys.get("assumed") or ""),
           "duplicate_of": str(keys.get("duplicate_of") or ""),
           "note": str(keys.get("note") or "")}
    if keys.get("amount") not in (None, ""):
        row["amount"] = out(money(keys["amount"]) or Decimal("0"))
    return row


def rules_of(records: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """name -> (value, quote), the latest of each name."""
    rules: Dict[str, Tuple[str, str]] = {}
    for record in records:
        keys = keys_of(record)
        rules[str(keys.get("name") or "")] = (str(keys.get("value") or ""), str(keys.get("quote") or ""))
    return rules
