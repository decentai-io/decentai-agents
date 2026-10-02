"""The Notion agent in a real worker against a loopback Notion API
holding Sidra Office Supplies' workspace: an "Office move tasks"
database with three rows, a "Showroom launch plan" page with a checklist,
a folded supplier list and a child page, and "Board minutes", which
exists but was not shared with the integration.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.notion_stub import NotionStub

TASKS_SCHEMA = {
    "Name": "title",
    "Status": ("status", ["Not started", "In progress", "Done"]),
    "Owner": "rich_text",
    "Due": "date",
    "Tags": ("multi_select", ["Furniture", "IT", "Facilities"]),
    "Signed off": "checkbox",
    "Budget": "number",
}


def run(awaitable):
    return asyncio.run(awaitable)


class Workspace:
    """The fictional workspace, and the ids a test needs."""

    def __init__(self, stub: NotionStub):
        self.stub = stub
        stub.clock = "2026-09-10T08:00:00.000Z"
        self.tasks = stub.add_database("Office move tasks", TASKS_SCHEMA)
        self.desks = stub.add_row(self.tasks, {
            "Name": "Order standing desks", "Status": "In progress", "Owner": "Dana",
            "Due": "2026-09-18", "Tags": ["Furniture"], "Signed off": False, "Budget": 4200},
            edited="2026-09-10T08:00:00.000Z")
        # Two rows edited in the same minute: the newest minute is shared.
        self.rack = stub.add_row(self.tasks, {
            "Name": "Move network rack", "Status": "Not started", "Owner": "Omar",
            "Due": "2026-09-25", "Tags": ["IT"], "Signed off": False, "Budget": 900},
            created="2026-09-09T10:00:00.000Z", edited="2026-09-10T08:30:00.000Z")
        self.movers = stub.add_row(self.tasks, {
            "Name": "Book movers", "Status": "Done", "Owner": "Dana",
            "Due": "2026-09-12", "Tags": ["Facilities"], "Signed off": True, "Budget": 1500},
            created="2026-09-09T10:00:00.000Z", edited="2026-09-10T08:30:00.000Z")

        self.plan = stub.add_page("Showroom launch plan", edited="2026-09-09T16:00:00.000Z")
        stub.add_block(self.plan, "heading_1", "Launch goals")
        stub.add_block(self.plan, "paragraph", "Open the showroom on 1 October.")
        stub.add_block(self.plan, "to_do", "Confirm opening date", checked=True)
        stub.add_block(self.plan, "to_do", "Print price cards", checked=False)
        contacts = stub.add_block(self.plan, "toggle", "Supplier contacts")
        stub.add_block(contacts, "paragraph", "Northlight Seating: quotes@northlight-seating.example")
        harbourline = stub.add_block(contacts, "bulleted_list_item", "Harbourline Desks")
        stub.add_block(harbourline, "paragraph", "Ask for Omar, weekdays only.")
        stub.add_block(self.plan, "numbered_list_item", "Clean the floor")
        stub.add_block(self.plan, "numbered_list_item", "Hang the signs")
        stub.add_block(self.plan, "callout", "Keys are with reception.")
        self.floor_plan = stub.add_page("Floor plan notes", parent_page=self.plan,
                                        edited="2026-09-09T15:00:00.000Z")
        stub.add_comment(self.plan, "Can we open a day earlier?")

        self.minutes = stub.add_page("Board minutes", edited="2026-09-08T12:00:00.000Z")
        stub.restricted.add(self.minutes)
        stub.clock = "2026-09-10T09:00:00.000Z"


@pytest.fixture
def ws():
    stub = NotionStub().start()
    yield Workspace(stub)
    stub.stop()


def executor(ws, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"notion__notion": ws.stub.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["notion"], name, inputs, chat_level=chat_level))


def ok(result_status):
    result, status = result_status
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["notion"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_every_change_to_the_workspace_is_level_three(self, agents):
        functions = {f"{t['id']}.{f['id']}": f
                     for t in agents["notion"].manifest.document["tools"]
                     for f in t["functions"]}
        levels = {name: f["permission_level"] for name, f in functions.items()}
        assert {n for n, level in levels.items() if level == 3} == {
            "pages.create", "pages.update", "blocks.append", "comments.add"}
        assert {n for n, level in levels.items() if level == 1} == {
            "watch.database", "watch.changes"}
        changes = functions["watch.changes"]
        assert changes["schedulable"] is True and not changes.get("llm")
        assert [n for n, f in functions.items() if f.get("schedulable")] == ["watch.changes"]

    def test_the_sign_in_is_declared_the_way_notion_wants_it(self, agents):
        oauth = agents["notion"].manifest.resource("secrets", "notion")["oauth"]
        assert oauth["provider"] == "notion"
        assert (oauth["token_auth"], oauth["token_format"]) == ("basic", "json")
        assert oauth["authorize_params"] == {"owner": "user"}
        assert oauth["identity"] == {"source": "token", "field": "owner.user.person.email"}

    def test_status_names_the_workspace(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "notion.account.status", {}))
        assert result == {"connected": True, "workspace": "Sidra Office Supplies",
                          "integration": "DecentAI", "account": NotionStub.ACCOUNT}


class TestFinding:
    def test_search_finds_a_page_by_title(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "notion.search.find", {"query": "launch"}))
        [row] = result["results"]
        assert (row["id"], row["kind"], row["title"], row["parent"]) == (
            ws.plan, "page", "Showroom launch plan", "workspace")
        assert row["url"].startswith("https://www.notion.so/")

    def test_search_narrows_to_databases_and_never_shows_what_was_not_shared(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "notion.search.find", {"kind": "database"}))
        assert [r["title"] for r in result["results"]] == ["Office move tasks"]
        everything = ok(invoke(agents, ex, "notion.search.find", {}))
        assert "Board minutes" not in [r["title"] for r in everything["results"]]
        # Newest edited first.
        edited = [r["last_edited"] for r in everything["results"]]
        assert edited == sorted(edited, reverse=True)

    def test_search_pages_with_a_cursor(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "notion.search.find", {"max_results": 2}))
        assert len(first["results"]) == 2 and first["next_cursor"]
        rest = ok(invoke(agents, ex, "notion.search.find",
                         {"start_cursor": first["next_cursor"]}))
        ids = [r["id"] for r in first["results"] + rest["results"]]
        assert len(ids) == len(set(ids)) and "next_cursor" not in rest


class TestReadingPages:
    def test_a_page_reads_as_lines_with_one_level_of_nesting(self, agents, ws):
        ex, _ = executor(ws)
        page = ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.plan}))
        assert page["title"] == "Showroom launch plan"
        assert page["properties"] == {"title": "Showroom launch plan"}
        lines = [(b["depth"], b["text"]) for b in page["blocks"]]
        assert lines == [
            (0, "# Launch goals"),
            (0, "Open the showroom on 1 October."),
            (0, "[x] Confirm opening date"),
            (0, "[ ] Print price cards"),
            (0, "> Supplier contacts"),
            (1, "Northlight Seating: quotes@northlight-seating.example"),
            (1, "- Harbourline Desks (has nested content, not shown)"),
            (0, "1. Clean the floor"),
            (0, "2. Hang the signs"),
            (0, "Note: Keys are with reception."),
            (0, f"[page] Floor plan notes (page_id {ws.floor_plan})"),
        ]
        assert (page["total"], page["more"], page["total_capped"]) == (9, False, False)
        # The child page is named, never walked into; the deeper paragraph
        # is not fetched.
        assert "Omar" not in str(page["blocks"])

    def test_a_long_page_is_read_in_windows(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.plan, "max_blocks": 4}))
        assert [b["index"] for b in first["blocks"]] == [0, 1, 2, 3]
        assert (first["total"], first["more"], first["next_from"]) == (9, True, 4)
        second = ok(invoke(agents, ex, "notion.pages.read",
                           {"page_id": ws.plan, "from": first["next_from"], "max_blocks": 4}))
        assert second["blocks"][0]["text"] == "> Supplier contacts"
        assert second["next_from"] == 8
        # Numbering counts from the list's start, not the window's.
        third = ok(invoke(agents, ex, "notion.pages.read",
                          {"page_id": ws.plan, "from": 6, "max_blocks": 4}))
        assert third["blocks"][0]["text"] == "2. Hang the signs"
        assert third["more"] is False and "next_from" not in third

    def test_a_long_paragraph_is_clipped(self, agents, ws):
        ws.stub.add_block(ws.plan, "paragraph", "Price list. " * 400)
        ex, _ = executor(ws)
        page = ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.plan, "from": 9}))
        text = page["blocks"][0]["text"]
        assert len(text) <= 2000 and text.endswith("…")

    def test_a_row_reads_its_properties_as_text(self, agents, ws):
        ex, _ = executor(ws)
        row = ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.movers}))
        assert row["properties"] == {
            "Name": "Book movers", "Status": "Done", "Owner": "Dana", "Due": "2026-09-12",
            "Tags": "Facilities", "Signed off": "yes", "Budget": "1500"}
        assert row["parent"] == f"database {ws.tasks}"
        assert row["blocks"] == [] and row["total"] == 0

    def test_comments_are_listed(self, agents, ws):
        ex, _ = executor(ws)
        result = ok(invoke(agents, ex, "notion.comments.list", {"page_id": ws.plan}))
        [comment] = result["comments"]
        assert comment["text"] == "Can we open a day earlier?"
        assert comment["author_id"] == "user-dana"


class TestReadingDatabases:
    def test_the_schema_names_every_property_and_option(self, agents, ws):
        ex, _ = executor(ws)
        schema = ok(invoke(agents, ex, "notion.databases.get", {"database_id": ws.tasks}))
        assert schema["title"] == "Office move tasks"
        by_name = {p["name"]: p for p in schema["properties"]}
        assert by_name["Status"] == {"name": "Status", "type": "status",
                                     "options": ["Not started", "In progress", "Done"]}
        assert by_name["Tags"]["options"] == ["Furniture", "IT", "Facilities"]
        assert by_name["Due"] == {"name": "Due", "type": "date"}

    def _names(self, result):
        return sorted(r["title"] for r in result["rows"])

    def test_rows_are_filtered_simply(self, agents, ws):
        ex, _ = executor(ws)
        query = lambda **kw: ok(invoke(agents, ex, "notion.databases.query",
                                       {"database_id": ws.tasks, **kw}))
        assert self._names(query(filter_property="Status", filter_operator="equals",
                                 filter_value="in progress")) == ["Order standing desks"]
        assert self._names(query(filter_property="Tags", filter_operator="contains",
                                 filter_value="IT")) == ["Move network rack"]
        assert self._names(query(filter_property="Signed off", filter_operator="checkbox",
                                 filter_value=True)) == ["Book movers"]
        assert self._names(query(filter_property="Due", filter_operator="on_or_after",
                                 filter_value="2026-09-15")) == [
            "Move network rack", "Order standing desks"]
        # The option was sent as the schema spells it.
        assert ws.stub.queries[0]["filter"] == {"property": "Status",
                                                "status": {"equals": "In progress"}}

    def test_rows_sort_and_page(self, agents, ws):
        ex, _ = executor(ws)
        first = ok(invoke(agents, ex, "notion.databases.query", {
            "database_id": ws.tasks, "sort_property": "Due", "sort_direction": "ascending",
            "max_results": 2}))
        assert [r["title"] for r in first["rows"]] == ["Book movers", "Order standing desks"]
        assert first["rows"][0]["properties"]["Signed off"] == "yes"
        rest = ok(invoke(agents, ex, "notion.databases.query", {
            "database_id": ws.tasks, "sort_property": "Due", "sort_direction": "ascending",
            "start_cursor": first["next_cursor"]}))
        assert [r["title"] for r in rest["rows"]] == ["Move network rack"]
        assert "next_cursor" not in rest

    def test_a_filter_on_an_option_that_does_not_exist_is_refused(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "notion.databases.query", {
            "database_id": ws.tasks, "filter_property": "Status",
            "filter_operator": "equals", "filter_value": "Blocked"})
        assert status == "error" and result["kind"] == "invalid"
        assert "Not started, In progress, Done" in result["error"]
        assert ws.stub.queries == []


class TestWriting:
    def test_a_row_is_checked_against_the_schema_sent_and_recorded(self, agents, ws):
        ex, provider = executor(ws)
        made = ok(invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "title": "Order desk lamps",
            "properties": {"Status": "Not started", "Owner": "Dana", "Due": "2026-09-20",
                           "Tags": ["Furniture"], "Budget": 350, "Signed off": False},
            "paragraphs": ["Warm white, twelve units."]}, chat_level=3))
        [sent] = ws.stub.created
        assert sent["parent"] == {"database_id": ws.tasks}
        assert sent["properties"]["Status"] == {"status": {"name": "Not started"}}
        assert sent["properties"]["Tags"] == {"multi_select": [{"name": "Furniture"}]}
        assert sent["properties"]["Due"] == {"date": {"start": "2026-09-20"}}
        assert sent["properties"]["Name"]["title"][0]["text"]["content"] == "Order desk lamps"
        assert sent["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == \
            "Warm white, twelve units."
        assert made["title"] == "Order desk lamps"
        assert made["parent"] == f"database {ws.tasks}"
        record = provider.data["notion__write"][made["write_ref"]]
        assert record["keys"]["action"] == "page_created"
        assert record["keys"]["page_id"] == made["page_id"]
        assert record["values"]["detail"]["sent"]["Status"] == "Not started"
        row = ok(invoke(agents, ex, "notion.pages.read", {"page_id": made["page_id"]}))
        assert row["properties"]["Budget"] == "350"
        assert row["blocks"][0]["text"] == "Warm white, twelve units."

    def test_an_unknown_property_is_refused_with_the_real_ones_listed(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "title": "Order lamps",
            "properties": {"Priority": "High"}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert "no property 'Priority'" in result["error"]
        assert "Name, Status, Owner, Due, Tags, Signed off, Budget" in result["error"]
        assert ws.stub.created == [] and provider.data == {}

    def test_an_unknown_option_is_refused_with_the_options_listed(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "title": "Order lamps",
            "properties": {"Tags": ["Furniture", "Lighting"]}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert "'Lighting' is not an option of 'Tags'" in result["error"]
        assert "Furniture, IT, Facilities" in result["error"]
        assert ws.stub.created == [] and provider.data == {}

    def test_a_computed_or_badly_typed_value_is_refused(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "properties": {"Due": "next Friday"}}, chat_level=3)
        assert status == "error" and "YYYY-MM-DD" in result["error"]
        result, status = invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "properties": {"Budget": "a lot"}}, chat_level=3)
        assert status == "error" and "number" in result["error"]
        assert ws.stub.created == []

    def test_a_page_under_a_page_takes_a_title_and_paragraphs(self, agents, ws):
        ex, provider = executor(ws)
        made = ok(invoke(agents, ex, "notion.pages.create", {
            "parent_page_id": ws.plan, "title": "Opening day checklist",
            "paragraphs": ["Doors at 10.", "Coffee for forty."]}, chat_level=3))
        assert ws.stub.created[0]["parent"] == {"page_id": ws.plan}
        page = ok(invoke(agents, ex, "notion.pages.read", {"page_id": made["page_id"]}))
        assert page["title"] == "Opening day checklist"
        assert [b["text"] for b in page["blocks"]] == ["Doors at 10.", "Coffee for forty."]
        refused, status = invoke(agents, ex, "notion.pages.create", {
            "parent_page_id": ws.plan, "title": "x", "properties": {"Status": "Done"}},
            chat_level=3)
        assert status == "error" and "database" in refused["error"]
        assert len(provider.data["notion__write"]) == 1

    def test_an_update_records_what_was_there_before(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "notion.pages.update", {
            "page_id": ws.desks, "properties": {"Status": "Done", "Budget": 3950}},
            chat_level=3))
        assert result["changed"] == ["Status", "Budget"]
        assert result["previous"] == {"Status": "In progress", "Budget": "4200"}
        assert result["now"] == {"Status": "Done", "Budget": "3950"}
        assert ws.stub.updated[0]["properties"]["Status"] == {"status": {"name": "Done"}}
        record = provider.data["notion__write"][result["write_ref"]]
        assert (record["keys"]["action"], record["keys"]["changed"]) == (
            "page_updated", "Status, Budget")
        assert record["keys"]["parent_id"] == ws.tasks
        assert record["values"]["detail"]["previous"] == {"Status": "In progress",
                                                           "Budget": "4200"}

    def test_an_update_with_an_unknown_option_sends_nothing(self, agents, ws):
        ex, provider = executor(ws)
        result, status = invoke(agents, ex, "notion.pages.update", {
            "page_id": ws.desks, "properties": {"Status": "Blocked"}}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert ws.stub.updated == [] and provider.data == {}

    def test_blocks_are_appended_in_order(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "notion.blocks.append", {
            "page_id": ws.plan, "items": [
                {"type": "to_do", "text": "Order price card stock"},
                {"type": "bullet", "text": "Ask about Saturday delivery"},
                {"text": "Signed off by Dana."}]}, chat_level=3))
        assert result["appended"] == 3 and len(result["block_ids"]) == 3
        page = ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.plan, "from": 9}))
        assert [b["text"] for b in page["blocks"]] == [
            "[ ] Order price card stock", "- Ask about Saturday delivery", "Signed off by Dana."]
        record = provider.data["notion__write"][result["write_ref"]]
        assert record["keys"]["action"] == "blocks_appended"

    def test_a_comment_is_added_and_recorded(self, agents, ws):
        ex, provider = executor(ws)
        result = ok(invoke(agents, ex, "notion.comments.add", {
            "page_id": ws.plan, "text": "Opening a day earlier works for the movers."},
            chat_level=3))
        assert ws.stub.commented[0]["parent"] == {"page_id": ws.plan}
        listed = ok(invoke(agents, ex, "notion.comments.list", {"page_id": ws.plan}))
        assert [c["text"] for c in listed["comments"]][-1] == \
            "Opening a day earlier works for the movers."
        assert provider.data["notion__write"][result["write_ref"]]["keys"]["action"] == \
            "comment_added"

    def test_writing_needs_the_chats_trust(self, agents, ws):
        ex, provider = executor(ws)
        refused, status = invoke(agents, ex, "notion.pages.create", {
            "parent_page_id": ws.plan, "title": "Unapproved"}, chat_level=1)
        assert status == "error" and refused.get("denied") is True
        assert ws.stub.created == [] and provider.data == {}


class TestWatching:
    def _changes(self, agents, ex, **inputs):
        return ok(invoke(agents, ex, "notion.watch.changes", inputs))

    def test_created_and_edited_rows_are_handed_on_exactly_once(self, agents, ws):
        stub = ws.stub
        ex, _ = executor(ws)
        watch = ok(invoke(agents, ex, "notion.watch.database",
                          {"database_id": ws.tasks, "note": "tell me what moved"}))
        # From now in Notion's clock: the newest edit marks the spot, and
        # both rows on that minute count as seen.
        assert (watch["database"], watch["since"]) == ("Office move tasks",
                                                       "2026-09-10T08:30:00.000Z")
        assert self._changes(agents, ex) == {"checked": 1, "rows": [], "more": False}
        assert stub.queries[-1]["filter"] == {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": "2026-09-10T08:30:00.000Z"}}
        assert stub.queries[-1]["sorts"] == [{"timestamp": "last_edited_time",
                                              "direction": "ascending"}]

        # 09:10 — a new row, and an old row edited, in the same minute.
        stub.clock = "2026-09-10T09:10:00.000Z"
        lamps = stub.add_row(ws.tasks, {"Name": "Order desk lamps", "Status": "Not started"})
        stub.edit_row(ws.desks, {"Status": "Done"})
        news = self._changes(agents, ex)
        rows = {r["page_id"]: r for r in news["rows"]}
        assert set(rows) == {lamps, ws.desks} and news["more"] is False
        assert rows[lamps]["change"] == "created"
        assert rows[ws.desks]["change"] == "edited"
        assert rows[ws.desks]["properties"]["Status"] == "Done"
        assert rows[ws.desks]["watch_ref"] == watch["watch_ref"]
        assert rows[ws.desks]["database"] == "Office move tasks"
        assert self._changes(agents, ex)["rows"] == []

        # Still 09:10: a third row edited on the cursor's own minute is
        # new, and is reported once.
        stub.edit_row(ws.rack, {"Owner": "Dana"})
        news = self._changes(agents, ex)
        assert [(r["page_id"], r["change"]) for r in news["rows"]] == [(ws.rack, "edited")]
        assert self._changes(agents, ex)["rows"] == []

        # The honest limit: a second edit to a reported row within the
        # same minute carries the same time and is not seen...
        stub.edit_row(ws.desks, {"Budget": 3900})
        assert self._changes(agents, ex)["rows"] == []
        # ...until an edit in a later minute.
        stub.clock = "2026-09-10T09:11:00.000Z"
        stub.edit_row(ws.desks, {"Budget": 3850})
        news = self._changes(agents, ex, watch_ref=watch["watch_ref"])
        assert [(r["page_id"], r["change"]) for r in news["rows"]] == [(ws.desks, "edited")]
        assert news["rows"][0]["properties"]["Budget"] == "3850"
        assert self._changes(agents, ex)["rows"] == []

    def test_a_busy_minute_is_handed_on_in_turns_and_says_more(self, agents, ws):
        stub = ws.stub
        ex, _ = executor(ws)
        watch = ok(invoke(agents, ex, "notion.watch.database", {"database_id": ws.tasks}))
        stub.clock = "2026-09-10T09:20:00.000Z"
        made = {stub.add_row(ws.tasks, {"Name": f"Label boxes, floor {n}"}) for n in (1, 2, 3)}
        first = self._changes(agents, ex, watch_ref=watch["watch_ref"], max_results=2)
        assert len(first["rows"]) == 2 and first["more"] is True
        second = self._changes(agents, ex, watch_ref=watch["watch_ref"], max_results=2)
        assert len(second["rows"]) == 1 and second["more"] is False
        assert {r["page_id"] for r in first["rows"] + second["rows"]} == made
        assert all(r["change"] == "created" for r in first["rows"] + second["rows"])
        assert self._changes(agents, ex)["rows"] == []


class TestTheConnection:
    def test_a_page_not_shared_says_how_to_share_it(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "notion.pages.read", {"page_id": ws.minutes})
        assert status == "error" and result["kind"] == "not_shared"
        assert "Connections" in result["error"] and "integration" in result["error"]

    def test_an_unknown_id_carries_the_same_hint(self, agents, ws):
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "notion.databases.get",
                                {"database_id": "00000000-0000-4000-8000-000000000000"})
        assert status == "error" and result["kind"] == "not_found"
        assert "not shared with the integration" in result["error"]
        assert "Connections" in result["error"]

    def test_being_rate_limited_says_when_to_try_again(self, agents, ws):
        ws.stub.limit_next = 1
        ex, _ = executor(ws)
        result, status = invoke(agents, ex, "notion.search.find", {"query": "launch"})
        assert status == "error" and result["kind"] == "rate_limited"
        assert result["retry_after_seconds"] == 7 and "7 seconds" in result["error"]

    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, ws):
        ex, provider = executor(ws, access_token="expired")
        result, status = invoke(agents, ex, "notion.search.find", {"query": "launch"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "notion.pages.create", {
            "database_id": ws.tasks, "title": "Order lamps"}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "notion.watch.database", {"database_id": ws.tasks})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {} and ws.stub.created == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result = ok(invoke(agents, ex, "notion.account.status", {}))
        assert result["connected"] is False
        assert "No Notion workspace" in result["problem"]

    def test_every_request_carries_the_pinned_version(self, agents, ws):
        ex, _ = executor(ws)
        ok(invoke(agents, ex, "notion.pages.read", {"page_id": ws.plan}))
        ok(invoke(agents, ex, "notion.databases.query", {"database_id": ws.tasks}))
        assert ws.stub.requests >= 4 and ws.stub.unversioned == 0
