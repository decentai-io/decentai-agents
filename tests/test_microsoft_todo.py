"""The Microsoft To Do agent, run the way production runs it: in its own
worker over the real wire, against a loopback Microsoft Graph holding
fictional tasks for Sidra Office Supplies. The same cases as the Google
Tasks agent's suite: the contracts are the same, the wire is not.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_todo_stub import GraphTodoStub


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def todo():
    stub = GraphTodoStub().start()
    stub.add_list("Flagged emails", "flaggedEmails")
    stub.default = stub.add_list("Tasks", "defaultList")
    stub.office = stub.add_list("Office move")
    stub.quote = stub.add_task(stub.default, "Send the chair quotation to Dana",
                               due="2026-09-17", importance="high",
                               note="Quotation Q-2041 from Northlight, 20 chairs.")
    stub.clock = "2026-09-10T08:05:00"
    stub.desks = stub.add_task(stub.office, "Order desks for 20 people", due="2026-10-01")
    stub.clock = "2026-09-10T08:10:00"
    stub.done = stub.add_task(stub.default, "Book the movers", status="completed")
    stub.checklists[stub.quote] = [
        {"id": "ci-1", "displayName": "Check delivery date", "isChecked": True},
        {"id": "ci-2", "displayName": "Attach the PDF", "isChecked": False}]
    stub.clock = "2026-09-10T09:00:00"
    yield stub
    stub.stop()


def executor(todo, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"microsoft_todo__microsoft": todo.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["microsoft_todo"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["microsoft_todo"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_the_secret_has_the_same_shape_as_outlooks(self, agents):
        """One saved Microsoft credential must be grantable to both."""
        mail = agents["outlook"].manifest.resource("secrets", "microsoft")["fields"]
        todo = agents["microsoft_todo"].manifest.resource("secrets", "microsoft")["fields"]
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        assert strip(mail) == strip(todo)

    def test_the_levels_are_the_rules(self, agents):
        levels = {name: agents["microsoft_todo"].manifest.function(
            f"microsoft_todo.{name}")[1]["permission_level"]
            for name in ("account.status", "lists.list", "lists.create", "tasks.list",
                         "tasks.get", "tasks.create", "tasks.update", "tasks.complete",
                         "tasks.reopen", "tasks.delete", "sync.watch",
                         "sync.completed", "sync.changed")}
        assert levels == {"account.status": 0, "lists.list": 0, "lists.create": 1,
                          "tasks.list": 0, "tasks.get": 0, "tasks.create": 1,
                          "tasks.update": 1, "tasks.complete": 1, "tasks.reopen": 1,
                          "tasks.delete": 3, "sync.watch": 1, "sync.completed": 1,
                          "sync.changed": 1}
        for name in ("sync.completed", "sync.changed"):
            _, function = agents["microsoft_todo"].manifest.function(f"microsoft_todo.{name}")
            assert function["schedulable"] is True and not function.get("llm")

    def test_the_account_reports_its_address(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.account.status", {})
        assert (status, result) == ("success", {"connected": True,
                                                "email": GraphTodoStub.ACCOUNT})


class TestReading:
    def test_lists_are_listed_with_their_kind(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.lists.list", {})
        assert status == "success", result
        assert [(r["name"], r["kind"]) for r in result["lists"]] == [
            ("Flagged emails", "flaggedEmails"), ("Tasks", "defaultList"),
            ("Office move", "none")]
        assert result["more"] is False
        capped, _ = invoke(agents, ex, "microsoft_todo.lists.list", {"max_results": 2})
        assert len(capped["lists"]) == 2 and capped["more"] is True

    def test_the_default_list_is_open_tasks_newest_change_first(self, agents, todo):
        todo.add_task(todo.default, "Confirm the delivery address", due="2026-09-30")
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.tasks.list", {})
        assert status == "success", result
        assert result["list_id"] == todo.default
        assert [t["title"] for t in result["tasks"]] == [
            "Confirm the delivery address", "Send the chair quotation to Dana"]
        row = result["tasks"][1]
        assert (row["task_id"], row["due"], row["importance"], row["status"]) == (
            todo.quote, "2026-09-17", "high", "notStarted")
        assert row["note"].startswith("Quotation Q-2041")

        done, _ = invoke(agents, ex, "microsoft_todo.tasks.list", {"status": "completed"})
        assert [t["title"] for t in done["tasks"]] == ["Book the movers"]
        every, _ = invoke(agents, ex, "microsoft_todo.tasks.list", {"status": "all"})
        assert len(every["tasks"]) == 3
        soon, _ = invoke(agents, ex, "microsoft_todo.tasks.list", {"due_before": "2026-09-20"})
        assert [t["task_id"] for t in soon["tasks"]] == [todo.quote]

    def test_a_long_list_pages(self, agents, todo):
        for i in range(3):
            todo.add_task(todo.office, f"Label box {i}")
        ex, _ = executor(todo)
        first, _ = invoke(agents, ex, "microsoft_todo.tasks.list",
                          {"list_id": todo.office, "max_results": 2})
        assert len(first["tasks"]) == 2 and first["next_page_token"]
        second, _ = invoke(agents, ex, "microsoft_todo.tasks.list",
                           {"list_id": todo.office, "max_results": 2,
                            "page_token": first["next_page_token"]})
        ids = {t["task_id"] for t in first["tasks"] + second["tasks"]}
        assert len(ids) == 4 and "next_page_token" not in second

    def test_a_foreign_page_token_is_refused(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.tasks.list",
                                {"page_token": "https://elsewhere.example/steal"})
        assert status == "error" and result["kind"] == "invalid"

    def test_a_task_is_read_whole_with_its_checklist(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.tasks.get",
                                {"list_id": todo.default, "task_id": todo.quote})
        assert status == "success", result
        assert result["note"] == "Quotation Q-2041 from Northlight, 20 chairs."
        assert result["note_truncated"] is False
        assert result["checklist"] == [
            {"item_id": "ci-1", "title": "Check delivery date", "checked": True},
            {"item_id": "ci-2", "title": "Attach the PDF", "checked": False}]

    def test_an_unknown_task_is_named_not_invented(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.tasks.get",
                                {"list_id": todo.default, "task_id": "AAMkAG/nope="})
        assert status == "error" and result["kind"] == "not_found"


class TestChanging:
    def test_create_update_complete_reopen(self, agents, todo):
        ex, provider = executor(todo)
        created, status = invoke(agents, ex, "microsoft_todo.tasks.create", {
            "list_id": todo.office, "title": "Measure the meeting room",
            "due": "2026-09-18", "importance": "high", "note": "Both walls.",
            "reminder": "2026-09-18T09:00:00+04:00"})
        assert status == "success", created
        stored = todo.task(created["task_id"])
        assert stored["dueDateTime"] == {"dateTime": "2026-09-18T00:00:00", "timeZone": "UTC"}
        assert stored["reminderDateTime"] == {"dateTime": "2026-09-18T05:00:00", "timeZone": "UTC"}
        assert stored["isReminderOn"] is True
        assert (created["due"], created["reminder"], created["importance"]) == (
            "2026-09-18", "2026-09-18T05:00:00Z", "high")

        updated, status = invoke(agents, ex, "microsoft_todo.tasks.update", {
            "list_id": todo.office, "task_id": created["task_id"],
            "title": "Measure both meeting rooms", "due": ""})
        assert status == "success", updated
        assert updated["title"] == "Measure both meeting rooms" and updated["due"] == ""
        assert "dueDateTime" not in todo.task(created["task_id"])
        assert todo.task(created["task_id"])["body"]["content"] == "Both walls."

        done, status = invoke(agents, ex, "microsoft_todo.tasks.complete", {
            "list_id": todo.office, "task_id": created["task_id"]})
        assert status == "success", done
        assert (done["status"], done["completed"]) == ("completed", "2026-09-10")

        again, status = invoke(agents, ex, "microsoft_todo.tasks.reopen", {
            "list_id": todo.office, "task_id": created["task_id"]})
        assert status == "success", again
        assert (again["status"], again["completed"]) == ("notStarted", "")
        assert provider.data == {}          # nothing kept on the platform

    def test_a_task_without_a_list_goes_to_the_default_list(self, agents, todo):
        ex, _ = executor(todo)
        created, status = invoke(agents, ex, "microsoft_todo.tasks.create",
                                 {"title": "Water the plants"})
        assert status == "success", created
        assert created["list_id"] == todo.default

    def test_bad_dates_are_refused_and_nothing_is_written(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.tasks.create",
                                {"title": "x", "due": "2026-02-30"})
        assert status == "error" and result["kind"] == "invalid"
        result, status = invoke(agents, ex, "microsoft_todo.tasks.create",
                                {"title": "x", "reminder": "2026-09-18T09:00:00"})
        assert status == "error" and "offset" in result["error"]
        assert todo.writes == []

    def test_a_list_is_created(self, agents, todo):
        ex, _ = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.lists.create",
                                {"name": "Showroom"})
        assert status == "success", result
        assert todo.lists[result["list_id"]]["displayName"] == "Showroom"

    def test_deleting_needs_the_chats_trust(self, agents, todo):
        ex, _ = executor(todo)
        address = {"list_id": todo.office, "task_id": todo.desks}
        refused, status = invoke(agents, ex, "microsoft_todo.tasks.delete", address)
        assert status == "error" and refused.get("denied") is True
        assert todo.task(todo.desks) is not None

        deleted, status = invoke(agents, ex, "microsoft_todo.tasks.delete", address,
                                 chat_level=3)
        assert status == "success", deleted
        assert deleted == {"deleted": True, "task_id": todo.desks,
                           "title": "Order desks for 20 people"}
        assert todo.task(todo.desks) is None


class TestWatching:
    def test_a_completion_is_handed_on_exactly_once(self, agents, todo):
        ex, provider = executor(todo)
        watch, status = invoke(agents, ex, "microsoft_todo.sync.watch", {"note": "mirror"})
        assert status == "success", watch
        # From now, in Graph's clock: the newest change marks the spot.
        assert watch["since"] == "2026-09-10T08:10:00Z"
        assert watch["list_name"] == "(every list)"

        quiet, status = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "completed": [], "more": False}

        # An edit is not a completion; a tick is.
        todo.edit_in_app(todo.desks, title="Order desks for 22 people")
        todo.complete_in_app(todo.quote)
        news, _ = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todo.quote]
        row = news["completed"][0]
        assert (row["list_name"], row["list_id"], row["watch_ref"]) == (
            "Tasks", todo.default, watch["watch_ref"])
        assert (row["status"], row["completed"]) == ("completed", "2026-09-10")
        again, _ = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert again["completed"] == []

        # A second task ticked off in the very same second is neither lost
        # nor shown twice.
        todo.complete_in_app(todo.desks)
        news, _ = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert [r["task_id"] for r in news["completed"]] == [todo.desks]
        assert news["completed"][0]["list_name"] == "Office move"
        again, _ = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert again["completed"] == []
        keys = next(iter(provider.data["microsoft_todo__watch"].values()))["keys"]
        assert keys["completed_since"] == "2026-09-10T09:00:00Z"
        assert set(keys["completed_ids"].split(",")) == {todo.quote, todo.desks}

    def test_the_changed_watch_has_its_own_cursor(self, agents, todo):
        ex, _ = executor(todo)
        watch, _ = invoke(agents, ex, "microsoft_todo.sync.watch", {"list_id": todo.default})
        assert watch["list_name"] == "Tasks"

        todo.edit_in_app(todo.quote, title="Send the revised quotation to Dana")
        todo.add_task(todo.office, "Not watched: another list")
        created = todo.add_task(todo.default, "Collect keys")
        changed, status = invoke(agents, ex, "microsoft_todo.sync.changed",
                                 {"watch_ref": watch["watch_ref"]})
        assert status == "success", changed
        assert [r["task_id"] for r in changed["changed"]] == [todo.quote, created]
        assert changed["changed"][0]["title"] == "Send the revised quotation to Dana"
        again, _ = invoke(agents, ex, "microsoft_todo.sync.changed", {})
        assert again["changed"] == []

        # The completion cursor did not move with it.
        todo.clock = "2026-09-10T09:30:00"
        todo.complete_in_app(created)
        done, _ = invoke(agents, ex, "microsoft_todo.sync.completed", {})
        assert [r["task_id"] for r in done["completed"]] == [created]
        changed, _ = invoke(agents, ex, "microsoft_todo.sync.changed", {})
        assert [(r["task_id"], r["status"]) for r in changed["changed"]] == [
            (created, "completed")]

    def test_a_watch_pages_and_says_there_is_more(self, agents, todo):
        ex, _ = executor(todo)
        watch, _ = invoke(agents, ex, "microsoft_todo.sync.watch", {})
        for i in range(3):
            todo.clock = f"2026-09-10T09:0{i}:00"
            todo.add_task(todo.office if i % 2 else todo.default, f"Box {i}")
        page, _ = invoke(agents, ex, "microsoft_todo.sync.changed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["title"] for r in page["changed"]] == ["Box 0", "Box 1"]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "microsoft_todo.sync.changed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["title"] for r in page["changed"]] == ["Box 2"]
        assert page["more"] is False

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, todo):
        ex, provider = executor(todo)
        result, status = invoke(agents, ex, "microsoft_todo.sync.watch", {"since": "yesterday"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, todo):
        ex, provider = executor(todo, access_token="expired")
        result, status = invoke(agents, ex, "microsoft_todo.tasks.list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "microsoft_todo.tasks.create",
                                {"list_id": todo.default, "title": "x"})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "microsoft_todo.sync.watch", {})
        assert status == "error" and result["kind"] == "auth"
        assert todo.writes == []
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "microsoft_todo.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Microsoft account" in result["problem"]
