"""The Todoist agent in a real worker against a loopback Todoist holding
a fictional task list for Sidra Office Supplies: an Inbox, a shared
"Office Move" project with Furniture and IT sections, and a private
"Suppliers" project. Due dates are relative to today, because Todoist's
"today" and "overdue" are.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.todoist_stub import TodoistStub

TODAY = date.today()


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def todoist():
    stub = TodoistStub().start()
    stub.inbox = stub.add_project("Inbox", inbox=True)
    stub.move = stub.add_project("Office Move", shared=True)
    stub.suppliers = stub.add_project("Suppliers")
    stub.furniture = stub.add_section(stub.move, "Furniture")
    stub.add_section(stub.move, "IT")
    stub.add_label("calls")
    stub.add_label("errands")
    stub.chairs = stub.add_task("Order twenty desk chairs", stub.move, section_id=stub.furniture,
                                labels=["calls"], priority=4, due_date=TODAY.isoformat(),
                                description="Northlight quote Q-2041")
    stub.quote = stub.add_task("Chase the Northlight quote", stub.suppliers, labels=["calls"],
                               priority=2, due_date=(TODAY - timedelta(days=2)).isoformat())
    stub.keys = stub.add_task("Collect keys for the new office", stub.move,
                              due_date=(TODAY + timedelta(days=5)).isoformat())
    stub.review = stub.add_task("Weekly supplier review", stub.suppliers,
                                due_date=(TODAY + timedelta(days=1)).isoformat(),
                                due_string="every Monday", recurring=True)
    yield stub
    stub.stop()


def executor(todoist, access_token="tk-1"):
    provider = InMemoryResourceProvider(secrets={"todoist__todoist": todoist.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["todoist"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["todoist"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_the_levels_are_what_the_actions_cost(self, agents):
        doc = agents["todoist"].manifest.document
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in doc["tools"] for f in t["functions"]}
        assert levels == {
            "account.status": 0, "projects.list": 0, "projects.create": 1,
            "sections.list": 0, "labels.list": 0,
            "tasks.list": 0, "tasks.get": 0, "tasks.create": 1, "tasks.update": 1,
            "tasks.complete": 1, "tasks.reopen": 1, "tasks.delete": 3,
            "comments.add": 3, "sync.watch": 1, "sync.completed": 1}
        _, completed = agents["todoist"].manifest.function("todoist.sync.completed")
        assert completed["schedulable"] is True and not completed.get("llm")

    def test_the_secret_signs_in_with_commas_between_scopes(self, agents):
        secret = agents["todoist"].manifest.resource("secrets", "todoist")
        assert secret["oauth"]["provider"] == "todoist"
        assert secret["oauth"]["scope_separator"] == ","
        assert secret["oauth"]["scopes"] == ["data:read_write", "data:delete"]

    def test_status_reports_the_account(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.account.status", {})
        assert status == "success"
        assert result == {"connected": True, "email": TodoistStub.ACCOUNT,
                          "full_name": "Sam Haddad", "timezone": "Asia/Dubai"}


class TestReading:
    def test_open_tasks_come_with_app_priorities_projects_and_links(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.list", {})
        assert status == "success", result
        rows = {r["content"]: r for r in result["tasks"]}
        assert len(rows) == 4 and result["more"] is False
        chairs = rows["Order twenty desk chairs"]
        # API priority 4 is what the app shows as P1.
        assert chairs["priority"] == "P1"
        assert chairs["project"] == "Office Move" and chairs["labels"] == ["calls"]
        assert chairs["due_date"] == TODAY.isoformat()
        assert chairs["link"] == f"https://app.todoist.com/app/task/{todoist.chairs}"
        assert rows["Weekly supplier review"]["recurring"] is True
        assert rows["Collect keys for the new office"]["priority"] == "P4"

    def test_by_project_section_and_label_names(self, agents, todoist):
        ex, _ = executor(todoist)
        result, _ = invoke(agents, ex, "todoist.tasks.list",
                           {"project": "office move", "section": "Furniture"})
        assert [r["task_id"] for r in result["tasks"]] == [todoist.chairs]
        result, _ = invoke(agents, ex, "todoist.tasks.list", {"label": "Calls"})
        assert {r["task_id"] for r in result["tasks"]} == {todoist.chairs, todoist.quote}

    def test_a_filter_query_is_todoists_own(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.list", {"query": "today | overdue"})
        assert status == "success", result
        assert {r["task_id"] for r in result["tasks"]} == {todoist.chairs, todoist.quote}
        refused, status = invoke(agents, ex, "todoist.tasks.list",
                                 {"query": "today", "project": "Suppliers"})
        assert status == "error" and refused["kind"] == "invalid"
        bad, status = invoke(agents, ex, "todoist.tasks.list", {"query": "whenever-ish"})
        assert status == "error" and bad["kind"] == "http"

    def test_a_long_list_is_paged_by_cursor(self, agents, todoist):
        ex, _ = executor(todoist)
        first, _ = invoke(agents, ex, "todoist.tasks.list", {"max_results": 3})
        assert len(first["tasks"]) == 3 and first["more"] is True
        rest, _ = invoke(agents, ex, "todoist.tasks.list",
                         {"max_results": 3, "cursor": first["next_cursor"]})
        assert len(rest["tasks"]) == 1 and rest["more"] is False

    def test_an_unknown_project_is_refused_with_the_real_names(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.list", {"project": "Warehouse"})
        assert status == "error" and result["kind"] == "invalid"
        assert result["known"] == ["Inbox", "Office Move", "Suppliers"]
        assert "Office Move" in result["error"]

    def test_get_has_the_description_and_comment_count(self, agents, todoist):
        ex, _ = executor(todoist)
        invoke(agents, ex, "todoist.comments.add",
               {"task_id": todoist.chairs, "content": "Asked for a sample."}, chat_level=3)
        result, status = invoke(agents, ex, "todoist.tasks.get", {"task_id": todoist.chairs})
        assert status == "success", result
        assert result["description"] == "Northlight quote Q-2041"
        assert result["comment_count"] == 1 and result["completed"] is False
        missing, status = invoke(agents, ex, "todoist.tasks.get", {"task_id": "6X9999"})
        assert status == "error" and missing["kind"] == "not_found"

    def test_projects_sections_and_labels(self, agents, todoist):
        ex, _ = executor(todoist)
        projects, _ = invoke(agents, ex, "todoist.projects.list", {})
        move = next(p for p in projects["projects"] if p["name"] == "Office Move")
        assert move["shared"] is True and move["inbox"] is False
        sections, _ = invoke(agents, ex, "todoist.sections.list", {"project": "Office Move"})
        assert [s["name"] for s in sections["sections"]] == ["Furniture", "IT"]
        assert sections["sections"][0]["project"] == "Office Move"
        labels, _ = invoke(agents, ex, "todoist.labels.list", {})
        assert [l["name"] for l in labels["labels"]] == ["calls", "errands"]


class TestWriting:
    def test_create_refuses_an_unknown_project_or_label_and_sends_nothing(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.create",
                                {"content": "Book movers", "project": "Warehouse"})
        assert status == "error" and result["kind"] == "invalid"
        assert result["known"] == ["Inbox", "Office Move", "Suppliers"]
        result, status = invoke(agents, ex, "todoist.tasks.create",
                                {"content": "Book movers", "labels": ["urgent"]})
        assert status == "error" and result["known"] == ["calls", "errands"]
        assert todoist.writes == []

    def test_create_files_it_where_the_person_said(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.create", {
            "content": "Order standing desks", "project": "Office Move",
            "section": "furniture", "due_string": "tomorrow", "priority": "P1",
            "labels": ["CALLS"], "description": "Twenty, oak."})
        assert status == "success", result
        _, path, body = todoist.writes[-1]
        assert path == "tasks"
        assert body == {"content": "Order standing desks", "description": "Twenty, oak.",
                        "due_string": "tomorrow", "priority": 4, "labels": ["calls"],
                        "project_id": todoist.move, "section_id": todoist.furniture}
        assert result["priority"] == "P1" and result["project"] == "Office Move"
        assert result["due_date"] == (TODAY + timedelta(days=1)).isoformat()
        assert todoist.tasks[result["task_id"]]["section_id"] == todoist.furniture

    def test_a_bad_due_date_is_refused_before_anything_is_sent(self, agents, todoist):
        ex, _ = executor(todoist)
        result, status = invoke(agents, ex, "todoist.tasks.create",
                                {"content": "Book movers", "due_date": "next week"})
        assert status == "error" and "YYYY-MM-DD" in result["error"]
        assert todoist.writes == []

    def test_update_complete_reopen_and_delete(self, agents, todoist):
        ex, _ = executor(todoist)
        updated, status = invoke(agents, ex, "todoist.tasks.update", {
            "task_id": todoist.keys, "due_date": "2026-10-01", "priority": "P2"})
        assert status == "success", updated
        assert updated["due_date"] == "2026-10-01" and updated["priority"] == "P2"
        assert todoist.tasks[todoist.keys]["priority"] == 3

        done, status = invoke(agents, ex, "todoist.tasks.complete", {"task_id": todoist.keys})
        assert status == "success", done
        assert done == {"task_id": todoist.keys, "content": "Collect keys for the new office",
                        "completed": True, "already": False, "recurring": False}
        assert todoist.tasks[todoist.keys]["checked"] is True
        again, _ = invoke(agents, ex, "todoist.tasks.complete", {"task_id": todoist.keys})
        assert again["already"] is True

        reopened, status = invoke(agents, ex, "todoist.tasks.reopen", {"task_id": todoist.keys})
        assert status == "success" and reopened["reopened"] is True
        assert todoist.tasks[todoist.keys]["checked"] is False

        refused, status = invoke(agents, ex, "todoist.tasks.delete", {"task_id": todoist.keys})
        assert status == "error" and refused.get("denied") is True
        assert todoist.tasks[todoist.keys]["is_deleted"] is False
        gone, status = invoke(agents, ex, "todoist.tasks.delete",
                              {"task_id": todoist.keys}, chat_level=3)
        assert status == "success" and gone["deleted"] is True
        assert todoist.tasks[todoist.keys]["is_deleted"] is True

    def test_a_recurring_task_says_it_recurs(self, agents, todoist):
        ex, _ = executor(todoist)
        done, _ = invoke(agents, ex, "todoist.tasks.complete", {"task_id": todoist.review})
        assert done["recurring"] is True

    def test_a_comment_needs_the_chats_trust(self, agents, todoist):
        ex, _ = executor(todoist)
        refused, status = invoke(agents, ex, "todoist.comments.add",
                                 {"task_id": todoist.chairs, "content": "Ordered."})
        assert status == "error" and refused.get("denied") is True
        assert todoist.comments == {}
        posted, status = invoke(agents, ex, "todoist.comments.add",
                                {"task_id": todoist.chairs, "content": "Ordered."}, chat_level=3)
        assert status == "success", posted
        assert posted["task"] == "Order twenty desk chairs"
        assert todoist.comments[posted["comment_id"]]["content"] == "Ordered."

    def test_a_project_is_created_under_a_real_parent(self, agents, todoist):
        ex, _ = executor(todoist)
        made, status = invoke(agents, ex, "todoist.projects.create",
                              {"name": "Reception", "parent": "Office Move"})
        assert status == "success", made
        assert made["parent_id"] == todoist.move and made["name"] == "Reception"
        refused, status = invoke(agents, ex, "todoist.projects.create",
                                 {"name": "Reception", "parent": "Warehouse"})
        assert status == "error" and refused["kind"] == "invalid"


class TestWatchingCompletions:
    def test_each_completion_is_handed_on_exactly_once(self, agents, todoist):
        ex, provider = executor(todoist)
        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        # Ticked off before the watch began: marks the spot, is not news.
        earlier = todoist.add_task("Measure the meeting room", todoist.move)
        todoist.complete_at(earlier, base - timedelta(minutes=30))

        watch, status = invoke(agents, ex, "todoist.sync.watch", {"note": "mirror into Tasks"})
        assert status == "success", watch
        assert watch["since"] == todoist.tasks[earlier]["completed_at"]

        quiet, status = invoke(agents, ex, "todoist.sync.completed", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "completed": [], "more": False}

        # Two tasks completed in the very same second, answered by
        # Todoist newest first: both arrive, oldest first, once.
        todoist.complete_at(todoist.quote, base)
        todoist.complete_at(todoist.chairs, base)
        news, _ = invoke(agents, ex, "todoist.sync.completed", {})
        assert sorted(r["task_id"] for r in news["completed"]) == sorted([todoist.quote, todoist.chairs])
        row = next(r for r in news["completed"] if r["task_id"] == todoist.chairs)
        assert row["watch_ref"] == watch["watch_ref"] and row["project"] == "Office Move"
        assert row["completed_at"] == todoist.tasks[todoist.chairs]["completed_at"]
        assert news["more"] is False

        again, _ = invoke(agents, ex, "todoist.sync.completed", {})
        assert again["completed"] == []

        # A third in that same second is neither lost nor shown twice.
        todoist.complete_at(todoist.keys, base)
        news, _ = invoke(agents, ex, "todoist.sync.completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todoist.keys]
        again, _ = invoke(agents, ex, "todoist.sync.completed", {})
        assert again["completed"] == []
        keys = provider.data["todoist__watch"][watch["watch_ref"]]["keys"]
        assert set(keys["cursor_ids"].split(",")) == {todoist.quote, todoist.chairs, todoist.keys}

    def test_a_project_watch_pages_oldest_first(self, agents, todoist):
        ex, _ = executor(todoist)
        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        watch, status = invoke(agents, ex, "todoist.sync.watch", {
            "project": "Suppliers", "since": (base - timedelta(minutes=1)).isoformat()})
        assert status == "success", watch
        assert watch["project"] == "Suppliers"

        todoist.complete_at(todoist.chairs, base)                           # Office Move
        todoist.complete_at(todoist.review, base + timedelta(minutes=5))
        todoist.complete_at(todoist.quote, base + timedelta(minutes=1))
        extra = todoist.add_task("Pay the Northlight deposit", todoist.suppliers)
        todoist.complete_at(extra, base + timedelta(minutes=9))

        page, _ = invoke(agents, ex, "todoist.sync.completed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["task_id"] for r in page["completed"]] == [todoist.quote, todoist.review]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "todoist.sync.completed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["task_id"] for r in page["completed"]] == [extra]
        assert page["more"] is False
        assert all(q.get("project_id") == todoist.suppliers for q in todoist.completed_queries)

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, todoist):
        ex, provider = executor(todoist)
        result, status = invoke(agents, ex, "todoist.sync.watch", {"since": "last week"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, todoist):
        ex, provider = executor(todoist, access_token="revoked")
        result, status = invoke(agents, ex, "todoist.tasks.list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "todoist.tasks.create",
                                {"content": "Book movers", "project": "Office Move"})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "todoist.sync.watch", {})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}
        assert todoist.writes == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "todoist.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Todoist account" in result["problem"]
