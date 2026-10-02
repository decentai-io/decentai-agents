"""The Google Docs agent in a real worker against a loopback Docs and
Drive holding Sidra Office Supplies' documents: a Q3 proposal with a
heading, a delivery paragraph, a price table and a logo, and a supplier
handbook edited earlier.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gdocs_stub import GoogleDocsStub

DANA = "Dana Harbour"
DELIVERY = "Delivery takes fifteen working days from the signed order."
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/tasks",
]


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def docs():
    stub = GoogleDocsStub().start()
    stub.handbook = stub.add_document("Sidra supplier handbook", [
        ("TITLE", "Supplier handbook"),
        ("NORMAL_TEXT", "Returns are accepted within thirty days."),
    ])
    stub.proposal = stub.add_document("Q3 proposal — Harbourline offices", [
        ("HEADING_1", "Proposal"),
        ("NORMAL_TEXT", "Sidra Office Supplies will furnish twenty desks."),
        ("HEADING_2", "Delivery"),
        ("NORMAL_TEXT", DELIVERY),
        {"table": [[["Desk"], ["120.00"]], [["Chair"], ["45.50"]]]},
        ("NORMAL_TEXT", "", "image"),
        ("NORMAL_TEXT", "Prices are valid until 30 September 2026."),
    ])
    yield stub
    stub.stop()


def executor(docs, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"google_docs__google": docs.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["google_docs"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_docs"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_asks_google_for_the_shared_scope_list(self, agents):
        oauth = agents["google_docs"].manifest.resource("secrets", "google")["oauth"]
        assert oauth["scopes"] == SCOPES

    def test_what_each_function_costs(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["google_docs"].manifest.document["tools"]
                  for f in t["functions"]}
        assert levels == {
            "account.status": 0, "docs.find": 0, "docs.read": 0,
            "comments.list": 0, "comments.add": 3, "comments.reply": 3,
            "comments.resolve": 3, "comments.watch": 1, "comments.new": 1,
            "edits.propose": 1, "edits.apply": 3, "edits.discard": 1}
        _, new = agents["google_docs"].manifest.function("google_docs.comments.new")
        assert new["schedulable"] is True and not new.get("llm")

    def test_status_reports_the_account(self, agents, docs):
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.account.status", {})
        assert status == "success", result
        assert result == {"connected": True, "email": GoogleDocsStub.ACCOUNT}


class TestReading:
    def test_find_returns_documents_newest_first_with_links(self, agents, docs):
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.docs.find", {"query": "sidra"})
        assert status == "success", result
        assert [d["title"] for d in result["documents"]] == [
            "Q3 proposal — Harbourline offices", "Sidra supplier handbook"]
        assert result["documents"][0]["link"].endswith(f"/document/d/{docs.proposal}/edit")

    def test_a_document_is_read_as_numbered_paragraphs_a_slice_at_a_time(self, agents, docs):
        ex, _ = executor(docs)
        first, status = invoke(agents, ex, "google_docs.docs.read",
                               {"document_id": docs.proposal, "max_paragraphs": 3})
        assert status == "success", first
        assert first["title"] == "Q3 proposal — Harbourline offices"
        assert first["revision_id"] == "ALm37BV-rev1"
        assert first["total_paragraphs"] == 10
        assert [(p["index"], p["style"], p["text"]) for p in first["paragraphs"]] == [
            (1, "HEADING_1", "Proposal"),
            (2, "NORMAL_TEXT", "Sidra Office Supplies will furnish twenty desks."),
            (3, "HEADING_2", "Delivery")]
        assert first["next_from"] == 4

        rest, _ = invoke(agents, ex, "google_docs.docs.read",
                         {"document_id": docs.proposal, "from": first["next_from"]})
        assert [(p["index"], p["text"], p["in_table"]) for p in rest["paragraphs"]] == [
            (4, DELIVERY, False), (5, "Desk", True), (6, "120.00", True),
            (7, "Chair", True), (8, "45.50", True), (9, "", False),
            (10, "Prices are valid until 30 September 2026.", False)]
        assert "next_from" not in rest

    def test_the_character_budget_stops_a_slice_early(self, agents, docs):
        ex, _ = executor(docs)
        result, _ = invoke(agents, ex, "google_docs.docs.read",
                           {"document_id": docs.proposal, "from": 2, "max_chars": 500})
        assert len(result["paragraphs"]) == 9
        small, _ = invoke(agents, ex, "google_docs.docs.read",
                          {"document_id": docs.proposal, "from": 2, "max_chars": 500,
                           "max_paragraphs": 1})
        assert small["next_from"] == 3

    def test_an_unknown_document_is_not_found(self, agents, docs):
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.docs.read", {"document_id": "nope"})
        assert status == "error" and result["kind"] == "not_found"


class TestComments:
    def test_comments_are_listed_open_and_resolved_with_their_replies(self, agents, docs):
        open_id = docs.comment_as(docs.proposal, DANA, "Can this be ten days?", quote=DELIVERY)
        docs.reply_as(docs.proposal, open_id, "Demo", "Checking with the warehouse.", me=True)
        done_id = docs.comment_as(docs.proposal, DANA, "Typo in the heading.")
        docs.reply_as(docs.proposal, done_id, "Demo", "Fixed.", action="resolve", me=True)
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.comments.list",
                                {"document_id": docs.proposal})
        assert status == "success", result
        rows = {c["comment_id"]: c for c in result["comments"]}
        assert (rows[open_id]["author"], rows[open_id]["quoted"], rows[open_id]["resolved"],
                rows[open_id]["reply_count"]) == (DANA, DELIVERY, False, 1)
        assert rows[done_id]["resolved"] is True
        assert [(r["comment_id"], r["text"], r["action"]) for r in result["replies"]] == [
            (open_id, "Checking with the warehouse.", ""), (done_id, "Fixed.", "resolve")]

        only_open, _ = invoke(agents, ex, "google_docs.comments.list",
                              {"document_id": docs.proposal, "include_resolved": False})
        assert [c["comment_id"] for c in only_open["comments"]] == [open_id]

    def test_a_comment_quoting_the_document_is_added_replied_to_and_resolved(self, agents, docs):
        ex, _ = executor(docs)
        added, status = invoke(agents, ex, "google_docs.comments.add", {
            "document_id": docs.proposal, "text": "Confirm with logistics.",
            "quote": "fifteen working days"}, chat_level=3)
        assert status == "success", added
        comment = docs.find_comment(docs.proposal, added["comment_id"])
        assert comment["quotedFileContent"]["value"] == "fifteen working days"
        replied, status = invoke(agents, ex, "google_docs.comments.reply", {
            "document_id": docs.proposal, "comment_id": added["comment_id"],
            "text": "Logistics confirmed."}, chat_level=3)
        assert status == "success", replied
        resolved, status = invoke(agents, ex, "google_docs.comments.resolve", {
            "document_id": docs.proposal, "comment_id": added["comment_id"]}, chat_level=3)
        assert status == "success" and resolved["resolved"] is True, resolved
        assert comment["resolved"] is True
        assert [r.get("action", "") for r in comment["replies"]] == ["", "resolve"]

    def test_a_quote_the_document_does_not_hold_is_refused_and_nothing_posted(self, agents, docs):
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.comments.add", {
            "document_id": docs.proposal, "text": "?", "quote": "ten working days"}, chat_level=3)
        assert status == "error" and result["kind"] == "invalid"
        assert docs.comment_writes == []

    def test_commenting_needs_the_chats_trust(self, agents, docs):
        ex, _ = executor(docs)
        result, status = invoke(agents, ex, "google_docs.comments.add", {
            "document_id": docs.proposal, "text": "Hello"}, chat_level=1)
        assert status == "error" and "approv" in result["error"].lower()
        assert docs.comment_writes == []


class TestEdits:
    def propose(self, agents, ex, docs, paragraph=4, text="Delivery takes ten working days from the signed order."):
        result, status = invoke(agents, ex, "google_docs.edits.propose", {
            "document_id": docs.proposal, "paragraph": paragraph, "new_text": text}, chat_level=1)
        assert status == "success", result
        return result

    def test_a_proposal_is_recorded_and_the_document_does_not_move(self, agents, docs):
        ex, provider = executor(docs)
        before = docs.text_of(docs.proposal)
        proposed = self.propose(agents, ex, docs)
        assert (proposed["old_text"], proposed["revision_id"]) == (DELIVERY, "ALm37BV-rev1")
        keys = provider.data["google_docs__edit"][proposed["edit_ref"]]["keys"]
        assert (keys["paragraph"], keys["old_text"], keys["status"]) == (4, DELIVERY, "proposed")
        assert docs.text_of(docs.proposal) == before
        assert docs.batch_updates == []

    def test_applying_replaces_only_that_paragraphs_text(self, agents, docs):
        ex, provider = executor(docs)
        before = docs.text_of(docs.proposal)
        proposed = self.propose(agents, ex, docs)
        applied, status = invoke(agents, ex, "google_docs.edits.apply",
                                 {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "success", applied
        assert applied["revision_id"] == "ALm37BV-rev2"
        after = docs.text_of(docs.proposal)
        assert after[3] == "Delivery takes ten working days from the signed order."
        assert after[:3] == before[:3] and after[4:] == before[4:]
        [sent] = docs.batch_updates
        assert sent["writeControl"] == {"requiredRevisionId": "ALm37BV-rev1"}
        keys = provider.data["google_docs__edit"][proposed["edit_ref"]]["keys"]
        assert (keys["status"], keys["applied_revision_id"]) == ("applied", "ALm37BV-rev2")

        again, status = invoke(agents, ex, "google_docs.edits.apply",
                               {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and "applied" in again["error"]
        assert len(docs.batch_updates) == 1

    def test_a_paragraph_after_a_table_and_an_empty_one_are_found_by_range(self, agents, docs):
        ex, _ = executor(docs)
        last = self.propose(agents, ex, docs, paragraph=10, text="Prices hold until 31 October 2026.")
        applied, status = invoke(agents, ex, "google_docs.edits.apply",
                                 {"edit_ref": last["edit_ref"]}, chat_level=3)
        assert status == "success", applied
        assert docs.text_of(docs.proposal)[9] == "Prices hold until 31 October 2026."
        cell = self.propose(agents, ex, docs, paragraph=6, text="115.00")
        applied, status = invoke(agents, ex, "google_docs.edits.apply",
                                 {"edit_ref": cell["edit_ref"]}, chat_level=3)
        assert status == "success", applied
        assert docs.text_of(docs.proposal)[4:8] == ["Desk", "115.00", "Chair", "45.50"]

    def test_an_edit_to_a_document_changed_since_is_refused_not_clobbered(self, agents, docs):
        ex, provider = executor(docs)
        proposed = self.propose(agents, ex, docs)
        docs.edit_as(docs.proposal, 2, "Sidra Office Supplies will furnish thirty desks.")
        result, status = invoke(agents, ex, "google_docs.edits.apply",
                                {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "conflict", result
        assert "changed" in result["error"]
        assert docs.batch_updates == []
        assert docs.text_of(docs.proposal)[3] == DELIVERY
        keys = provider.data["google_docs__edit"][proposed["edit_ref"]]["keys"]
        assert keys["status"] == "refused" and "changed" in keys["reason"]

    def test_a_change_landing_between_read_and_write_is_refused_by_google(self, agents, docs):
        ex, provider = executor(docs)
        proposed = self.propose(agents, ex, docs)
        docs.before_batch = lambda doc_id: docs.edit_as(doc_id, 4, "Delivery is by arrangement.")
        result, status = invoke(agents, ex, "google_docs.edits.apply",
                                {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "conflict", result
        assert docs.text_of(docs.proposal)[3] == "Delivery is by arrangement."
        assert provider.data["google_docs__edit"][proposed["edit_ref"]]["keys"]["status"] == "refused"

    def test_an_apply_with_no_answer_is_unknown_and_not_sent_again(self, agents, docs):
        ex, provider = executor(docs)
        proposed = self.propose(agents, ex, docs)
        docs.drop_batch = True
        result, status = invoke(agents, ex, "google_docs.edits.apply",
                                {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        again, status = invoke(agents, ex, "google_docs.edits.apply",
                               {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and again["kind"] == "unknown"
        assert len(docs.batch_updates) == 1
        assert provider.data["google_docs__edit"][proposed["edit_ref"]]["keys"]["status"] == "unknown"

    def test_a_paragraph_with_an_image_or_a_number_past_the_end_is_not_proposed(self, agents, docs):
        ex, provider = executor(docs)
        image, status = invoke(agents, ex, "google_docs.edits.propose", {
            "document_id": docs.proposal, "paragraph": 9, "new_text": "x"}, chat_level=1)
        assert status == "error" and "image" in image["error"]
        past, status = invoke(agents, ex, "google_docs.edits.propose", {
            "document_id": docs.proposal, "paragraph": 11, "new_text": "x"}, chat_level=1)
        assert status == "error" and "10 paragraphs" in past["error"]
        assert provider.data == {}

    def test_applying_needs_the_chats_trust_and_discard_leaves_the_document(self, agents, docs):
        ex, provider = executor(docs)
        proposed = self.propose(agents, ex, docs)
        denied, status = invoke(agents, ex, "google_docs.edits.apply",
                                {"edit_ref": proposed["edit_ref"]}, chat_level=1)
        assert status == "error" and "approv" in denied["error"].lower()
        discarded, status = invoke(agents, ex, "google_docs.edits.discard",
                                   {"edit_ref": proposed["edit_ref"]}, chat_level=1)
        assert status == "success" and discarded["discarded"] is True
        result, status = invoke(agents, ex, "google_docs.edits.apply",
                                {"edit_ref": proposed["edit_ref"]}, chat_level=3)
        assert status == "error" and "discarded" in result["error"]
        assert docs.batch_updates == []


class TestWatching:
    def test_the_watch_hands_on_what_others_wrote_exactly_once(self, agents, docs):
        earlier = docs.comment_as(docs.proposal, DANA, "Old question.")
        ex, _ = executor(docs)
        watch, status = invoke(agents, ex, "google_docs.comments.watch",
                               {"document_id": docs.proposal}, chat_level=1)
        assert status == "success", watch
        assert watch["since"] == docs.find_comment(docs.proposal, earlier)["createdTime"]

        quiet, status = invoke(agents, ex, "google_docs.comments.new", {}, chat_level=1)
        assert status == "success", quiet
        assert quiet == {"checked": 1, "comments": [], "more": False}

        # The account's own comment is not news; Dana's comment and her
        # reply on the old thread are.
        docs.comment_as(docs.proposal, "Demo", "Note to self.", me=True)
        asked = docs.comment_as(docs.proposal, DANA, "Ten days possible?", quote=DELIVERY)
        answered = docs.reply_as(docs.proposal, earlier, DANA, "Still waiting on this.")
        news, _ = invoke(agents, ex, "google_docs.comments.new", {}, chat_level=1)
        assert [(c["kind"], c["comment_id"], c["reply_id"]) for c in news["comments"]] == [
            ("comment", asked, ""), ("reply", earlier, answered)]
        first = news["comments"][0]
        assert (first["author"], first["quoted"], first["watch_ref"], first["title"]) == (
            DANA, DELIVERY, watch["watch_ref"], "Q3 proposal — Harbourline offices")
        assert news["comments"][1]["in_reply_to"] == "Old question."

        again, _ = invoke(agents, ex, "google_docs.comments.new", {}, chat_level=1)
        assert again["comments"] == []

        # A second comment at the very same moment is neither lost nor
        # shown twice.
        same_moment = docs.find_comment(docs.proposal, earlier)["replies"][-1]["createdTime"]
        twin = docs.comment_as(docs.proposal, DANA, "And the chairs?", at=same_moment)
        news, _ = invoke(agents, ex, "google_docs.comments.new", {}, chat_level=1)
        assert [c["comment_id"] for c in news["comments"]] == [twin]
        again, _ = invoke(agents, ex, "google_docs.comments.new", {}, chat_level=1)
        assert again["comments"] == []

    def test_a_long_backlog_comes_a_page_at_a_time(self, agents, docs):
        ex, _ = executor(docs)
        watch, _ = invoke(agents, ex, "google_docs.comments.watch",
                          {"document_id": docs.handbook}, chat_level=1)
        made = [docs.comment_as(docs.handbook, DANA, f"Point {n}") for n in range(3)]
        page, _ = invoke(agents, ex, "google_docs.comments.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}, chat_level=1)
        assert [c["comment_id"] for c in page["comments"]] == made[:2] and page["more"] is True
        page, _ = invoke(agents, ex, "google_docs.comments.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2}, chat_level=1)
        assert [c["comment_id"] for c in page["comments"]] == made[2:] and page["more"] is False


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, docs):
        ex, provider = executor(docs, access_token="expired")
        result, status = invoke(agents, ex, "google_docs.docs.read", {"document_id": docs.proposal})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        for name, inputs in (("google_docs.edits.propose",
                              {"document_id": docs.proposal, "paragraph": 4, "new_text": "x"}),
                             ("google_docs.comments.watch", {"document_id": docs.proposal})):
            result, status = invoke(agents, ex, name, inputs, chat_level=1)
            assert status == "error" and result["kind"] == "auth", name
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "google_docs.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Google account" in result["problem"]
