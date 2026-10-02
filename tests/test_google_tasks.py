"""The Google Tasks agent, run the way production runs it: in its own
worker over the real wire, against a loopback Google Tasks API holding
fictional tasks for Sidra Office Supplies. The same cases as the
Microsoft To Do agent's suite: the contracts are the same, the wire is
not.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gtasks_stub import TasksStub


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def gtasks():
    stub = TasksStub().start()
    stub.default = stub.add_list("My Tasks")
    stub.office = stub.add_list("Office move")
    stub.quote = stub.add_task(stub.default, "Send the chair quotation to Dana",
                               due="2026-09-17",
                               notes="Quotation Q-2041 from Northlight, 20 chairs.")
    stub.clock = "2026-09-10T08:01:00"
    stub.pdf = stub.add_task(stub.default, "Attach the PDF", parent=stub.quote)
    stub.clock = "2026-09-10T08:05:00"
    stub.desks = stub.add_task(stub.office, "Order desks for 20 people", due="2026-10-01")
    stub.clock = "2026-09-10T08:10:00"
    stub.done = stub.add_task(stub.default, "Book the movers", status="completed")
    stub.clock = "2026-09-10T09:00:00"
    yield stub
    stub.stop()


def executor(gtasks, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"google_tasks__google": gtasks.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["google_tasks"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_tasks"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_the_secret_has_the_same_shape_as_the_gmail_agents(self, agents):
        """One saved Google credential must be grantable to both."""
        mail = agents["gmail"].manifest.resource("secrets", "google")["fields"]
        tasks = agents["google_tasks"].manifest.resource("secrets", "google")["fields"]
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        assert strip(mail) == strip(tasks)

    def test_the_levels_are_the_rules(self, agents):
        levels = {name: agents["google_tasks"].manifest.function(
            f"google_tasks.{name}")[1]["permission_level"]
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
            _, function = agents["google_tasks"].manifest.function(f"google_tasks.{name}")
            assert function["schedulable"] is True and not function.get("llm")

    def test_the_account_reports_its_address(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.account.status", {})
        assert (status, result) == ("success", {"connected": True,
                                                "email": TasksStub.ACCOUNT})


class TestReading:
    def test_lists_are_listed(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.lists.list", {})
        assert status == "success", result
        assert [(r["list_id"], r["name"]) for r in result["lists"]] == [
            (gtasks.default, "My Tasks"), (gtasks.office, "Office move")]
        assert result["more"] is False
        capped, _ = invoke(agents, ex, "google_tasks.lists.list", {"max_results": 1})
        assert len(capped["lists"]) == 1 and capped["more"] is True

    def test_the_default_list_is_open_tasks_newest_change_first(self, agents, gtasks):
        gtasks.add_task(gtasks.default, "Confirm the delivery address", due="2026-09-30")
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.tasks.list", {})
        assert status == "success", result
        assert result["list_id"] == gtasks.default          # the real id, not "@default"
        assert [t["title"] for t in result["tasks"]] == [
            "Confirm the delivery address", "Attach the PDF",
            "Send the chair quotation to Dana"]
        subtask, quote = result["tasks"][1], result["tasks"][2]
        assert subtask["parent"] == gtasks.quote
        assert (quote["task_id"], quote["due"], quote["status"]) == (
            gtasks.quote, "2026-09-17", "needsAction")
        assert quote["note"].startswith("Quotation Q-2041")
        assert quote["link"].endswith(gtasks.quote)

        # Ticked off in Google's app, so hidden as well as completed.
        done, _ = invoke(agents, ex, "google_tasks.tasks.list", {"status": "completed"})
        assert [t["title"] for t in done["tasks"]] == ["Book the movers"]
        every, _ = invoke(agents, ex, "google_tasks.tasks.list", {"status": "all"})
        assert len(every["tasks"]) == 4
        soon, _ = invoke(agents, ex, "google_tasks.tasks.list", {"due_before": "2026-09-20"})
        assert [t["task_id"] for t in soon["tasks"]] == [gtasks.quote]

    def test_a_long_list_is_read_past_googles_pages_and_pages_itself(self, agents, gtasks):
        for i in range(3):
            gtasks.clock = f"2026-09-10T09:0{i}:00"
            gtasks.add_task(gtasks.office, f"Label box {i}")
        ex, _ = executor(gtasks)
        first, _ = invoke(agents, ex, "google_tasks.tasks.list",
                          {"list_id": gtasks.office, "max_results": 2})
        assert [t["title"] for t in first["tasks"]] == ["Label box 2", "Label box 1"]
        assert first["next_page_token"] == "2"
        second, _ = invoke(agents, ex, "google_tasks.tasks.list",
                           {"list_id": gtasks.office, "max_results": 2,
                            "page_token": first["next_page_token"]})
        assert [t["title"] for t in second["tasks"]] == [
            "Label box 0", "Order desks for 20 people"]
        assert "next_page_token" not in second

    def test_a_foreign_page_token_is_refused(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.tasks.list",
                                {"page_token": "CgwI-other"})
        assert status == "error" and result["kind"] == "invalid"

    def test_a_task_is_read_whole_with_its_subtasks(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.tasks.get",
                                {"list_id": gtasks.default, "task_id": gtasks.quote})
        assert status == "success", result
        assert result["note"] == "Quotation Q-2041 from Northlight, 20 chairs."
        assert result["note_truncated"] is False
        assert result["subtasks"] == [
            {"task_id": gtasks.pdf, "title": "Attach the PDF", "status": "needsAction"}]

    def test_an_unknown_task_is_named_not_invented(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.tasks.get",
                                {"list_id": gtasks.default, "task_id": "dGFzay-nope"})
        assert status == "error" and result["kind"] == "not_found"


class TestChanging:
    def test_create_update_complete_reopen(self, agents, gtasks):
        ex, provider = executor(gtasks)
        created, status = invoke(agents, ex, "google_tasks.tasks.create", {
            "list_id": gtasks.office, "title": "Measure the meeting room",
            "due": "2026-09-18", "note": "Both walls."})
        assert status == "success", created
        stored = gtasks.task(created["task_id"])
        assert stored["due"] == "2026-09-18T00:00:00.000Z"
        assert stored["notes"] == "Both walls."
        assert (created["due"], created["status"]) == ("2026-09-18", "needsAction")

        updated, status = invoke(agents, ex, "google_tasks.tasks.update", {
            "list_id": gtasks.office, "task_id": created["task_id"],
            "title": "Measure both meeting rooms", "due": ""})
        assert status == "success", updated
        assert updated["title"] == "Measure both meeting rooms" and updated["due"] == ""
        assert "due" not in gtasks.task(created["task_id"])
        assert gtasks.task(created["task_id"])["notes"] == "Both walls."

        done, status = invoke(agents, ex, "google_tasks.tasks.complete", {
            "list_id": gtasks.office, "task_id": created["task_id"]})
        assert status == "success", done
        assert (done["status"], done["completed"]) == ("completed", "2026-09-10T09:00:00Z")

        again, status = invoke(agents, ex, "google_tasks.tasks.reopen", {
            "list_id": gtasks.office, "task_id": created["task_id"]})
        assert status == "success", again
        assert (again["status"], again["completed"]) == ("needsAction", "")
        assert "completed" not in gtasks.task(created["task_id"])
        assert provider.data == {}          # nothing kept on the platform

    def test_a_subtask_names_its_parent_and_lands_in_the_default_list(self, agents, gtasks):
        ex, _ = executor(gtasks)
        created, status = invoke(agents, ex, "google_tasks.tasks.create",
                                 {"title": "Check the delivery date", "parent": gtasks.quote})
        assert status == "success", created
        assert (created["list_id"], created["parent"]) == (gtasks.default, gtasks.quote)

    def test_bad_dates_are_refused_and_nothing_is_written(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.tasks.create",
                                {"title": "x", "due": "2026-02-30"})
        assert status == "error" and result["kind"] == "invalid"
        assert gtasks.writes == []

    def test_a_list_is_created(self, agents, gtasks):
        ex, _ = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.lists.create", {"name": "Showroom"})
        assert status == "success", result
        assert gtasks.lists[result["list_id"]]["title"] == "Showroom"

    def test_deleting_needs_the_chats_trust(self, agents, gtasks):
        ex, _ = executor(gtasks)
        address = {"list_id": gtasks.default, "task_id": gtasks.quote}
        refused, status = invoke(agents, ex, "google_tasks.tasks.delete", address)
        assert status == "error" and refused.get("denied") is True
        assert gtasks.task(gtasks.quote) is not None

        deleted, status = invoke(agents, ex, "google_tasks.tasks.delete", address,
                                 chat_level=3)
        assert status == "success", deleted
        assert deleted == {"deleted": True, "task_id": gtasks.quote,
                           "title": "Send the chair quotation to Dana"}
        assert gtasks.task(gtasks.quote) is None and gtasks.task(gtasks.pdf) is None


class TestWatching:
    def test_a_completion_is_handed_on_exactly_once(self, agents, gtasks):
        ex, provider = executor(gtasks)
        watch, status = invoke(agents, ex, "google_tasks.sync.watch", {"note": "mirror"})
        assert status == "success", watch
        # From now, in Google's clock: the newest change marks the spot.
        assert watch["since"] == "2026-09-10T08:10:00Z"
        assert watch["list_name"] == "(every list)"

        quiet, status = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "completed": [], "more": False}

        # An edit is not a completion; a tick in Google's own app is, even
        # though the app hides the task.
        gtasks.edit_in_app(gtasks.desks, title="Order desks for 22 people")
        gtasks.complete_in_app(gtasks.quote)
        news, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert [r["task_id"] for r in news["completed"]] == [gtasks.quote]
        row = news["completed"][0]
        assert (row["list_name"], row["list_id"], row["watch_ref"]) == (
            "My Tasks", gtasks.default, watch["watch_ref"])
        assert (row["status"], row["completed"]) == ("completed", "2026-09-10T09:00:00Z")
        again, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert again["completed"] == []

        # A second task ticked off in the very same second is neither lost
        # nor shown twice.
        gtasks.complete_in_app(gtasks.desks)
        news, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert [r["task_id"] for r in news["completed"]] == [gtasks.desks]
        assert news["completed"][0]["list_name"] == "Office move"
        again, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert again["completed"] == []

        # Editing a task already ticked off does not report it again.
        gtasks.clock = "2026-09-10T09:05:00"
        gtasks.edit_in_app(gtasks.quote, notes="Sent on Thursday.")
        again, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert again["completed"] == []
        keys = next(iter(provider.data["google_tasks__watch"].values()))["keys"]
        assert keys["completed_since"] == "2026-09-10T09:00:00Z"
        assert set(keys["completed_ids"].split(",")) == {gtasks.quote, gtasks.desks}

    def test_the_changed_watch_has_its_own_cursor(self, agents, gtasks):
        ex, _ = executor(gtasks)
        watch, _ = invoke(agents, ex, "google_tasks.sync.watch", {"list_id": gtasks.office})
        assert (watch["list_name"], watch["since"]) == ("Office move", "2026-09-10T08:05:00Z")

        gtasks.edit_in_app(gtasks.desks, title="Order desks for 22 people")
        gtasks.add_task(gtasks.default, "Not watched: another list")
        created = gtasks.add_task(gtasks.office, "Collect keys")
        changed, status = invoke(agents, ex, "google_tasks.sync.changed",
                                 {"watch_ref": watch["watch_ref"]})
        assert status == "success", changed
        assert [r["task_id"] for r in changed["changed"]] == [gtasks.desks, created]
        assert changed["changed"][0]["title"] == "Order desks for 22 people"
        again, _ = invoke(agents, ex, "google_tasks.sync.changed", {})
        assert again["changed"] == []

        # The completion cursor did not move with it.
        gtasks.clock = "2026-09-10T09:30:00"
        done, status = invoke(agents, ex, "google_tasks.tasks.complete",
                              {"list_id": gtasks.office, "task_id": created})
        assert status == "success", done
        completed, _ = invoke(agents, ex, "google_tasks.sync.completed", {})
        assert [r["task_id"] for r in completed["completed"]] == [created]
        changed, _ = invoke(agents, ex, "google_tasks.sync.changed", {})
        assert [(r["task_id"], r["status"]) for r in changed["changed"]] == [
            (created, "completed")]

    def test_a_watch_pages_and_says_there_is_more(self, agents, gtasks):
        ex, _ = executor(gtasks)
        watch, _ = invoke(agents, ex, "google_tasks.sync.watch", {})
        for i in range(3):
            gtasks.clock = f"2026-09-10T09:0{i}:00"
            gtasks.add_task(gtasks.office if i % 2 else gtasks.default, f"Box {i}")
        page, _ = invoke(agents, ex, "google_tasks.sync.changed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["title"] for r in page["changed"]] == ["Box 0", "Box 1"]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "google_tasks.sync.changed",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["title"] for r in page["changed"]] == ["Box 2"]
        assert page["more"] is False

    def test_a_bad_since_is_refused_and_records_nothing(self, agents, gtasks):
        ex, provider = executor(gtasks)
        result, status = invoke(agents, ex, "google_tasks.sync.watch", {"since": "yesterday"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, gtasks):
        ex, provider = executor(gtasks, access_token="expired")
        result, status = invoke(agents, ex, "google_tasks.tasks.list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "google_tasks.tasks.create",
                                {"list_id": gtasks.default, "title": "x"})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "google_tasks.sync.watch", {})
        assert status == "error" and result["kind"] == "auth"
        assert gtasks.writes == []
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "google_tasks.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Google account" in result["problem"]
