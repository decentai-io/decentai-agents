"""A database's schema, and what may be written to it or asked of it.

Notion would take a select option it has never seen and quietly add it
to the database, and it refuses an unknown property with a message that
does not say which properties exist. Both are checked here first, from
the schema itself, so a refusal names the valid choices and nothing is
sent that the database does not already define.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .text import PropertyText, Text

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}"
                  r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")

#: Property types Notion computes itself; nothing can write them.
COMPUTED = {"formula", "rollup", "created_time", "created_by", "last_edited_time",
            "last_edited_by", "unique_id", "verification", "button"}
TEXT_TYPES = {"title", "rich_text", "url", "email", "phone_number"}
OPTION_TYPES = ("select", "multi_select", "status")


class SchemaProblem(Exception):
    """A value the schema does not allow, worded for the person."""


class DatabaseSchema:
    def __init__(self, database: Dict[str, Any]):
        self.database_id = str(database.get("id") or "")
        self.title = Text.rich(database.get("title"))
        self.url = str(database.get("url") or "")
        self.properties: Dict[str, Dict[str, Any]] = {
            str(name): prop for name, prop in (database.get("properties") or {}).items()
            if isinstance(prop, dict)}

    # -- reading -------------------------------------------------------
    def type_of(self, name: str) -> str:
        return str(self.properties[name].get("type") or "")

    def options(self, name: str) -> List[str]:
        prop = self.properties.get(name) or {}
        kind = str(prop.get("type") or "")
        if kind not in OPTION_TYPES:
            return []
        return [str(o.get("name") or "") for o in (prop.get(kind) or {}).get("options") or []]

    def title_property(self) -> str:
        return next((n for n in self.properties if self.type_of(n) == "title"), "")

    def rows(self) -> List[Dict[str, Any]]:
        out = []
        for name in self.properties:
            row: Dict[str, Any] = {"name": name, "type": self.type_of(name)}
            if self.type_of(name) in OPTION_TYPES:
                row["options"] = self.options(name)
            out.append(row)
        return out

    def _known(self, name: str) -> str:
        if name in self.properties:
            return name
        # A case slip is forgiven only when it names exactly one property.
        folded = [n for n in self.properties if n.lower() == str(name).lower()]
        if len(folded) == 1:
            return folded[0]
        raise SchemaProblem(
            f"The database '{self.title}' has no property '{name}'. Its properties "
            f"are: {', '.join(self.properties)}.")

    def _option(self, name: str, value: Any) -> str:
        wanted = str(value).strip()
        options = self.options(name)
        match = next((o for o in options if o == wanted), None) or next(
            (o for o in options if o.lower() == wanted.lower()), None)
        if match is None:
            raise SchemaProblem(
                f"'{wanted}' is not an option of '{name}'. The options are: "
                f"{', '.join(options) or '(none defined)'}.")
        return match

    # -- writing -------------------------------------------------------
    def build(self, values: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Values by property name as Notion's property payload, and the
        same values as text for the record. Raises SchemaProblem."""
        payload, shown = {}, {}
        for raw_name, value in values.items():
            name = self._known(raw_name)
            payload[name] = self._value(name, value)
            shown[name] = Text.clip(self._shown(value), 500)
        return payload, shown

    @staticmethod
    def _shown(value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return "" if value is None else str(value)

    def _value(self, name: str, value: Any) -> Dict[str, Any]:
        kind = self.type_of(name)
        if kind in COMPUTED:
            raise SchemaProblem(f"'{name}' is a {kind} property that Notion fills in "
                                f"itself; it cannot be written.")
        if value is None:
            return self._clear(name, kind)
        if kind in ("title", "rich_text"):
            return {kind: Text.write(str(value))}
        if kind in ("url", "email", "phone_number"):
            return {kind: str(value)}
        if kind == "number":
            if isinstance(value, bool):
                raise SchemaProblem(f"'{name}' is a number, not yes/no.")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise SchemaProblem(f"'{name}' is a number; '{value}' is not one.")
            return {"number": int(number) if number.is_integer() else number}
        if kind == "checkbox":
            if isinstance(value, bool):
                return {"checkbox": value}
            if str(value).strip().lower() in ("true", "yes"):
                return {"checkbox": True}
            if str(value).strip().lower() in ("false", "no"):
                return {"checkbox": False}
            raise SchemaProblem(f"'{name}' is a checkbox; give true or false.")
        if kind in ("select", "status"):
            return {kind: {"name": self._option(name, value)}}
        if kind == "multi_select":
            items = value if isinstance(value, list) else [
                part for part in str(value).split(",") if part.strip()]
            return {"multi_select": [{"name": self._option(name, v)} for v in items]}
        if kind == "date":
            start = str(value).strip()
            if not DATE.match(start):
                raise SchemaProblem(f"'{name}' is a date; give YYYY-MM-DD or an ISO "
                                    f"8601 date-time, not '{start}'.")
            return {"date": {"start": start}}
        if kind == "relation":
            ids = value if isinstance(value, list) else [value]
            return {"relation": [{"id": str(i).strip()} for i in ids if str(i).strip()]}
        raise SchemaProblem(f"'{name}' is a {kind} property, which this agent does "
                            f"not write. Ask the person to set it in Notion.")

    def _clear(self, name: str, kind: str) -> Dict[str, Any]:
        if kind in ("title", "rich_text", "multi_select", "relation"):
            return {kind: []}
        if kind in ("number", "select", "date", "url", "email", "phone_number"):
            return {kind: None}
        raise SchemaProblem(f"'{name}' cannot be emptied here.")

    # -- asking ----------------------------------------------------------
    def filter(self, name: str, operator: str, value: Any) -> Dict[str, Any]:
        """One simple condition as a Notion filter. Raises SchemaProblem."""
        name = self._known(name)
        kind = self.type_of(name)
        text = "" if value is None else str(value).strip()
        if operator == "checkbox":
            if kind != "checkbox":
                raise SchemaProblem(f"'{name}' is a {kind} property, not a checkbox.")
            return {"property": name, "checkbox": {"equals": self._value(name, value)["checkbox"]}}
        if operator == "on_or_after":
            if kind not in ("date", "created_time", "last_edited_time"):
                raise SchemaProblem(f"'{name}' is a {kind} property; on_or_after needs a date.")
            if not DATE.match(text):
                raise SchemaProblem(f"on_or_after takes YYYY-MM-DD or an ISO 8601 "
                                    f"date-time, not '{text}'.")
            return {"property": name, kind: {"on_or_after": text}}
        if operator == "contains":
            if kind in TEXT_TYPES:
                return {"property": name, kind: {"contains": text}}
            if kind == "multi_select":
                return {"property": name, kind: {"contains": self._option(name, text)}}
            if kind in ("people", "relation"):
                return {"property": name, kind: {"contains": text}}
            raise SchemaProblem(f"'{name}' is a {kind} property; contains works on text, "
                                f"multi-select, people and relation properties.")
        if operator == "equals":
            if kind in TEXT_TYPES:
                return {"property": name, kind: {"equals": text}}
            if kind in ("select", "status"):
                return {"property": name, kind: {"equals": self._option(name, text)}}
            if kind == "multi_select":
                return {"property": name, kind: {"contains": self._option(name, text)}}
            if kind == "number":
                return {"property": name, "number": {"equals": self._value(name, value)["number"]}}
            if kind == "checkbox":
                return {"property": name, "checkbox": {"equals": self._value(name, value)["checkbox"]}}
            if kind == "date":
                if not DATE.match(text):
                    raise SchemaProblem(f"'{name}' is a date; give YYYY-MM-DD.")
                return {"property": name, "date": {"equals": text}}
            raise SchemaProblem(f"'{name}' is a {kind} property, which equals cannot test.")
        raise SchemaProblem(f"Unknown filter operator '{operator}'.")

    def sort(self, name: str, direction: str) -> Dict[str, Any]:
        direction = "ascending" if direction == "ascending" else "descending"
        if name in ("created_time", "last_edited_time") and name not in self.properties:
            return {"timestamp": name, "direction": direction}
        return {"property": self._known(name), "direction": direction}


class PageRow:
    """A page, or a database row, as one result row."""

    @staticmethod
    def parent(obj: Dict[str, Any]) -> str:
        parent = obj.get("parent") or {}
        kind = str(parent.get("type") or "")
        if kind == "workspace":
            return "workspace"
        if kind in ("page_id", "database_id", "block_id"):
            return f"{kind[:-3]} {parent.get(kind)}"
        return kind

    @staticmethod
    def parent_id(obj: Dict[str, Any]) -> str:
        parent = obj.get("parent") or {}
        kind = str(parent.get("type") or "")
        return str(parent.get(kind) or "") if kind in ("page_id", "database_id", "block_id") else ""

    @classmethod
    def row(cls, page: Dict[str, Any], with_properties: bool = True) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "page_id": str(page.get("id") or ""),
            "title": Text.clip(PropertyText.title(page), 300),
            "url": str(page.get("url") or ""),
            "created": str(page.get("created_time") or ""),
            "last_edited": str(page.get("last_edited_time") or ""),
        }
        if with_properties:
            row["properties"] = PropertyText.all(page)
        return row
