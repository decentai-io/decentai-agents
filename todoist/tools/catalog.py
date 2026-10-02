"""Names a person says, turned into the ids Todoist wants — and the
rows the assistant reads back.

A project, section or label is only ever one Todoist already has: a
name that matches nothing is refused with the real names listed, so
the assistant can ask instead of inventing one (Todoist would
otherwise create a label silently, or put the task in the Inbox).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .todoist_api import TodoistClient

#: Todoist's API counts priority upwards (4 is the most urgent); the app
#: shows it the other way round (P1 is the most urgent). The assistant
#: only ever sees and says the app's words.
APP_PRIORITY = {4: "P1", 3: "P2", 2: "P3", 1: "P4"}
API_PRIORITY = {label: number for number, label in APP_PRIORITY.items()}

#: How many real names a refusal lists before it stops.
NAMES_SHOWN = 30

Problem = Optional[Dict[str, Any]]


def invalid(message: str, known: List[str]) -> Dict[str, Any]:
    shown = known[:NAMES_SHOWN]
    listed = ", ".join(shown) if shown else "none"
    return {"error": f"{message} The real ones are: {listed}.",
            "kind": "invalid", "known": shown}


def task_row(task: Dict[str, Any], project_names: Dict[str, str]) -> Dict[str, Any]:
    due = task.get("due") or {}
    task_id = str(task.get("id") or "")
    project_id = str(task.get("project_id") or "")
    return {
        "task_id": task_id,
        "content": str(task.get("content") or ""),
        "project_id": project_id,
        "project": project_names.get(project_id, ""),
        "section_id": str(task.get("section_id") or ""),
        "labels": [str(label) for label in task.get("labels") or []],
        "priority": APP_PRIORITY.get(int(task.get("priority") or 1), "P4"),
        # A timed due has date "2026-09-14T10:00:00"; an all-day one the
        # bare date. Both are Todoist's own, passed on unchanged.
        "due_date": str(due.get("date") or ""),
        "due_string": str(due.get("string") or ""),
        "recurring": bool(due.get("is_recurring")),
        "link": TodoistClient.task_link(task_id),
    }


def project_row(project: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(project.get("id") or "")
    return {
        "project_id": project_id,
        "name": str(project.get("name") or ""),
        "parent_id": str(project.get("parent_id") or ""),
        "shared": bool(project.get("is_shared")),
        "inbox": bool(project.get("inbox_project")),
        "link": TodoistClient.project_link(project_id),
    }


class Catalog:
    """The account's projects, sections and labels, read once per call."""

    def __init__(self, client: TodoistClient):
        self.client = client
        self._projects: Optional[List[Dict[str, Any]]] = None
        self._labels: Optional[List[Dict[str, Any]]] = None

    def projects(self) -> List[Dict[str, Any]]:
        if self._projects is None:
            self._projects = self.client.projects()
        return self._projects

    def project_names(self) -> Dict[str, str]:
        return {str(p.get("id")): str(p.get("name") or "") for p in self.projects()}

    def project(self, wanted: str) -> Tuple[Optional[Dict[str, Any]], Problem]:
        """A project by its id or its name (any case)."""
        wanted = str(wanted or "").strip()
        projects = self.projects()
        for project in projects:
            if str(project.get("id")) == wanted:
                return project, None
        matches = [p for p in projects if str(p.get("name") or "").lower() == wanted.lower()]
        names = [str(p.get("name") or "") for p in projects]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, invalid(f"More than one project is called '{wanted}'; "
                                 f"give its project_id.",
                                 [f"{p.get('name')} ({p.get('id')})" for p in matches])
        return None, invalid(f"Todoist has no project '{wanted}'.", names)

    def section(self, wanted: str, project_id: str = "") -> Tuple[Optional[Dict[str, Any]], Problem]:
        """A section by id or name, inside the project when one is given."""
        wanted = str(wanted or "").strip()
        sections = self.client.sections(project_id)
        for section in sections:
            if str(section.get("id")) == wanted:
                return section, None
        matches = [s for s in sections if str(s.get("name") or "").lower() == wanted.lower()]
        if len(matches) == 1:
            return matches[0], None
        names = self.project_names()
        described = [f"{s.get('name')} ({names.get(str(s.get('project_id')), '')})"
                     for s in (matches or sections)]
        if matches:
            return None, invalid(f"More than one section is called '{wanted}'; "
                                 f"name the project too.", described)
        where = f" in {names.get(project_id, project_id)}" if project_id else ""
        return None, invalid(f"Todoist has no section '{wanted}'{where}.", described)

    def label_names(self, wanted: List[str]) -> Tuple[List[str], Problem]:
        """The real label names for the ones asked for, matched in any case."""
        if self._labels is None:
            self._labels = self.client.labels()
        real = {str(l.get("name") or "").lower(): str(l.get("name") or "")
                for l in self._labels}
        found, missing = [], []
        for name in wanted:
            name = str(name or "").strip()
            if not name:
                continue
            if name.lower() in real:
                found.append(real[name.lower()])
            else:
                missing.append(name)
        if missing:
            return [], invalid("Todoist has no label " + ", ".join(f"'{m}'" for m in missing)
                               + ".", sorted(real.values()))
        return found, None
