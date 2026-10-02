"""Reading a stored file as JSON, and finding things inside it.

Two ideas live here. First, a document is either JSON or JSON Lines, and
which one is decided by trying: a file that is not valid JSON but whose
every non-empty line is gets read as JSON Lines, which is what most
exports actually are. A file that is neither is reported with the line
and column where parsing stopped, never half-read.

Second, a path language small enough to state completely: ``a.b`` for a
nested key, ``a[0]`` for one item, ``a[*]`` for every item, ``*`` for
every value of an object, and ``$`` for the whole document. There is no
filtering and no arithmetic — a path names places, and the caller does
the thinking.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from typing import Any, Dict, Iterable, List, Tuple

MAX_BYTES = 12 * 1024 * 1024
MAX_DEPTH = 24
MAX_KEYS = 300


class Doc:
    """One file, read: its kind, what parsed, and what failed."""

    def __init__(self, file_ref: str, filename: str, kind: str, data: Any,
                 records: List[Any], size: int, problem: str = "",
                 line: int = 0, column: int = 0):
        self.file_ref = file_ref
        self.filename = filename
        self.kind = kind              # json | jsonl | csv | unsupported
        self.data = data
        self.records = records        # what "a record" means for this file
        self.size = size
        self.problem = problem
        self.line = line
        self.column = column

    @property
    def valid(self) -> bool:
        return not self.problem and self.kind in ("json", "jsonl")

    @property
    def top_level(self) -> str:
        return type_of(self.data) if self.valid else "none"


def type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


async def read_text(call, resource_id: str, file_ref: str) -> Tuple[str, str, str]:
    """The stored file as (filename, declared type, text)."""
    record = await call.resources.read_file(resource_id, file_ref)
    filename = str(record.get("filename") or "")
    file_type = str(record.get("file_type") or record.get("mime_type") or "")
    if record.get("content_base64"):
        raw = base64.b64decode(record["content_base64"])
        if len(raw) > MAX_BYTES:
            raise ValueError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        text = raw.decode("utf-8-sig", "replace")
    else:
        text = str(record.get("content") or "")
        if len(text.encode("utf-8", "replace")) > MAX_BYTES:
            raise ValueError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
        text = text.lstrip("﻿")
    return filename, file_type, text


async def load(call, resource_id: str, file_ref: str) -> Doc:
    filename, file_type, text = await read_text(call, resource_id, file_ref)
    return parse(file_ref, filename, file_type, text)


def parse(file_ref: str, filename: str, file_type: str, text: str) -> Doc:
    size = len(text.encode("utf-8", "replace"))
    stripped = text.strip()
    if not stripped:
        return Doc(file_ref, filename, "unsupported", None, [], size,
                   problem="The file is empty.")

    try:
        data = json.loads(text)
    except ValueError as exc:
        lines, records, failure = _jsonl(text)
        if failure is None:
            return Doc(file_ref, filename, "jsonl", lines, records, size)
        if _looks_csv(filename, file_type, text):
            return Doc(file_ref, filename, "csv", None, [], size,
                       problem="This is a CSV file, not JSON. "
                               "shape.to_json turns it into JSON.")
        line = getattr(exc, "lineno", 0) or 0
        column = getattr(exc, "colno", 0) or 0
        return Doc(file_ref, filename, "unsupported", None, [], size,
                   problem=f"The file is not valid JSON: {exc}", line=line,
                   column=column)

    records = data if isinstance(data, list) else [data]
    return Doc(file_ref, filename, "json", data, records, size)


def _jsonl(text: str):
    """Every non-empty line as its own JSON value, or the failure."""
    values: List[Any] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except ValueError as exc:
            return None, [], (number, exc)
    if len(values) < 2:
        # One line is just JSON that failed to parse as a whole; calling
        # that "JSON Lines" would hide a real syntax error.
        return None, [], (1, ValueError("not JSON Lines"))
    return values, values, None


def _looks_csv(filename: str, file_type: str, text: str) -> bool:
    name = (filename or "").lower()
    if name.endswith(".csv") or (file_type or "").lower() == "text/csv":
        return True
    head = text.strip().splitlines()[:3]
    return len(head) >= 2 and all("," in line for line in head)


# ── the path language ──────────────────────────────────────────────────

def parse_path(path: str) -> List[Tuple[str, Any]]:
    """``a.b[0].c[*]`` → steps. Raises ValueError on nonsense."""
    text = str(path or "").strip()
    if text in ("", "$"):
        return []
    if text.startswith("$."):
        text = text[2:]
    elif text.startswith("$"):
        text = text[1:]

    steps: List[Tuple[str, Any]] = []
    token = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == ".":
            if token:
                steps.append(_key(token))
                token = ""
            index += 1
            continue
        if char == "[":
            if token:
                steps.append(_key(token))
                token = ""
            close = text.find("]", index)
            if close == -1:
                raise ValueError(f"unclosed '[' in path '{path}'")
            inside = text[index + 1:close].strip()
            if inside == "*":
                steps.append(("wild", None))
            else:
                try:
                    steps.append(("index", int(inside)))
                except ValueError:
                    raise ValueError(
                        f"'[{inside}]' is not an index or '*' in path '{path}'")
            index = close + 1
            continue
        token += char
        index += 1
    if token:
        steps.append(_key(token))
    return steps


def _key(token: str) -> Tuple[str, Any]:
    return ("wild", None) if token == "*" else ("key", token)


def select(data: Any, steps: List[Tuple[str, Any]],
           limit: int = 1000) -> List[Tuple[str, Any]]:
    """(path, value) for everything the steps reach, in document order."""
    found: List[Tuple[str, Any]] = [("$", data)]
    for kind, argument in steps:
        nxt: List[Tuple[str, Any]] = []
        for path, value in found:
            if kind == "key":
                if isinstance(value, dict) and argument in value:
                    nxt.append((_join(path, argument), value[argument]))
            elif kind == "index":
                if isinstance(value, list):
                    position = argument if argument >= 0 else len(value) + argument
                    if 0 <= position < len(value):
                        nxt.append((f"{path}[{position}]", value[position]))
            else:  # wild
                if isinstance(value, list):
                    nxt.extend((f"{path}[{i}]", item)
                               for i, item in enumerate(value))
                elif isinstance(value, dict):
                    nxt.extend((_join(path, key), item)
                               for key, item in value.items())
            if len(nxt) >= limit:
                break
        found = nxt[:limit]
        if not found:
            break
    return found


def _join(path: str, key: str) -> str:
    return str(key) if path == "$" else f"{path}.{key}"


# ── what is actually in a file ─────────────────────────────────────────

def key_map(records: Iterable[Any], max_records: int) -> Tuple[List[Dict[str, Any]], bool, int, int]:
    """Every path the records use, how many records use it, the types
    seen there and one example. Array indexes collapse to ``[]``: what
    matters is that ``lines[].price`` exists, not that item 4 has it."""
    present: Dict[str, int] = {}
    types: Dict[str, List[str]] = {}
    example: Dict[str, str] = {}
    scanned = 0
    depth_seen = 0
    truncated = False

    for record in records:
        if scanned >= max_records:
            truncated = True
            break
        scanned += 1
        seen: Dict[str, None] = {}
        depth_seen = max(depth_seen, _walk(record, "", seen, types, example, 1))
        for path in seen:
            present[path] = present.get(path, 0) + 1

    rows = [{"path": path, "present": count,
             "types": types.get(path, []), "example": example.get(path, "")}
            for path, count in present.items()]
    rows.sort(key=lambda row: (-row["present"], row["path"]))
    keys_truncated = len(rows) > MAX_KEYS
    return rows[:MAX_KEYS], keys_truncated or truncated, scanned, depth_seen


def _walk(value: Any, path: str, seen: Dict[str, None],
          types: Dict[str, List[str]], example: Dict[str, str],
          depth: int) -> int:
    if depth > MAX_DEPTH:
        return depth
    deepest = depth
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _note(child, item, seen, types, example)
            deepest = max(deepest, _walk(item, child, seen, types, example, depth + 1))
    elif isinstance(value, list):
        child = f"{path}[]" if path else "[]"
        for item in value[:200]:
            if isinstance(item, (dict, list)):
                deepest = max(deepest,
                              _walk(item, child, seen, types, example, depth + 1))
            else:
                _note(child, item, seen, types, example)
    return deepest


def _note(path: str, value: Any, seen: Dict[str, None],
          types: Dict[str, List[str]], example: Dict[str, str]) -> None:
    seen[path] = None
    kind = type_of(value)
    known = types.setdefault(path, [])
    if kind not in known and len(known) < 5:
        known.append(kind)
    if path not in example and not isinstance(value, (dict, list)):
        example[path] = preview(value, 80)


def preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# ── flattening, for tables and for comparison ──────────────────────────

def flatten(value: Any, path: str = "") -> Dict[str, Any]:
    """A record as dotted columns. A list of scalars or objects becomes
    its JSON text: a table cell holds one value, and pretending
    otherwise loses data."""
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(item, dict):
                out.update(flatten(item, child))
            elif isinstance(item, list):
                out[child] = json.dumps(item, ensure_ascii=False, default=str)
            else:
                out[child] = item
    else:
        out[path or "value"] = (
            json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, list) else value)
    return out


def leaves(value: Any, path: str = "$") -> Dict[str, Any]:
    """Every scalar in the document, by full path — what a comparison
    walks. Arrays are compared by position, so their indexes stay."""
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        if not value:
            out[path] = {}
        for key, item in value.items():
            out.update(leaves(item, _join(path, key)))
    elif isinstance(value, list):
        if not value:
            out[path] = []
        for index, item in enumerate(value):
            out.update(leaves(item, f"{path}[{index}]"))
    else:
        out[path] = value
    return out


def cell(value: Any) -> str:
    """One value as a CSV cell: text as itself, everything else as JSON,
    so a reader can tell null from the word null."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def read_csv(text: str, max_rows: int) -> Tuple[List[str], List[List[str]], bool]:
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = []
    header: List[str] = []
    truncated = False
    for index, row in enumerate(reader):
        if index == 0:
            header = [str(name).strip() for name in row]
            continue
        if len(rows) >= max_rows:
            truncated = True
            break
        if any(str(c).strip() for c in row):
            rows.append([str(c) for c in row])
    return header, rows, truncated


def write_csv(columns: List[str], rows: List[List[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
