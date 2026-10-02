"""Notion's typed JSON, read as text the assistant can quote.

Every value Notion returns is an object tagged with its type; the
assistant needs "Status: In progress", not the object. Text is clipped
so one long paragraph or property cannot crowd out the rest of a
result — and a clipped text says it was clipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

#: The longest a block's or a comment's text is returned.
BLOCK_CHARS = 2000
#: The longest a property's value is returned.
PROPERTY_CHARS = 500


class Text:
    @staticmethod
    def clip(text: str, limit: int) -> str:
        text = str(text or "")
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @staticmethod
    def rich(parts: Any) -> str:
        """A rich-text array as its plain text."""
        out = []
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            plain = part.get("plain_text")
            if plain is None:
                plain = ((part.get("text") or {}).get("content")
                         or (part.get("equation") or {}).get("expression") or "")
            out.append(str(plain))
        return "".join(out)

    @staticmethod
    def write(text: str) -> List[Dict[str, Any]]:
        """Plain text as a rich-text array. Notion caps one text object
        at 2,000 characters, so longer text travels in pieces."""
        text = str(text or "")
        pieces = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [""]
        return [{"type": "text", "text": {"content": piece}} for piece in pieces]

    @staticmethod
    def minute(stamp: str) -> str:
        """A Notion timestamp in one spelling, to the minute — the
        granularity Notion keeps last_edited_time at — so two stamps
        compare as strings."""
        text = str(stamp or "").strip()
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")


class PropertyText:
    """One page property's value as text."""

    @classmethod
    def render(cls, prop: Dict[str, Any]) -> str:
        return Text.clip(cls._value(prop), PROPERTY_CHARS)

    @classmethod
    def all(cls, page: Dict[str, Any]) -> Dict[str, str]:
        return {str(name): cls.render(prop)
                for name, prop in (page.get("properties") or {}).items()
                if isinstance(prop, dict)}

    @staticmethod
    def title(obj: Dict[str, Any]) -> str:
        """A page's title (its title-typed property) or a database's."""
        if obj.get("object") == "database":
            return Text.rich(obj.get("title"))
        for prop in (obj.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return Text.rich(prop.get("title"))
        return ""

    @staticmethod
    def person(user: Any) -> str:
        user = user or {}
        return str(user.get("name") or (user.get("person") or {}).get("email")
                   or user.get("id") or "")

    @staticmethod
    def date(value: Any) -> str:
        value = value or {}
        start, end = str(value.get("start") or ""), str(value.get("end") or "")
        return f"{start} → {end}" if end else start

    @classmethod
    def _value(cls, prop: Dict[str, Any]) -> str:
        kind = str(prop.get("type") or "")
        value = prop.get(kind)
        if kind in ("title", "rich_text"):
            return Text.rich(value)
        if kind == "number":
            return "" if value is None else (str(int(value)) if float(value).is_integer()
                                             else str(value))
        if kind in ("select", "status"):
            return str((value or {}).get("name") or "")
        if kind == "multi_select":
            return ", ".join(str(o.get("name") or "") for o in value or [])
        if kind == "date":
            return cls.date(value)
        if kind == "checkbox":
            return "yes" if value else "no"
        if kind in ("url", "email", "phone_number", "created_time", "last_edited_time"):
            return str(value or "")
        if kind in ("people",):
            return ", ".join(cls.person(u) for u in value or [])
        if kind in ("created_by", "last_edited_by"):
            return cls.person(value)
        if kind == "relation":
            return ", ".join(str(r.get("id") or "") for r in value or [])
        if kind == "files":
            return ", ".join(str(f.get("name") or "") for f in value or [])
        if kind == "unique_id":
            value = value or {}
            prefix = str(value.get("prefix") or "")
            number = "" if value.get("number") is None else str(value.get("number"))
            return f"{prefix}-{number}" if prefix else number
        if kind == "formula":
            value = value or {}
            inner = str(value.get("type") or "")
            if inner == "date":
                return cls.date(value.get("date"))
            if inner == "boolean":
                return "yes" if value.get("boolean") else "no"
            return "" if value.get(inner) is None else str(value.get(inner))
        if kind == "rollup":
            value = value or {}
            inner = str(value.get("type") or "")
            if inner == "array":
                return ", ".join(cls._value(item) for item in value.get("array") or [])
            if inner == "date":
                return cls.date(value.get("date"))
            return "" if value.get(inner) is None else str(value.get(inner))
        return f"({kind})" if kind else ""


class BlockText:
    """One block as a line of text, the way a reader would skim it."""

    PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
              "bulleted_list_item": "- ", "quote": "> ", "toggle": "> "}

    #: Blocks whose children are other pages or databases in their own
    #: right: named, never walked into.
    OWN_PAGES = {"child_page", "child_database"}

    @classmethod
    def render(cls, block: Dict[str, Any], number: int = 0) -> str:
        return Text.clip(cls._line(block, number), BLOCK_CHARS)

    @classmethod
    def _line(cls, block: Dict[str, Any], number: int) -> str:
        kind = str(block.get("type") or "")
        body = block.get(kind) or {}
        text = Text.rich(body.get("rich_text"))
        if kind in cls.PREFIX:
            return cls.PREFIX[kind] + text
        if kind == "paragraph":
            return text
        if kind == "numbered_list_item":
            return f"{number or 1}. {text}"
        if kind == "to_do":
            return ("[x] " if body.get("checked") else "[ ] ") + text
        if kind == "code":
            return f"```{body.get('language') or ''}\n{text}\n```"
        if kind == "callout":
            icon = (body.get("icon") or {}).get("emoji") or ""
            return f"Note: {icon + ' ' if icon else ''}{text}"
        if kind == "child_page":
            return f"[page] {body.get('title') or ''} (page_id {block.get('id')})"
        if kind == "child_database":
            return f"[database] {body.get('title') or ''} (database_id {block.get('id')})"
        if kind == "divider":
            return "---"
        if kind == "equation":
            return str(body.get("expression") or "")
        if kind == "table_row":
            return " | ".join(Text.rich(cell) for cell in body.get("cells") or [])
        if kind in ("image", "file", "pdf", "video", "audio"):
            caption = Text.rich(body.get("caption"))
            return f"[{kind}] {caption}".rstrip()
        if kind in ("bookmark", "embed", "link_preview"):
            return f"[link] {body.get('url') or ''}".rstrip()
        if kind == "link_to_page":
            target = body.get("page_id") or body.get("database_id") or ""
            return f"[link to page] {target}"
        return f"[{kind}]" if kind else ""

    @classmethod
    def lines(cls, blocks: List[Dict[str, Any]]) -> List[str]:
        """A list of sibling blocks as lines, numbered lists counted."""
        out, number = [], 0
        for block in blocks:
            number = number + 1 if block.get("type") == "numbered_list_item" else 0
            out.append(cls.render(block, number))
        return out
