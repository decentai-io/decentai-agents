"""The Excel Online agent in a real worker against a loopback Microsoft
Graph holding Sidra Office Supplies' workbooks: an orders workbook
under Sales with an Orders table and a plain summary sheet, a stock
count too long to read in one go, and a notes document that is not a
workbook at all.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_workbook_stub import GraphWorkbookStub

HEADERS = ["Order", "Customer", "Item", "Amount", "Due"]
ORDERS = [
    ["SO-1041", "Harbourline", "Desk, oak", 18400, "2026-11-30"],
    ["SO-1042", "Northlight", "Chair, mesh", 6200, "2026-10-15"],
    ["SO-1043", "Riverside Clinic", "Filing cabinet", 2350, "2026-10-01"],
]
SCOPES = ["openid", "email", "offline_access", "User.Read", "Mail.ReadWrite",
          "Mail.Send", "Calendars.ReadWrite", "Files.ReadWrite.All", "Chat.ReadWrite",
          "Team.ReadBasic.All", "Channel.ReadBasic.All", "ChannelMessage.Send",
          "Tasks.ReadWrite"]


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def graph():
    stub = GraphWorkbookStub().start()
    orders = stub.add_file("/Sales", "Sidra orders.xlsx")
    stub.add_table(orders, "Orders", "Orders", HEADERS, ORDERS)
    stub.add_table(orders, "Returns", "Returns", ["Order", "Reason"], [])
    stub.add_sheet(orders, "Q3 Summary", [["Month", "Revenue"], ["Jul", 41200],
                                          ["Aug", 38950], ["Sep", 44010]])
    stock = stub.add_file("/Warehouse", "Stock count.xlsx")
    stub.add_sheet(stock, "Count", [["SKU", "On hand"]]
                   + [[f"SKU-{n:03d}", n * 3] for n in range(1, 31)])
    stub.add_file("/Sales", "Sidra orders notes.docx")
    stub.orders, stub.stock = orders, stock
    yield stub
    stub.stop()


def executor(graph, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"excel_online__microsoft": graph.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["excel_online"], name, inputs, chat_level=chat_level))


def ok(result_status):
    result, status = result_status
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["excel_online"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_one_connection_serves_both_workbook_agents(self, agents):
        mine = agents["excel_online"].manifest.resource("secrets", "microsoft")
        theirs = agents["microsoft_forms"].manifest.resource("secrets", "microsoft")
        assert mine["oauth"]["scopes"] == SCOPES
        assert theirs["oauth"] == mine["oauth"]
        assert theirs["fields"] == mine["fields"]

    def test_every_change_to_a_workbook_is_level_three(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["excel_online"].manifest.document["tools"]
                  for f in t["functions"]}
        assert {name for name, level in levels.items() if level == 3} == {
            "table.add_rows", "range.update"}
        assert levels["rows.watch"] == 1

    def test_new_rows_is_declared_schedulable(self, agents):
        _, function = agents["excel_online"].manifest.function("excel_online.rows.new")
        assert function["schedulable"] is True
        assert function["permission_level"] == 1 and not function.get("llm")

    def test_status_reports_the_account(self, agents, graph):
        ex, _ = executor(graph)
        assert ok(invoke(agents, ex, "excel_online.account.status", {})) == {
            "connected": True, "email": GraphWorkbookStub.ACCOUNT}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result = ok(invoke(agents, ex, "excel_online.account.status", {}))
        assert result["connected"] is False
        assert "No Microsoft account" in result["problem"]


class TestReading:
    def test_find_keeps_only_workbooks(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.workbooks.find", {"query": "sidra orders"}))
        assert [(w["name"], w["folder"]) for w in result["workbooks"]] == [
            ("Sidra orders.xlsx", "/Sales")]
        every = ok(invoke(agents, ex, "excel_online.workbooks.find", {}))
        assert sorted(w["name"] for w in every["workbooks"]) == [
            "Sidra orders.xlsx", "Stock count.xlsx"]

    def test_get_names_the_sheets_and_tables(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.workbooks.get",
                           {"workbook_id": graph.orders}))
        assert [(s["name"], s["used_range"], s["rows"], s["columns"])
                for s in result["worksheets"]] == [
            ("Orders", "A1:E4", 4, 5), ("Returns", "A1:B1", 1, 2), ("Q3 Summary", "A1:B4", 4, 2)]
        assert result["tables"] == [
            {"name": "Orders", "worksheet": "Orders", "headers": HEADERS, "rows": 3},
            # An empty table keeps one blank body row; it is not data.
            {"name": "Returns", "worksheet": "Returns", "headers": ["Order", "Reason"], "rows": 0}]

    def test_a_document_is_not_opened_as_a_workbook(self, agents, graph):
        ex, _ = executor(graph)
        notes = next(i for i, item in graph.items.items() if item["name"].endswith(".docx"))
        result, status = invoke(agents, ex, "excel_online.workbooks.get", {"workbook_id": notes})
        assert status == "error" and result["kind"] == "invalid"

    def test_the_used_range_reads_with_a_header(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.range.read", {
            "workbook_id": graph.orders, "worksheet": "Q3 Summary", "header": True}))
        assert result["header"] == ["Month", "Revenue"]
        assert result["rows"] == [["Jul", "41200"], ["Aug", "38950"], ["Sep", "44010"]]
        assert (result["address"], result["row_count"], result["rows_cut"]) == ("A1:B4", 3, 0)

    def test_a_long_read_is_capped_and_says_what_was_cut(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.range.read", {
            "workbook_id": graph.stock, "worksheet": "Count", "header": True, "max_rows": 5}))
        assert len(result["rows"]) == 5 and result["rows"][0] == ["SKU-001", "3"]
        assert (result["row_count"], result["rows_cut"]) == (30, 25)
        narrow = ok(invoke(agents, ex, "excel_online.range.read", {
            "workbook_id": graph.stock, "worksheet": "Count", "address": "A30:B31"}))
        assert narrow["rows"] == [["SKU-029", "87"], ["SKU-030", "90"]]

    def test_a_whole_column_is_not_an_address(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "excel_online.range.read", {
            "workbook_id": graph.stock, "worksheet": "Count", "address": "A:B"})
        assert status == "error" and "row numbers" in result["error"]

    def test_table_rows_are_keyed_by_header_and_paged(self, agents, graph):
        ex, _ = executor(graph)
        first = ok(invoke(agents, ex, "excel_online.table.rows", {
            "workbook_id": graph.orders, "table": "Orders", "max_results": 2}))
        assert first["total"] == 3 and first["next_page_token"] == "2"
        assert first["rows"][0] == {"Order": "SO-1041", "Customer": "Harbourline",
                                    "Item": "Desk, oak", "Amount": "18400", "Due": "2026-11-30"}
        rest = ok(invoke(agents, ex, "excel_online.table.rows", {
            "workbook_id": graph.orders, "table": "Orders",
            "page_token": first["next_page_token"]}))
        assert [r["Order"] for r in rest["rows"]] == ["SO-1043"]
        assert "next_page_token" not in rest

    def test_a_table_that_is_not_there_is_not_found(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "excel_online.table.rows", {
            "workbook_id": graph.orders, "table": "Invoices"})
        assert status == "error" and result["kind"] == "not_found"


class TestWriting:
    def test_rows_are_added_in_one_session_and_recorded(self, agents, graph):
        ex, provider = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.table.add_rows", {
            "workbook_id": graph.orders, "table": "Orders",
            "rows": [{"order": "SO-1044", "Customer": "Harbourline", "Item": "Chair, mesh",
                      "Amount": 3100}]}, chat_level=3))
        assert (result["table"], result["added"], result["total_rows"]) == ("Orders", 1, 4)
        assert graph.table_rows(graph.orders, "Orders")[-1] == [
            "SO-1044", "Harbourline", "Chair, mesh", 3100, ""]
        [session] = graph.sessions
        assert session["persistChanges"] is True and session["closed"] is True
        assert ("POST", f"/me/drive/items/{graph.orders}/workbook/tables/Orders/rows/add",
                session["id"]) in graph.session_requests
        record = provider.data["excel_online__write"][result["write_ref"]]
        assert record["keys"] == {"kind": "add_rows", "workbook_id": graph.orders,
                                  "workbook": "Sidra orders.xlsx", "target": "Orders",
                                  "rows": 1, "status": "written"}
        assert record["values"]["written"]["rows"] == [
            ["SO-1044", "Harbourline", "Chair, mesh", 3100, ""]]

    def test_the_first_row_of_an_empty_table_fills_its_blank_row(self, agents, graph):
        ex, _ = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.table.add_rows", {
            "workbook_id": graph.orders, "table": "Returns",
            "rows": [{"Order": "SO-1042", "Reason": "Wrong colour"}]}, chat_level=3))
        assert result["total_rows"] == 1
        assert graph.table_rows(graph.orders, "Returns") == [["SO-1042", "Wrong colour"]]

    def test_a_column_the_table_lacks_is_refused_and_nothing_written(self, agents, graph):
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "excel_online.table.add_rows", {
            "workbook_id": graph.orders, "table": "Orders",
            "rows": [{"Order": "SO-1045", "Discount": "10%"}]}, chat_level=3)
        assert status == "error" and "'Discount'" in result["error"]
        assert "Customer" in result["error"]
        assert graph.writes == [] and graph.sessions == [] and provider.data == {}

    def test_an_overwrite_keeps_what_was_there(self, agents, graph):
        ex, provider = executor(graph)
        result = ok(invoke(agents, ex, "excel_online.range.update", {
            "workbook_id": graph.orders, "worksheet": "Q3 Summary", "address": "B3:B4",
            "values": [[39100], [44250]]}, chat_level=3))
        assert (result["address"], result["cells"]) == ("B3:B4", 2)
        assert result["previous"] == [[38950], [44010]]
        assert graph.cell(graph.orders, "Q3 Summary", "B4") == 44250
        [session] = graph.sessions
        assert session["closed"] is True
        assert any(method == "PATCH" and sid == session["id"]
                   for method, _, sid in graph.session_requests)
        record = provider.data["excel_online__write"][result["write_ref"]]
        assert record["keys"]["target"] == "Q3 Summary!B3:B4"
        assert record["keys"]["kind"] == "update_range"
        assert record["values"]["previous"] == {"rows": [[38950], [44010]]}
        assert record["values"]["written"] == {"rows": [[39100], [44250]]}

    def test_values_of_the_wrong_shape_are_refused_before_anything_is_read(self, agents, graph):
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "excel_online.range.update", {
            "workbook_id": graph.orders, "worksheet": "Q3 Summary", "address": "B2:B4",
            "values": [[1], [2]]}, chat_level=3)
        assert status == "error" and "3 row(s) by 1 column(s)" in result["error"]
        assert graph.writes == [] and provider.data == {}

    def test_a_write_waits_for_the_chats_trust(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "excel_online.table.add_rows", {
            "workbook_id": graph.orders, "table": "Orders",
            "rows": [{"Order": "SO-1046"}]}, chat_level=1)
        assert status == "error" and "approv" in result["error"].lower()
        assert graph.writes == []

    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, graph):
        ex, provider = executor(graph, access_token="expired")
        result, status = invoke(agents, ex, "excel_online.table.add_rows", {
            "workbook_id": graph.orders, "table": "Orders",
            "rows": [{"Order": "SO-1047"}]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "table": "Orders"})
        assert status == "error" and result["kind"] == "auth"
        assert graph.writes == [] and graph.sessions == [] and provider.data == {}


class TestWatching:
    def test_a_table_watch_hands_on_each_new_row_once(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "table": "Orders", "note": "tell me"}))
        assert (watch["rows"], watch["worksheet"], watch["headers"]) == (3, "Orders", HEADERS)

        quiet = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert quiet == {"checked": 1, "rows": [], "more": False, "reset": []}

        graph.append_rows(graph.orders, "Orders", [
            ["SO-1050", "Harbourline", "Desk, walnut", 9900, "2026-12-01"]])
        news = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        [row] = news["rows"]
        assert (row["watch_ref"], row["table"], row["row_number"]) == (watch["watch_ref"], "Orders", 5)
        assert row["values"]["Customer"] == "Harbourline" and row["values"]["Amount"] == "9900"

        again = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert again["rows"] == []

    def test_more_than_asked_for_waits_for_the_next_check(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "table": "Orders"}))
        graph.append_rows(graph.orders, "Orders", [
            [f"SO-106{n}", "Northlight", "Chair", 100 * n, ""] for n in range(3)])
        page = ok(invoke(agents, ex, "excel_online.rows.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}))
        assert [r["values"]["Order"] for r in page["rows"]] == ["SO-1060", "SO-1061"]
        assert page["more"] is True
        page = ok(invoke(agents, ex, "excel_online.rows.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}))
        assert [r["values"]["Order"] for r in page["rows"]] == ["SO-1062"]
        assert page["more"] is False

    def test_an_empty_tables_first_row_is_new(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "table": "Returns"}))
        assert watch["rows"] == 0
        graph.append_rows(graph.orders, "Returns", [["SO-1041", "Scratched top"]])
        news = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert [r["values"] for r in news["rows"]] == [{"Order": "SO-1041", "Reason": "Scratched top"}]

    def test_a_worksheet_watch_reads_its_used_range_as_a_table(self, agents, graph):
        ex, _ = executor(graph)
        watch = ok(invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "worksheet": "Q3 Summary"}))
        assert (watch["table"], watch["rows"], watch["headers"]) == ("", 3, ["Month", "Revenue"])
        graph.add_sheet(graph.orders, "Q3 Summary", [["Oct", 40500]], at="A5")
        news = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert [(r["row_number"], r["values"]) for r in news["rows"]] == [
            (5, {"Month": "Oct", "Revenue": "40500"})]
        assert ok(invoke(agents, ex, "excel_online.rows.new", {}))["rows"] == []

    def test_a_table_that_lost_rows_reports_nothing_and_starts_again(self, agents, graph):
        ex, provider = executor(graph)
        watch = ok(invoke(agents, ex, "excel_online.rows.watch", {
            "workbook_id": graph.orders, "table": "Orders"}))
        graph.remove_row(graph.orders, "Orders", 0)
        graph.remove_row(graph.orders, "Orders", 0)
        graph.append_rows(graph.orders, "Orders", [["SO-1070", "Harbourline", "Lamp", 80, ""]])
        checked = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert checked["rows"] == []
        [reset] = checked["reset"]
        assert (reset["watch_ref"], reset["rows"]) == (watch["watch_ref"], 2)
        assert "fewer than the 3" in reset["note"]
        assert provider.data["excel_online__watch"][watch["watch_ref"]]["keys"]["cursor"] == 2

        # From the new size on, the watch counts as before.
        graph.append_rows(graph.orders, "Orders", [["SO-1071", "Northlight", "Desk", 900, ""]])
        news = ok(invoke(agents, ex, "excel_online.rows.new", {}))
        assert [r["values"]["Order"] for r in news["rows"]] == ["SO-1071"]
        assert news["reset"] == []
