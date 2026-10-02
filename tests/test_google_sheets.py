"""The Google Sheets agent in a real worker against a loopback Sheets
holding Sidra Office Supplies' spreadsheets: an orders sheet, the
responses to a workshop sign-up form, and a supplier price list with a
cell someone pasted a whole email into.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gsheets_stub import GoogleSheetsStub

ORDERS = [["Date", "Customer", "Item", "Qty", "Total"],
          ["2026-09-01", "Harbourline", "Office chairs", "20", "1,840.00"],
          ["2026-09-03", "Riverside Dental", "Printer paper", "40", "236.00"]]
SIGNUPS = [["Timestamp", "Name", "Email", "Session"],
           ["2026-09-08 09:12:00", "Dana Haddad", "dana@sidra.example", "Morning"],
           ["2026-09-08 11:40:00", "Omar Saleh", "omar@harbourline.example", "Afternoon"]]
LONG_NOTE = "Prices valid until the end of the quarter. " * 40


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def sheets():
    stub = GoogleSheetsStub().start()
    stub.orders = stub.add_spreadsheet("Sidra orders 2026", {"Orders": ORDERS, "Notes": []},
                                       modified="2026-09-10T08:00:00.000Z")
    stub.signups = stub.add_spreadsheet("Workshop sign-ups (Responses)",
                                        {"Form responses 1": SIGNUPS},
                                        modified="2026-09-08T11:40:00.000Z")
    stub.prices = stub.add_spreadsheet("Sidra supplier price list", {"Prices": [
        ["Item", "Price", "Note"],
        *[[f"Item {n}", f"{n}.00", LONG_NOTE if n == 1 else ""] for n in range(1, 31)]]},
        modified="2026-08-20T08:00:00.000Z")
    yield stub
    stub.stop()


def executor(sheets, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"google_sheets__google": sheets.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["google_sheets"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_sheets"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_asks_google_for_what_the_forms_agent_asks(self, agents):
        """One connected account is granted to every Google agent, so the
        new ones ask for the identical scope list."""
        sheets = agents["google_sheets"].manifest.resource("secrets", "google")
        forms = agents["google_forms"].manifest.resource("secrets", "google")
        assert sheets["oauth"] == forms["oauth"]
        assert sheets["fields"] == forms["fields"]
        assert "https://www.googleapis.com/auth/spreadsheets" in sheets["oauth"]["scopes"]

    def test_new_rows_is_schedulable_and_the_writes_wait(self, agents):
        _, function = agents["google_sheets"].manifest.function("google_sheets.rows.new")
        assert function["schedulable"] is True
        assert function["permission_level"] == 1 and not function.get("llm")
        for name in ("values.append", "values.update"):
            _, write = agents["google_sheets"].manifest.function(f"google_sheets.{name}")
            assert write["permission_level"] == 3, name

    def test_status_reports_the_account(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.account.status", {})
        assert (status, result) == ("success", {"connected": True,
                                                "email": GoogleSheetsStub.ACCOUNT})


class TestFinding:
    def test_find_is_newest_first_with_links(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.spreadsheets.find", {"name": "sidra"})
        assert status == "success", result
        assert [s["name"] for s in result["spreadsheets"]] == [
            "Sidra orders 2026", "Sidra supplier price list"]
        assert result["spreadsheets"][0]["link"].endswith(f"/d/{sheets.orders}/edit")

    def test_find_pages(self, agents, sheets):
        ex, _ = executor(sheets)
        first, _ = invoke(agents, ex, "google_sheets.spreadsheets.find", {"max_results": 2})
        assert len(first["spreadsheets"]) == 2 and first["next_page_token"]
        rest, _ = invoke(agents, ex, "google_sheets.spreadsheets.find",
                         {"max_results": 2, "page_token": first["next_page_token"]})
        assert [s["name"] for s in rest["spreadsheets"]] == ["Sidra supplier price list"]

    def test_get_lists_the_tabs(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.spreadsheets.get",
                                {"spreadsheet_id": sheets.orders})
        assert status == "success", result
        assert result["title"] == "Sidra orders 2026" and result["tab_count"] == 2
        assert [(t["tab"], t["grid_rows"], t["grid_columns"]) for t in result["tabs"]] == [
            ("Orders", 1000, 26), ("Notes", 1000, 26)]

    def test_an_unknown_spreadsheet_is_not_found(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.spreadsheets.get",
                                {"spreadsheet_id": "1sHtNope"})
        assert status == "error" and result["kind"] == "not_found"


class TestReading:
    def test_a_tab_reads_as_header_and_rows(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.read",
                                {"spreadsheet_id": sheets.orders, "tab": "orders"})
        assert status == "success", result
        assert result["tab"] == "Orders" and result["header"] == ORDERS[0]
        assert result["rows"] == ORDERS[1:]
        assert (result["first_row"], result["total_rows"], result["truncated"]) == (2, 2, False)

    def test_a_range_without_its_header(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.read", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "cells": "B2:C3", "header": False})
        assert status == "success", result
        assert result["header"] == [] and result["first_row"] == 2
        assert result["rows"] == [["Harbourline", "Office chairs"],
                                  ["Riverside Dental", "Printer paper"]]

    def test_a_large_read_is_cut_off_with_where_to_read_on(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.read",
                                {"spreadsheet_id": sheets.prices, "tab": "Prices", "max_rows": 100})
        assert status == "success", result
        assert result["total_rows"] == 30 and result["truncated"] is False
        note = result["rows"][0][2]
        assert len(note) == 501 and note.endswith("…") and result["clipped_cells"] == 1

        few, _ = invoke(agents, ex, "google_sheets.values.read",
                        {"spreadsheet_id": sheets.prices, "tab": "Prices", "max_rows": 5})
        assert few["shown_rows"] == 5 and few["truncated"] is True and few["next_row"] == 7

    def test_the_cell_budget_caps_a_wide_read(self, agents, sheets):
        wide = sheets.add_spreadsheet("Wide", {"Grid": [[str(c) for c in range(20)]] * 40})
        ex, _ = executor(sheets)
        result, _ = invoke(agents, ex, "google_sheets.values.read", {
            "spreadsheet_id": wide, "tab": "Grid", "header": False, "max_rows": 100})
        assert result["shown_rows"] == 10 and result["total_rows"] == 40
        assert result["next_row"] == 11

    def test_a_tab_that_is_not_there_is_named_with_the_ones_that_are(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.read",
                                {"spreadsheet_id": sheets.orders, "tab": "Invoices"})
        assert status == "error" and result["kind"] == "not_found"
        assert "'Orders'" in result["error"] and "'Notes'" in result["error"]

    def test_a_range_with_the_tab_in_it_is_refused(self, agents, sheets):
        ex, _ = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.read", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "cells": "Orders!A1:B2"})
        assert status == "error" and result["kind"] == "invalid"


class TestWriting:
    def test_an_append_lands_after_the_table_and_is_recorded(self, agents, sheets):
        ex, provider = executor(sheets)
        row = ["2026-09-14", "Harbourline", "Standing desks", "4", "2,360.00"]
        result, status = invoke(agents, ex, "google_sheets.values.append", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "rows": [row]}, chat_level=3)
        assert status == "success", result
        assert result["updated_range"] == "'Orders'!A4:E4"
        assert (result["updated_rows"], result["updated_cells"]) == (1, 5)
        assert sheets.tab(sheets.orders, "Orders")["cells"][3] == row
        [record] = provider.data["google_sheets__write"].values()
        assert record["keys"]["kind"] == "append" and record["keys"]["range"] == "'Orders'!A4:E4"
        assert record["keys"]["spreadsheet"] == "Sidra orders 2026"
        assert record["values"]["written"] == {"rows": [row], "clipped": False}

    def test_an_append_waits_for_the_user(self, agents, sheets):
        ex, provider = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.append", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "rows": [["x"]]}, chat_level=1)
        assert status == "error" and "approv" in result["error"].lower()
        assert sheets.writes == [] and provider.data == {}

    def test_an_update_keeps_what_the_cells_held(self, agents, sheets):
        ex, provider = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.update", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "start_cell": "D3",
            "rows": [["45", "265.50"], ["", "Checked"]]}, chat_level=3)
        assert status == "success", result
        assert result["updated_range"] == "'Orders'!D3:E4"
        assert result["previous"] == [["40", "236.00"], ["", ""]]
        cells = sheets.tab(sheets.orders, "Orders")["cells"]
        assert cells[2][3:5] == ["45", "265.50"] and cells[3][3:5] == ["", "Checked"]
        [record] = provider.data["google_sheets__write"].values()
        assert record["keys"]["kind"] == "update"
        assert record["values"]["previous"]["rows"] == [["40", "236.00"], ["", ""]]
        assert record["values"]["written"]["rows"] == [["45", "265.50"], ["", "Checked"]]

    def test_an_update_past_the_grid_is_refused_before_anything_is_written(self, agents, sheets):
        small = sheets.add_spreadsheet("Small", {"Tab": [["a"]]}, grid_rows=2, grid_columns=2)
        ex, provider = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.update", {
            "spreadsheet_id": small, "tab": "Tab", "start_cell": "B2",
            "rows": [["1", "2"]]}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert "values.append" in result["error"]
        assert sheets.writes == [] and provider.data == {}

    def test_a_write_that_gets_no_answer_is_unknown_and_not_retried(self, agents, sheets):
        sheets.drop_writes = True
        ex, provider = executor(sheets)
        result, status = invoke(agents, ex, "google_sheets.values.append", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "rows": [["x"]]}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert sheets.writes == ["append"]
        assert provider.data == {}


class TestWatching:
    def watch(self, agents, ex, sheets, **extra):
        watch, status = invoke(agents, ex, "google_sheets.rows.watch", {
            "spreadsheet_id": sheets.signups, "tab": "Form responses 1", **extra})
        assert status == "success", watch
        return watch

    def test_a_watch_hands_on_new_rows_exactly_once(self, agents, sheets):
        ex, provider = executor(sheets)
        watch = self.watch(agents, ex, sheets, note="welcome them")
        assert watch["cursor"] == 3 and watch["header"] == SIGNUPS[0]

        quiet, status = invoke(agents, ex, "google_sheets.rows.new", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "rows": [], "reset": [], "more": False}

        sheets.add_rows(sheets.signups, "Form responses 1", [
            ["2026-09-09 08:02:00", "Layla Nasser", "layla@riverside.example", "Morning"],
            ["2026-09-09 08:30:00", "Sam Okafor", "", "Afternoon"]])
        news, _ = invoke(agents, ex, "google_sheets.rows.new", {})
        assert [r["row"] for r in news["rows"]] == [4, 5]
        first = news["rows"][0]
        assert first["watch_ref"] == watch["watch_ref"] and first["tab"] == "Form responses 1"
        assert first["cells"] == {"Timestamp": "2026-09-09 08:02:00", "Name": "Layla Nasser",
                                  "Email": "layla@riverside.example", "Session": "Morning"}
        assert "Email" not in news["rows"][1]["cells"]
        assert news["more"] is False

        again, _ = invoke(agents, ex, "google_sheets.rows.new", {})
        assert again["rows"] == []
        [record] = provider.data["google_sheets__watch"].values()
        assert record["keys"]["cursor"] == 5

    def test_more_says_a_later_check_continues(self, agents, sheets):
        ex, _ = executor(sheets)
        watch = self.watch(agents, ex, sheets)
        sheets.add_rows(sheets.signups, "Form responses 1",
                        [[f"2026-09-09 0{n}:00:00", f"Guest {n}", "", "Morning"] for n in range(3)])
        page, _ = invoke(agents, ex, "google_sheets.rows.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["cells"]["Name"] for r in page["rows"]] == ["Guest 0", "Guest 1"]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "google_sheets.rows.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["cells"]["Name"] for r in page["rows"]] == ["Guest 2"]
        assert page["more"] is False

    def test_fewer_rows_hands_on_nothing_and_says_so(self, agents, sheets):
        ex, provider = executor(sheets)
        watch = self.watch(agents, ex, sheets)
        sheets.delete_rows(sheets.signups, "Form responses 1", 2, 1)
        result, status = invoke(agents, ex, "google_sheets.rows.new", {})
        assert status == "success", result
        assert result["rows"] == []
        [reset] = result["reset"]
        assert reset["watch_ref"] == watch["watch_ref"] and reset["cursor"] == 2
        assert "fewer" in reset["message"] and "Nothing was handed on" in reset["message"]

        # From the new count on, the next row is news again — once.
        sheets.add_rows(sheets.signups, "Form responses 1",
                        [["2026-09-10 10:00:00", "Nadia Rahman", "", "Morning"]])
        news, _ = invoke(agents, ex, "google_sheets.rows.new", {})
        assert [(r["row"], r["cells"]["Name"]) for r in news["rows"]] == [(3, "Nadia Rahman")]
        assert news["reset"] == []
        keys = provider.data["google_sheets__watch"][watch["watch_ref"]]["keys"]
        assert keys["cursor"] == 3 and keys["status"] == "watching"

    def test_a_renamed_tab_is_still_the_tab_being_watched(self, agents, sheets):
        ex, _ = executor(sheets)
        watch = self.watch(agents, ex, sheets)
        sheets.tab(sheets.signups, "Form responses 1")["title"] = "Sign-ups"
        sheets.add_rows(sheets.signups, "Sign-ups", [["2026-09-10", "Ali Hassan", "", "Morning"]])
        news, _ = invoke(agents, ex, "google_sheets.rows.new", {"watch_ref": watch["watch_ref"]})
        assert [(r["tab"], r["cells"]["Name"]) for r in news["rows"]] == [("Sign-ups", "Ali Hassan")]

    def test_a_tab_that_went_closes_its_watch(self, agents, sheets):
        ex, provider = executor(sheets)
        watch = self.watch(agents, ex, sheets)
        sheets.spreadsheets[sheets.signups]["tabs"] = []
        result, _ = invoke(agents, ex, "google_sheets.rows.new", {})
        assert result["rows"] == [] and "no longer" in result["reset"][0]["message"]
        assert provider.data["google_sheets__watch"][watch["watch_ref"]]["keys"]["status"] == "closed"
        after, _ = invoke(agents, ex, "google_sheets.rows.new", {})
        assert after == {"checked": 0, "rows": [], "reset": [], "more": False}

    def test_without_a_header_cells_are_keyed_by_column(self, agents, sheets):
        ex, _ = executor(sheets)
        watch, status = invoke(agents, ex, "google_sheets.rows.watch", {
            "spreadsheet_id": sheets.orders, "tab": "Notes", "header_row": 0})
        assert status == "success" and watch["cursor"] == 0 and watch["header"] == []
        sheets.add_rows(sheets.orders, "Notes", [["Call Harbourline", "", "Friday"]])
        news, _ = invoke(agents, ex, "google_sheets.rows.new", {"watch_ref": watch["watch_ref"]})
        assert [(r["row"], r["cells"]) for r in news["rows"]] == [
            (1, {"A": "Call Harbourline", "C": "Friday"})]


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, sheets):
        ex, provider = executor(sheets, access_token="expired")
        result, status = invoke(agents, ex, "google_sheets.values.read",
                                {"spreadsheet_id": sheets.orders, "tab": "Orders"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "google_sheets.values.append", {
            "spreadsheet_id": sheets.orders, "tab": "Orders", "rows": [["x"]]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert len(sheets.tab(sheets.orders, "Orders")["cells"]) == 3
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "google_sheets.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Google account" in result["problem"]
