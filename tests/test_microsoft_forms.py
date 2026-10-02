"""The Microsoft Forms agent in a real worker against a loopback
Microsoft Graph holding Sidra Office Supplies' workbooks: a customer
feedback form's response workbook with three responses, an event
registration form nobody has answered yet, an orders workbook that is
not a response workbook, and a document that is not a workbook at all.

Microsoft Forms has no API, so there is no Forms stub: the responses
are rows in an Excel table, added the way Forms adds them.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_workbook_stub import GraphWorkbookStub

SATISFIED = "How satisfied were you with your delivery?"
IMPROVE = "Anything we should improve?"
FEEDBACK_HEADERS = ["ID", "Start time", "Completion time", "Email", "Name", SATISFIED, IMPROVE]


def response(n, name, satisfied, improve):
    email = f"{name.split()[0].lower()}@harbourline.example" if name else "anonymous"
    return [n, f"9/{n}/2026 10:00", f"9/{n}/2026 10:03", email, name, satisfied, improve]


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def graph():
    stub = GraphWorkbookStub().start()
    feedback = stub.add_file("/Forms", "Customer feedback.xlsx")
    stub.add_table(feedback, "Form1", "Form1", FEEDBACK_HEADERS, [
        response(1, "Dana Whitfield", "Very satisfied", ""),
        response(2, "Omar Haddad", "Satisfied", "Call before delivering"),
        response(3, "Lina Park", "Neutral", "The desk arrived scratched"),
    ])
    events = stub.add_file("/Forms", "Event registration.xlsx")
    stub.add_table(events, "Form1", "Form1",
                   ["ID", "Start time", "Completion time", "Email", "Name",
                    "Last modified time", "Which session?"], [])
    orders = stub.add_file("/Sales", "Sidra orders.xlsx")
    stub.add_table(orders, "Orders", "Orders", ["Order", "Customer", "Amount"],
                   [["SO-1041", "Harbourline", 18400]])
    stub.add_file("/Forms", "Feedback summary.docx")
    stub.feedback, stub.events, stub.orders = feedback, events, orders
    yield stub
    stub.stop()


def executor(graph, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"microsoft_forms__microsoft": graph.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["microsoft_forms"], name, inputs, chat_level=chat_level))


def ok(result_status):
    result, status = result_status
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["microsoft_forms"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_reads_and_watches_and_changes_nothing(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["microsoft_forms"].manifest.document["tools"]
                  for f in t["functions"]}
        assert levels == {"account.status": 0, "forms.find": 0, "responses.list": 0,
                          "responses.watch": 1, "responses.new": 1}
        _, function = agents["microsoft_forms"].manifest.function("microsoft_forms.responses.new")
        assert function["schedulable"] is True and not function.get("llm")

    def test_the_limitation_is_said_plainly(self, agents):
        agent = agents["microsoft_forms"].manifest.document["agent"]
        assert "Excel workbook" in agent["description"]
        assert "no API" in agent["instructions"]
        assert "Open in Excel" in agent["instructions"]

    def test_status_reports_the_account(self, agents, graph):
        ex, _ = executor(graph)
        assert ok(invoke(agents, ex, "microsoft_forms.account.status", {})) == {
            "connected": True, "email": GraphWorkbookStub.ACCOUNT}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result = ok(invoke(agents, ex, "microsoft_forms.account.status", {}))
        assert result["connected"] is False
        assert "No Microsoft account" in result["problem"]


class TestFinding:
    def test_find_says_which_workbooks_hold_responses(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "microsoft_forms.forms.find", {}))
        rows = {r["name"]: r for r in result["workbooks"]}
        assert set(rows) == {"Customer feedback.xlsx", "Event registration.xlsx",
                             "Sidra orders.xlsx"}
        assert [r["is_response_workbook"] for r in result["workbooks"]] == [True, True, False]
        feedback = rows["Customer feedback.xlsx"]
        assert (feedback["table"], feedback["responses"], feedback["folder"]) == ("Form1", 3, "/Forms")
        assert feedback["questions"] == [SATISFIED, IMPROVE]
        # Forms' own bookkeeping is not a question; an empty form has none yet.
        events = rows["Event registration.xlsx"]
        assert (events["questions"], events["responses"]) == (["Which session?"], 0)
        assert "table" not in rows["Sidra orders.xlsx"]


class TestReading:
    def test_the_latest_responses_come_newest_first_keyed_by_question(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "microsoft_forms.responses.list", {
            "workbook_id": graph.feedback, "max_results": 2}))
        assert (result["table"], result["total"]) == ("Form1", 3)
        assert [r["response_id"] for r in result["responses"]] == [3, 2]
        latest = result["responses"][0]
        assert (latest["name"], latest["email"], latest["completion_time"], latest["row_number"]) == (
            "Lina Park", "lina@harbourline.example", "9/3/2026 10:03", 4)
        assert latest["answers"] == {SATISFIED: "Neutral", IMPROVE: "The desk arrived scratched"}

    def test_a_workbook_without_responses_is_named_as_such(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "microsoft_forms.responses.list",
                                {"workbook_id": graph.orders})
        assert status == "error" and result["kind"] == "not_found"
        assert "Excel workbook" in result["error"]
        result, status = invoke(agents, ex, "microsoft_forms.responses.list",
                                {"workbook_id": graph.orders, "table": "Orders"})
        assert status == "error" and result["kind"] == "invalid"

    def test_an_unanswered_form_has_no_responses(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "microsoft_forms.responses.list",
                           {"workbook_id": graph.events}))
        assert (result["total"], result["responses"]) == (0, [])


class TestWatching:
    def test_a_watch_hands_on_each_new_response_once_oldest_first(self, agents, graph):
        ex, provider = executor(graph)
        watch = ok(invoke(agents, ex, "microsoft_forms.responses.watch", {
            "workbook_id": graph.feedback, "note": "thank each one"}))
        assert (watch["table"], watch["last_id"], watch["responses"]) == ("Form1", 3, 3)

        quiet = ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))
        assert quiet == {"checked": 1, "responses": [], "more": False}

        graph.append_rows(graph.feedback, "Form1", [
            response(4, "Sam Okafor", "Very satisfied", ""),
            response(5, "", "Dissatisfied", "Late by two days")])
        news = ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))
        assert [r["response_id"] for r in news["responses"]] == [4, 5]
        first = news["responses"][0]
        assert (first["watch_ref"], first["workbook"], first["name"]) == (
            watch["watch_ref"], "Customer feedback.xlsx", "Sam Okafor")
        assert news["responses"][1]["answers"][IMPROVE] == "Late by two days"
        assert news["more"] is False

        again = ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))
        assert again["responses"] == []
        keys = provider.data["microsoft_forms__watch"][watch["watch_ref"]]["keys"]
        assert (keys["last_id"], keys["row_count"]) == (5, 5)

    def test_more_than_asked_for_waits_for_the_next_check(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "microsoft_forms.responses.watch",
                          {"workbook_id": graph.feedback}))
        graph.append_rows(graph.feedback, "Form1", [
            response(n, "Dana Whitfield", "Satisfied", "") for n in (4, 5, 6)])
        page = ok(invoke(agents, ex, "microsoft_forms.responses.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}))
        assert [r["response_id"] for r in page["responses"]] == [4, 5]
        assert page["more"] is True
        page = ok(invoke(agents, ex, "microsoft_forms.responses.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}))
        assert [r["response_id"] for r in page["responses"]] == [6]
        assert page["more"] is False

    def test_rows_deleted_from_the_workbook_neither_hide_nor_repeat_a_response(self, agents, graph):
        ex, _ = executor(graph)
        ok(invoke(agents, ex, "microsoft_forms.responses.watch", {"workbook_id": graph.feedback}))
        # Someone tidies the workbook: two old responses go, and a new
        # one arrives — the table is shorter than when it was watched.
        graph.remove_row(graph.feedback, "Form1", 0)
        graph.remove_row(graph.feedback, "Form1", 0)
        graph.append_rows(graph.feedback, "Form1", [response(4, "Omar Haddad", "Satisfied", "")])
        news = ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))
        assert [r["response_id"] for r in news["responses"]] == [4]
        assert ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))["responses"] == []

    def test_the_first_response_to_an_empty_form_is_new(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "microsoft_forms.responses.watch", {"workbook_id": graph.events}))
        assert (watch["last_id"], watch["responses"]) == (0, 0)
        graph.append_rows(graph.events, "Form1", [
            [1, "9/10/2026 09:00", "9/10/2026 09:01", "dana@harbourline.example", "Dana Whitfield",
             "9/10/2026 09:01", "Morning"]])
        news = ok(invoke(agents, ex, "microsoft_forms.responses.new", {}))
        [row] = news["responses"]
        # Last modified time is Forms' bookkeeping, not an answer.
        assert row["answers"] == {"Which session?": "Morning"}

    def test_an_expired_token_says_reconnect_and_records_nothing(self, agents, graph):
        ex, provider = executor(graph, access_token="expired")
        result, status = invoke(agents, ex, "microsoft_forms.responses.watch",
                                {"workbook_id": graph.feedback})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "microsoft_forms.forms.find", {})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}
