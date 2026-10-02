"""Forms: the files in ``forms/`` that say what an application asks, in
what order, and what each answer must satisfy — and the checks that hold
an answer to them. Everything here is deterministic; the model is never
asked whether an answer is acceptable."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

FORMS_DIR = Path(__file__).resolve().parent.parent / "forms"
KINDS = ("text", "number", "date", "choice", "email", "file")
CHECK_KINDS = ("date_after",)
CENT = Decimal("0.01")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FormError(Exception):
    """A form file this agent cannot use, and why."""


def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(text: Any) -> Optional[date]:
    """YYYY-MM-DD, or nothing. A day and a month with no year, or in an
    order that could be read two ways, is not a date this agent takes."""
    text = str(text or "").strip()
    if not _ISO_DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def load_forms() -> Dict[str, Dict[str, Any]]:
    """Every form in the folder, checked. A broken form is a FormError
    naming the file and the fault; the agent refuses rather than asks
    questions it cannot check."""
    forms: Dict[str, Dict[str, Any]] = {}
    for path in sorted(FORMS_DIR.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise FormError(f"{path.name}: not valid YAML ({exc})")
        problems = check_form(document)
        if problems:
            raise FormError(f"{path.name}: " + "; ".join(problems))
        forms[str(document["id"])] = document
    return forms


def check_form(document: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(document, dict):
        return ["a form is a mapping"]
    for key in ("id", "title", "steps"):
        if not document.get(key):
            problems.append(f"{key} is required")
    if not re.match(r"^[a-z][a-z0-9_]{1,59}$", str(document.get("id") or "")):
        problems.append("id must be lower-case letters, digits and underscores")
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["steps must be a non-empty list"]
    seen: set = set()
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict):
            problems.append(f"{where}: a step is a mapping")
            continue
        field = str(step.get("field") or "")
        if not re.match(r"^[a-z][a-z0-9_]{0,59}$", field):
            problems.append(f"{where}: field must be a lower-case identifier")
        if field in seen:
            problems.append(f"{where}: field '{field}' repeats")
        seen.add(field)
        if not step.get("label"):
            problems.append(f"{where}: label is required")
        kind = step.get("kind")
        if kind not in KINDS:
            problems.append(f"{where}: kind must be one of {', '.join(KINDS)}")
        if kind == "choice" and not (isinstance(step.get("choices"), list)
                                     and 2 <= len(step["choices"]) <= 8):
            problems.append(f"{where}: a choice step offers 2 to 8 choices")
        if kind == "file":
            accept = step.get("accept")
            if not isinstance(accept, list) or not accept:
                problems.append(f"{where}: a file step names the types it accepts")
            read = step.get("read")
            if read is not None and not (isinstance(read, dict) and read
                                         and all(isinstance(v, str) for v in read.values())):
                problems.append(f"{where}: read maps field names to what to read")
            for check in step.get("checks") or []:
                if not isinstance(check, dict) or check.get("kind") not in CHECK_KINDS:
                    problems.append(f"{where}: a check's kind must be one of {', '.join(CHECK_KINDS)}")
                    continue
                if check.get("read_field") not in (read or {}):
                    problems.append(f"{where}: check reads '{check.get('read_field')}', which read does not name")
                if not any(s.get("field") == check.get("after_field") and s.get("kind") == "date"
                           for s in steps[:index] if isinstance(s, dict)):
                    problems.append(f"{where}: check's after_field must be an earlier date step")
        if step.get("after") is not None and not any(
                s.get("field") == step["after"] and s.get("kind") == "date"
                for s in steps[:index] if isinstance(s, dict)):
            problems.append(f"{where}: after must name an earlier date step")
        if step.get("pattern") is not None:
            try:
                re.compile(str(step["pattern"]))
            except re.error:
                problems.append(f"{where}: pattern is not a valid regular expression")
    return problems


def is_required(step: Dict[str, Any]) -> bool:
    return step.get("required", True) is not False


def public_step(step: Dict[str, Any], remaining: int = 0) -> Dict[str, Any]:
    """A step as the assistant is shown it: what to ask, and how."""
    shown: Dict[str, Any] = {
        "field": step["field"], "label": step["label"], "kind": step["kind"],
        "required": is_required(step),
    }
    if step.get("help"):
        shown["help"] = step["help"]
    if step["kind"] == "choice":
        shown["choices"] = list(step["choices"])
    if step["kind"] == "file":
        shown["accept"] = list(step["accept"])
    if step["kind"] == "date":
        shown["format"] = "YYYY-MM-DD"
    shown["remaining"] = remaining
    return shown


def check_answer(step: Dict[str, Any], value: Any,
                 answers: Dict[str, Any]) -> Tuple[Any, List[str]]:
    """The stored value and the problems — an empty list is acceptance.
    Nothing about an answer is guessed: a date not written as a date is
    refused, a number out of bounds is refused, a choice must be one of
    those offered."""
    kind = step["kind"]
    text = str(value if value is not None else "").strip()
    label = step["label"]
    if not text:
        if is_required(step):
            return None, [f"An answer is required: {label}"]
        return None, []                       # an optional step, skipped
    if kind == "file":
        return None, ["This step takes a document: attach one (applications.attach)."]

    if kind == "text":
        low, high = step.get("min_length"), step.get("max_length")
        if low is not None and len(text) < int(low):
            return None, [f"At least {int(low)} characters are needed."]
        if high is not None and len(text) > int(high):
            return None, [f"At most {int(high)} characters are allowed."]
        if step.get("pattern") and not re.fullmatch(str(step["pattern"]), text):
            hint = step.get("pattern_hint") or "the form's pattern"
            return None, [f"'{text}' is not in the expected form: {hint}."]
        return text, []

    if kind == "email":
        if not _EMAIL.match(text):
            return None, [f"'{text}' is not an email address."]
        return text.lower(), []

    if kind == "number":
        try:
            number = Decimal(text.replace(",", ""))
        except InvalidOperation:
            return None, [f"'{text}' is not a number."]
        if step.get("min") is not None and number < Decimal(str(step["min"])):
            return None, [f"The number must be at least {step['min']}."]
        if step.get("max") is not None and number > Decimal(str(step["max"])):
            return None, [f"The number must be at most {step['max']}."]
        return str(number.quantize(CENT, rounding=ROUND_HALF_UP)), []

    if kind == "date":
        when = parse_date(text)
        if when is None:
            return None, [f"'{text}' is not a date written as YYYY-MM-DD."]
        if step.get("not_before") == "today" and when < today():
            return None, [f"{label} cannot be in the past."]
        if step.get("after"):
            earlier = parse_date(answers.get(step["after"]))
            if earlier is not None and when <= earlier:
                return None, [f"{label} must be after {step['after']} ({earlier.isoformat()})."]
        return when.isoformat(), []

    if kind == "choice":
        for choice in step["choices"]:
            if str(choice).strip().lower() == text.lower():
                return str(choice), []
        return None, [f"'{text}' is not one of the choices: "
                      + ", ".join(str(c) for c in step["choices"]) + "."]

    return None, [f"Unknown step kind '{kind}'."]


def run_checks(step: Dict[str, Any], read: Dict[str, Dict[str, Any]],
               answers: Dict[str, Any]) -> List[str]:
    """The form's rules on what was read from a document. A value that
    could not be read as a date fails the rule that needs it, and says
    so, rather than passing by absence."""
    problems: List[str] = []
    for check in step.get("checks") or []:
        if check.get("kind") != "date_after":
            continue
        field = str(check.get("read_field") or "")
        value = str((read.get(field) or {}).get("value") or "")
        when = parse_date(value)
        if when is None:
            problems.append(
                f"{field} could not be read as a date from the document"
                + (f" (read: '{value}')." if value else "."))
            continue
        after = parse_date(answers.get(str(check.get("after_field") or "")))
        if after is None:
            continue
        months = int(check.get("months") or 0)
        year, month = after.year, after.month + months
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
        threshold = date(year, month, min(after.day, 28))
        if when < threshold:
            problems.append(str(check.get("message")
                                or f"{field} ({when.isoformat()}) must be after "
                                   f"{threshold.isoformat()}."))
    return problems
