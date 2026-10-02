"""The Google Forms agent in a real worker against a loopback Forms
holding Sidra Office Supplies' customer feedback form — a name, a
rating, the products bought, comments, a receipt upload and a delivery
grid — and a few responses already in.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gforms_stub import GoogleFormsStub

ITEMS = [
    {"itemId": "i1", "title": "Your name",
     "questionItem": {"question": {"questionId": "q-name", "required": True,
                                   "textQuestion": {}}}},
    {"itemId": "i2", "title": "How did we do?",
     "questionItem": {"question": {"questionId": "q-rating", "required": True,
                                   "scaleQuestion": {"low": 1, "high": 5}}}},
    {"itemId": "i3", "title": "What did you buy?",
     "questionItem": {"question": {"questionId": "q-bought", "choiceQuestion": {
         "type": "CHECKBOX", "options": [{"value": "Chairs"}, {"value": "Desks"},
                                         {"value": "Paper"}, {"isOther": True}]}}}},
    {"itemId": "i4", "title": "Anything else?",
     "questionItem": {"question": {"questionId": "q-comments",
                                   "textQuestion": {"paragraph": True}}}},
    {"itemId": "i5", "title": "Receipt",
     "questionItem": {"question": {"questionId": "q-receipt", "fileUploadQuestion": {}}}},
    {"itemId": "i6", "title": "Delivery",
     "questionGroupItem": {
         "grid": {"columns": {"type": "RADIO", "options": [{"value": "Poor"}, {"value": "Good"}]}},
         "questions": [{"questionId": "q-speed", "rowQuestion": {"title": "Speed"}},
                       {"questionId": "q-packing", "rowQuestion": {"title": "Packaging"}}]}},
    {"itemId": "i7", "title": "Thank you", "textItem": {}},
]


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def forms():
    stub = GoogleFormsStub().start()
    stub.feedback = stub.add_form("Sidra customer feedback", ITEMS,
                                  description="Tell us how your order went.",
                                  modified="2026-09-09T08:00:00.000Z",
                                  linked_sheet_id="1sHt0001Sidra")
    stub.add_form("Workshop registration", [], modified="2026-09-02T08:00:00Z")
    stub.dana = stub.respond(stub.feedback, "2026-09-08T09:00:00.120Z", {
        "q-name": "Dana Haddad", "q-rating": "5", "q-bought": ["Chairs", "Desks"],
        "q-speed": "Good", "q-packing": "Good"}, email="dana@sidra.example")
    stub.omar = stub.respond(stub.feedback, "2026-09-08T11:30:00Z", {
        "q-name": "Omar Saleh", "q-rating": "3", "q-comments": "Late by a day. " * 60,
        "q-receipt": {"file": "receipt-4471.pdf"}})
    stub.layla = stub.respond(stub.feedback, "2026-09-09T07:45:10.5Z", {
        "q-name": "Layla Nasser", "q-rating": "4"})
    yield stub
    stub.stop()


def executor(forms, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"google_forms__google": forms.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["google_forms"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_forms"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_only_reads_google(self, agents):
        """Nothing here changes a form: reads at 0, the watch record at 1,
        and the check a schedule may run."""
        doc = agents["google_forms"].manifest.document
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in doc["tools"] for f in t["functions"]}
        assert levels == {"account.status": 0, "forms.find": 0, "forms.get": 0,
                          "responses.list": 0, "responses.watch": 1, "responses.new": 1}
        _, new = agents["google_forms"].manifest.function("google_forms.responses.new")
        assert new["schedulable"] is True and not new.get("llm")
        scopes = agents["google_forms"].manifest.resource("secrets", "google")["oauth"]["scopes"]
        assert "https://www.googleapis.com/auth/forms.responses.readonly" in scopes

    def test_status_reports_the_account(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.account.status", {})
        assert (status, result) == ("success", {"connected": True,
                                                "email": GoogleFormsStub.ACCOUNT})


class TestReading:
    def test_find_is_newest_first(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.forms.find", {})
        assert status == "success", result
        assert [f["name"] for f in result["forms"]] == ["Sidra customer feedback",
                                                        "Workshop registration"]
        only, _ = invoke(agents, ex, "google_forms.forms.find", {"name": "workshop"})
        assert [f["name"] for f in only["forms"]] == ["Workshop registration"]

    def test_get_names_every_question(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.forms.get", {"form_id": forms.feedback})
        assert status == "success", result
        assert result["title"] == "Sidra customer feedback"
        assert result["linked_sheet_id"] == "1sHt0001Sidra"
        assert result["responder_link"].endswith("/viewform")
        assert [(q["title"], q["kind"], q["required"]) for q in result["questions"]] == [
            ("Your name", "short_text", True), ("How did we do?", "scale", True),
            ("What did you buy?", "checkboxes", False), ("Anything else?", "paragraph", False),
            ("Receipt", "file_upload", False), ("Delivery — Speed", "grid", False),
            ("Delivery — Packaging", "grid", False)]
        assert result["questions"][2]["options"] == ["Chairs", "Desks", "Paper", "Other"]
        assert result["questions"][1]["options"] == ["1", "2", "3", "4", "5"]
        assert result["questions"][5]["options"] == ["Poor", "Good"]

    def test_responses_are_newest_first_keyed_by_title(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.responses.list", {"form_id": forms.feedback})
        assert status == "success", result
        assert result["total"] == 3 and result["cut_short"] is False
        assert [r["response_id"] for r in result["responses"]] == [forms.layla, forms.omar, forms.dana]
        dana = result["responses"][2]
        assert dana["email"] == "dana@sidra.example"
        assert dana["answers"] == {"Your name": "Dana Haddad", "How did we do?": "5",
                                   "What did you buy?": "Chairs, Desks",
                                   "Delivery — Speed": "Good", "Delivery — Packaging": "Good"}
        omar = result["responses"][1]["answers"]
        assert omar["Receipt"] == "file: receipt-4471.pdf"
        assert len(omar["Anything else?"]) == 501 and omar["Anything else?"].endswith("…")

    def test_responses_page_and_narrow_by_time(self, agents, forms):
        ex, _ = executor(forms)
        first, _ = invoke(agents, ex, "google_forms.responses.list",
                          {"form_id": forms.feedback, "max_results": 2})
        assert [r["response_id"] for r in first["responses"]] == [forms.layla, forms.omar]
        rest, _ = invoke(agents, ex, "google_forms.responses.list", {
            "form_id": forms.feedback, "max_results": 2, "page_token": first["next_page_token"]})
        assert [r["response_id"] for r in rest["responses"]] == [forms.dana]
        assert "next_page_token" not in rest

        since, status = invoke(agents, ex, "google_forms.responses.list", {
            "form_id": forms.feedback, "since": "2026-09-08T15:00:00+04:00"})
        assert status == "success", since
        assert [r["response_id"] for r in since["responses"]] == [forms.layla, forms.omar]
        assert forms.filters[-1] == "timestamp >= 2026-09-08T11:00:00Z"

    def test_a_bad_since_is_refused(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.responses.list",
                                {"form_id": forms.feedback, "since": "last week"})
        assert status == "error" and result["kind"] == "invalid"

    def test_an_unknown_form_is_not_found(self, agents, forms):
        ex, _ = executor(forms)
        result, status = invoke(agents, ex, "google_forms.forms.get", {"form_id": "1FaIpQLnope"})
        assert status == "error" and result["kind"] == "not_found"


class TestWatching:
    def test_a_watch_hands_on_each_response_exactly_once(self, agents, forms):
        ex, provider = executor(forms)
        watch, status = invoke(agents, ex, "google_forms.responses.watch",
                               {"form_id": forms.feedback, "note": "thank them"})
        assert status == "success", watch
        # From now, in the form's clock: the newest response marks the
        # spot and is not news itself.
        assert watch["since"] == "2026-09-09T07:45:10.5Z" and watch["responses_so_far"] == 3

        quiet, status = invoke(agents, ex, "google_forms.responses.new", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "responses": [], "closed": [], "more": False}
        assert forms.filters[-1] == "timestamp >= 2026-09-09T07:45:10.5Z"

        sam = forms.respond(forms.feedback, "2026-09-10T08:00:00Z",
                            {"q-name": "Sam Okafor", "q-rating": "2"})
        # A second response in the very same instant, with a longer
        # fraction: neither lost nor shown twice.
        nadia = forms.respond(forms.feedback, "2026-09-10T08:00:00.000Z",
                              {"q-name": "Nadia Rahman", "q-rating": "5"})
        news, _ = invoke(agents, ex, "google_forms.responses.new", {})
        assert sorted(r["response_id"] for r in news["responses"]) == sorted([sam, nadia])
        row = next(r for r in news["responses"] if r["response_id"] == sam)
        assert (row["watch_ref"], row["form"]) == (watch["watch_ref"], "Sidra customer feedback")
        assert row["answers"] == {"Your name": "Sam Okafor", "How did we do?": "2"}

        again, _ = invoke(agents, ex, "google_forms.responses.new", {})
        assert again["responses"] == []
        keys = provider.data["google_forms__watch"][watch["watch_ref"]]["keys"]
        assert set(keys["cursor_ids"].split(",")) == {sam, nadia}

        ali = forms.respond(forms.feedback, "2026-09-10T08:00:00.000000001Z", {"q-name": "Ali Hassan"})
        news, _ = invoke(agents, ex, "google_forms.responses.new", {})
        assert [r["response_id"] for r in news["responses"]] == [ali]

    def test_new_responses_are_oldest_first_and_page(self, agents, forms):
        ex, _ = executor(forms)
        watch, _ = invoke(agents, ex, "google_forms.responses.watch", {"form_id": forms.feedback})
        made = [forms.respond(forms.feedback, f"2026-09-1{n}T09:00:00Z", {"q-name": f"Guest {n}"})
                for n in range(3)]
        page, _ = invoke(agents, ex, "google_forms.responses.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["response_id"] for r in page["responses"]] == made[:2]
        assert page["more"] is True
        page, _ = invoke(agents, ex, "google_forms.responses.new",
                         {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [r["response_id"] for r in page["responses"]] == made[2:]
        assert page["more"] is False

    def test_an_edited_response_comes_again(self, agents, forms):
        ex, _ = executor(forms)
        invoke(agents, ex, "google_forms.responses.watch", {"form_id": forms.feedback})
        forms.edit(forms.feedback, forms.omar, "2026-09-11T10:00:00Z", "q-comments", "Sorted now.")
        news, _ = invoke(agents, ex, "google_forms.responses.new", {})
        [row] = news["responses"]
        assert row["response_id"] == forms.omar
        assert row["answers"]["Anything else?"] == "Sorted now."

    def test_a_form_with_no_responses_watches_from_now(self, agents, forms):
        ex, _ = executor(forms)
        empty = forms.add_form("Office move survey", ITEMS[:1])
        watch, status = invoke(agents, ex, "google_forms.responses.watch", {"form_id": empty})
        assert status == "success" and watch["responses_so_far"] == 0
        assert watch["since"].endswith("Z") and watch["since"] > "2026-01-01"
        first = forms.respond(empty, "2099-01-01T00:00:00Z", {"q-name": "Dana Haddad"})
        news, _ = invoke(agents, ex, "google_forms.responses.new", {"watch_ref": watch["watch_ref"]})
        assert [r["response_id"] for r in news["responses"]] == [first]

    def test_a_form_that_went_closes_its_watch(self, agents, forms):
        ex, provider = executor(forms)
        watch, _ = invoke(agents, ex, "google_forms.responses.watch", {"form_id": forms.feedback})
        del forms.forms[forms.feedback]
        result, status = invoke(agents, ex, "google_forms.responses.new", {})
        assert status == "success", result
        assert result["responses"] == [] and "closed" in result["closed"][0]["message"]
        assert provider.data["google_forms__watch"][watch["watch_ref"]]["keys"]["status"] == "closed"


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_records_nothing(self, agents, forms):
        ex, provider = executor(forms, access_token="expired")
        result, status = invoke(agents, ex, "google_forms.responses.list", {"form_id": forms.feedback})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "google_forms.responses.watch", {"form_id": forms.feedback})
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "google_forms.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Google account" in result["problem"]
