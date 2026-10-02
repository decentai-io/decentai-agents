"""The Box agent in a real worker against a loopback Box holding Sidra
Office Supplies' files: a lease under Contracts, a budget workbook under
Finance, a proposal draft at the top level, and a showroom floor plan
Dana owns and shared with the account.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.box_stub import BoxStub

DANA = "dana@sidra.example"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
BUDGET = b"PK\x03\x04 q3 budget workbook"
PROPOSAL = b"PK\x03\x04 proposal draft"
FLOOR_PLAN = b"%PDF-1.4\n% showroom floor plan\n"
NOTICE = b"%PDF-1.4\n% renewal notice\n"
INVOICE = b"%PDF-1.4\n% invoice INV-2209\n"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def box():
    stub = BoxStub().start()
    stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.add_file("/Finance", "Q3 budget.xlsx", BUDGET)
    stub.add_file("/", "Proposal draft.docx", PROPOSAL)
    stub.share_with_me("Showroom floor plan.pdf", FLOOR_PLAN, DANA)
    yield stub
    stub.stop()


def executor(box, access_token="at-1"):
    provider = InMemoryResourceProvider(secrets={"box__box": box.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["box"], name, inputs, chat_level=chat_level))


def find_one(agents, ex, query):
    result, status = invoke(agents, ex, "box.files.search", {"query": query})
    assert status == "success", result
    assert len(result["items"]) == 1, result
    return result["items"][0]


def attach(provider, filename, raw):
    """A file a person attached to the chat."""
    return run(provider.create_file("chat_attachment", filename, raw))["resource_ref"]


def downloaded(provider, result):
    return provider.files["box__download"][result["file_ref"]]["content"]


def levels(agents, agent_id):
    return {f"{t['id']}.{f['id']}": f["permission_level"]
            for t in agents[agent_id].manifest.document["tools"] for f in t["functions"]}


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["box"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_keeps_the_dropbox_twins_contract(self, agents):
        """The same functions at the same levels as OneDrive and Dropbox;
        the change watch runs unattended, so it reads and records only."""
        assert levels(agents, "box") == levels(agents, "dropbox")
        onedrive = levels(agents, "onedrive")
        onedrive.pop("files.move_many")          # OneDrive's alone, so far
        assert levels(agents, "box") == {**onedrive, "changes.watch": 1, "changes.new": 1}
        _, new = agents["box"].manifest.function("box.changes.new")
        assert new["schedulable"] is True and not new.get("llm")

    def test_the_secret_is_a_box_sign_in(self, agents):
        secret = agents["box"].manifest.resource("secrets", "box")
        oauth = secret["oauth"]
        assert (oauth["provider"], oauth["scopes"]) == ("box", ["root_readwrite"])
        assert oauth["identity"] == {"url": "https://api.box.com/2.0/users/me", "field": "login"}
        assert [f["name"] for f in secret["fields"]] == ["api_base_url"]

    def test_every_file_slot_is_capped_and_no_list_passes_twenty_five(self, agents):
        document = agents["box"].manifest.document
        assert {f["id"]: f["constraints"]["max_size_mb"]
                for f in document["resources"]["files"]} == {"download": 25, "outgoing": 25}
        for tool in document["tools"]:
            for function in tool["functions"]:
                most = function["inputs"].get("properties", {}).get("max_results")
                if most:
                    assert most["maximum"] <= 25, function["id"]

    def test_status_reports_the_account(self, agents, box):
        ex, _ = executor(box)
        result, status = invoke(agents, ex, "box.account.status", {})
        assert status == "success", result
        assert result["connected"] is True and result["email"] == BoxStub.ACCOUNT
        assert result["enterprise"] == "Sidra Office Supplies"
        assert result["used_bytes"] == len(LEASE) + len(BUDGET) + len(PROPOSAL)


class TestFinding:
    def test_search_finds_a_file_and_says_where_it_is(self, agents, box):
        ex, _ = executor(box)
        row = find_one(agents, ex, "lease")
        assert row["name"] == "Riverside office lease.pdf"
        assert (row["folder"], row["path"], row["kind"], row["mime_type"], row["size"]) == (
            "/Contracts", "/Contracts/Riverside office lease.pdf", "file",
            "application/pdf", len(LEASE))

    def test_a_folder_is_listed_by_path_folders_first(self, agents, box):
        ex, _ = executor(box)
        top, status = invoke(agents, ex, "box.files.list", {})
        assert status == "success", top
        assert [(i["name"], i["kind"]) for i in top["items"]] == [
            ("Contracts", "folder"), ("Finance", "folder"), ("Proposal draft.docx", "file"),
            ("Showroom floor plan.pdf", "file")]
        contracts, _ = invoke(agents, ex, "box.files.list", {"folder_path": "/Contracts/"})
        assert [i["name"] for i in contracts["items"]] == ["Riverside office lease.pdf"]
        by_id, _ = invoke(agents, ex, "box.files.list", {"folder_id": top["items"][0]["item_id"]})
        assert by_id["items"] == contracts["items"]

    def test_paging_follows_the_page_token(self, agents, box):
        ex, _ = executor(box)
        first, _ = invoke(agents, ex, "box.files.list", {"max_results": 3})
        assert len(first["items"]) == 3 and first["next_page_token"]
        rest, _ = invoke(agents, ex, "box.files.list", {"page_token": first["next_page_token"]})
        assert [i["name"] for i in rest["items"]] == ["Showroom floor plan.pdf"]
        assert "next_page_token" not in rest

    def test_a_folder_that_is_not_there_is_named(self, agents, box):
        ex, _ = executor(box)
        result, status = invoke(agents, ex, "box.files.list", {"folder_path": "/Contracts/Nope"})
        assert status == "error" and result["kind"] == "not_found"
        assert "/Contracts/Nope" in result["error"]

    def test_shared_with_me_says_who_shared(self, agents, box):
        ex, _ = executor(box)
        result, status = invoke(agents, ex, "box.files.shared_with_me", {})
        assert status == "success", result
        [row] = result["items"]
        assert (row["name"], row["shared_by"], row["shared"]) == (
            "Showroom floor plan.pdf", DANA, True)

    def test_a_folder_id_is_found_when_given_bare(self, agents, box):
        ex, _ = executor(box)
        folder = box.by_path("/Finance")
        result, status = invoke(agents, ex, "box.files.get", {"item_id": folder["id"]})
        assert status == "success", result
        assert (result["item"]["kind"], result["item"]["name"]) == ("folder", "Finance")


class TestFetching:
    def test_a_download_follows_the_redirect_to_the_same_bytes(self, agents, box):
        ex, provider = executor(box)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "box.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Riverside office lease.pdf"
        assert downloaded(provider, result) == LEASE

    def test_an_office_file_comes_in_as_pdf_on_request(self, agents, box):
        ex, provider = executor(box)
        row = find_one(agents, ex, "proposal")
        result, status = invoke(agents, ex, "box.files.download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Proposal draft.pdf"
        assert downloaded(provider, result).startswith(b"%PDF")

    def test_a_pdf_box_cannot_make_is_said_so(self, agents, box):
        box.pdf_state = "error"
        ex, provider = executor(box)
        row = find_one(agents, ex, "proposal")
        result, status = invoke(agents, ex, "box.files.download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "error" and "PDF" in result["error"]
        assert not provider.files.get("box__download")

    def test_a_file_too_large_to_store_is_refused_before_it_is_fetched(self, agents, box):
        """The platform stores at most 25 MiB. Box's metadata carries the
        size, so the bytes are never asked for."""
        box.add_file("/Finance", "Scanned archive.pdf", b"%PDF-1.4 " + b"x" * (26 * 1024 * 1024))
        ex, provider = executor(box)
        row = find_one(agents, ex, "Scanned archive")
        result, status = invoke(agents, ex, "box.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "too_large" and result["limit"] == 25 * 1024 * 1024
        assert f"{result['size'] // 1024:,} KB" in result["error"]
        assert box.downloads == []
        assert not provider.files.get("box__download")

    def test_a_folder_cannot_be_downloaded(self, agents, box):
        ex, _ = executor(box)
        result, status = invoke(agents, ex, "box.files.download",
                                {"item_id": box.by_path("/Contracts")["id"]}, chat_level=2)
        assert status == "error" and "folder" in result["error"]

    def test_a_shared_file_is_fetched_by_its_id(self, agents, box):
        ex, provider = executor(box)
        shared, _ = invoke(agents, ex, "box.files.shared_with_me", {})
        result, status = invoke(agents, ex, "box.files.download", {
            "item_id": shared["items"][0]["item_id"]}, chat_level=2)
        assert status == "success", result
        assert downloaded(provider, result) == FLOOR_PLAN


class TestChanging:
    def test_an_upload_never_makes_a_second_file_of_the_same_name(self, agents, box):
        ex, provider = executor(box)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        names = []
        for _ in range(2):
            result, status = invoke(agents, ex, "box.files.upload", {
                "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
            assert status == "success", result
            names.append(result["item"]["name"])
            assert result["item"]["folder"] == "/Contracts"
        assert names == ["renewal notice.pdf", "renewal notice 1.pdf"]
        assert box.by_path("/Contracts/renewal notice 1.pdf")["_content"] == NOTICE

    def test_fail_refuses_a_taken_name_and_replace_is_a_new_version(self, agents, box):
        ex, provider = executor(box)
        lease = find_one(agents, ex, "lease")
        ref = attach(provider, "Riverside office lease.pdf", NOTICE)
        refused, status = invoke(agents, ex, "box.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts", "on_conflict": "fail"}, chat_level=3)
        assert status == "error" and "Riverside office lease.pdf" in refused["error"]
        assert box.uploads == []
        result, status = invoke(agents, ex, "box.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts", "on_conflict": "replace"}, chat_level=3)
        assert status == "success", result
        assert result["item"]["item_id"] == lease["item_id"]
        assert box.items[lease["item_id"]]["_content"] == NOTICE

    def test_a_folder_is_made_and_a_file_moved_and_renamed_into_it(self, agents, box):
        ex, _ = executor(box)
        made, status = invoke(agents, ex, "box.files.create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "success", made
        assert (made["item"]["kind"], made["item"]["folder"]) == ("folder", "/Contracts")
        again, status = invoke(agents, ex, "box.files.create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and again["kind"] == "http"
        assert "already has an item named '2026'" in again["error"]
        lease = find_one(agents, ex, "lease")
        moved, status = invoke(agents, ex, "box.files.move", {
            "item_id": lease["item_id"], "to_folder_path": "/Contracts/2026",
            "new_name": "Riverside lease 2024-2027.pdf"}, chat_level=3)
        assert status == "success", moved
        assert (moved["item"]["name"], moved["item"]["folder"]) == (
            "Riverside lease 2024-2027.pdf", "/Contracts/2026")
        assert box.by_path("/Contracts/2026/Riverside lease 2024-2027.pdf") is not None
        assert box.by_path("/Contracts/Riverside office lease.pdf") is None

    def test_delete_goes_to_the_trash(self, agents, box):
        ex, _ = executor(box)
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "box.files.delete",
                                {"item_id": budget["item_id"]}, chat_level=3)
        assert status == "success", result
        assert result["deleted"] is True and "trash" in result["note"]
        assert box.items[budget["item_id"]]["item_status"] == "trashed"
        after, _ = invoke(agents, ex, "box.files.search", {"query": "budget"})
        assert after["items"] == []

    def test_an_upload_that_gets_no_answer_is_unknown_and_not_retried(self, agents, box):
        box.drop_upload = True
        ex, provider = executor(box)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        result, status = invoke(agents, ex, "box.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert box.uploads == ["renewal notice.pdf"]


class TestSharing:
    def test_a_person_given_access_shows_in_who_has_access_until_revoked(self, agents, box):
        ex, provider = executor(box)
        lease = find_one(agents, ex, "lease")
        shared, status = invoke(agents, ex, "box.sharing.invite", {
            "item_id": lease["item_id"], "recipients": [DANA], "role": "write"}, chat_level=3)
        assert status == "success", shared
        assert shared["invited"] == [DANA]
        access, _ = invoke(agents, ex, "box.sharing.permissions", {"item_id": lease["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("write", "invite")
        revoked, status = invoke(agents, ex, "box.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "success" and revoked["revoked"] is True, revoked
        access, _ = invoke(agents, ex, "box.sharing.permissions", {"item_id": lease["item_id"]})
        assert [p["who"] for p in access["permissions"]] == [BoxStub.ACCOUNT]
        statuses = [r["keys"]["status"] for r in provider.data["box__share"].values()]
        assert statuses == ["granted", "revoked"]

    def test_access_given_on_a_folder_above_is_inherited_and_not_revoked_here(self, agents, box):
        box.collaborate(box.by_path("/Finance")["id"], DANA, "viewer")
        ex, _ = executor(box)
        budget = find_one(agents, ex, "budget")
        access, _ = invoke(agents, ex, "box.sharing.permissions", {"item_id": budget["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("read", "inherited")
        result, status = invoke(agents, ex, "box.sharing.revoke", {
            "item_id": budget["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "error" and "folder above" in result["error"]

    def test_a_link_is_for_the_company_unless_asked(self, agents, box):
        ex, provider = executor(box)
        lease = find_one(agents, ex, "lease")
        made, status = invoke(agents, ex, "box.sharing.link",
                              {"item_id": lease["item_id"]}, chat_level=3)
        assert status == "success", made
        assert made["reach"] == "people at Sidra Office Supplies"
        assert made["link"].startswith("https://app.box.com/s/")
        assert box.items[lease["item_id"]]["shared_link"]["access"] == "company"
        wider, status = invoke(agents, ex, "box.sharing.link", {
            "item_id": lease["item_id"], "link_scope": "anonymous"}, chat_level=3)
        assert status == "success" and wider["reach"] == "anyone with the link"
        assert box.items[lease["item_id"]]["shared_link"]["access"] == "open"
        revoked, status = invoke(agents, ex, "box.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": "link"}, chat_level=3)
        assert status == "success", revoked
        assert "shared_link" not in box.items[lease["item_id"]]

    def test_a_personal_account_is_asked_rather_than_widened(self, agents):
        stub = BoxStub(enterprise=False).start()
        try:
            item_id = stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
            ex, _ = executor(stub)
            result, status = invoke(agents, ex, "box.sharing.link", {"item_id": item_id},
                                    chat_level=3)
            assert status == "error" and "anyone with the link" in result["error"]
            assert "shared_link" not in stub.items[item_id]
        finally:
            stub.stop()

    def test_the_owner_cannot_be_removed(self, agents, box):
        ex, _ = executor(box)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "box.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": "owner"}, chat_level=3)
        assert status == "error" and "owner" in result["error"]

    def test_a_name_is_not_an_address(self, agents, box):
        ex, _ = executor(box)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "box.sharing.invite", {
            "item_id": lease["item_id"], "recipients": ["Dana"]}, chat_level=3)
        assert status == "error" and "email" in result["error"]


class TestWatchingChanges:
    def test_a_watch_hands_on_an_upload_and_a_trash_exactly_once(self, agents, box):
        ex, provider = executor(box)
        watch, status = invoke(agents, ex, "box.changes.watch", {"note": "file invoices"},
                               chat_level=1)
        assert status == "success", watch
        quiet, status = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert status == "success", quiet
        assert quiet == {"checked": 1, "changes": [], "more": False}

        box.add_file("/Finance", "Invoice INV-2209.pdf", INVOICE, by=DANA)
        box.trash(box.by_path("/Finance/Q3 budget.xlsx")["id"], by=DANA)

        news, _ = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert [(c["change"], c["path"], c["modified_by"]) for c in news["changes"]] == [
            ("uploaded", "/Finance/Invoice INV-2209.pdf", DANA),
            ("trashed", "/Finance/Q3 budget.xlsx", DANA)]
        assert news["changes"][0]["size"] == len(INVOICE)
        assert news["changes"][0]["watch_ref"] == watch["watch_ref"] and news["more"] is False

        again, _ = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert again["changes"] == []
        [record] = provider.data["box__watch"].values()
        assert record["keys"]["status"] == "watching"

    def test_an_event_box_delivers_twice_is_handed_on_once(self, agents, box):
        ex, _ = executor(box)
        invoke(agents, ex, "box.changes.watch", {}, chat_level=1)
        box.add_file("/Finance", "Invoice INV-2209.pdf", INVOICE)
        uploaded = box.events[-1]
        box.redeliver(uploaded)                     # twice in the same chunk
        news, _ = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert [c["event_id"] for c in news["changes"]] == [uploaded["event_id"]]
        box.redeliver(uploaded)                     # and again, a check later
        again, _ = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert again["changes"] == []

    def test_what_is_not_about_a_file_or_folder_is_not_news(self, agents, box):
        ex, _ = executor(box)
        invoke(agents, ex, "box.changes.watch", {}, chat_level=1)
        box.events.append({"type": "event", "event_id": "evt-login", "event_type": "ITEM_PREVIEW",
                           "created_at": "2026-09-01T11:00:00-07:00",
                           "source": box.view(box.by_path("/Contracts/Riverside office lease.pdf"))})
        news, _ = invoke(agents, ex, "box.changes.new", {}, chat_level=1)
        assert news["changes"] == []

    def test_a_full_chunk_says_there_is_more(self, agents, box):
        ex, _ = executor(box)
        invoke(agents, ex, "box.changes.watch", {}, chat_level=1)
        for n in range(3):
            box.add_file("/Finance", f"Receipt {n}.pdf", INVOICE)
        first, _ = invoke(agents, ex, "box.changes.new", {"max_results": 2}, chat_level=1)
        assert [c["name"] for c in first["changes"]] == ["Receipt 0.pdf", "Receipt 1.pdf"]
        assert first["more"] is True
        rest, _ = invoke(agents, ex, "box.changes.new", {"max_results": 2}, chat_level=1)
        assert [c["name"] for c in rest["changes"]] == ["Receipt 2.pdf"]
        assert rest["more"] is False


class TestTheConnection:
    def test_an_expired_token_says_reconnect_and_writes_nothing(self, agents, box):
        ex, provider = executor(box, access_token="expired")
        result, status = invoke(agents, ex, "box.files.search", {"query": "lease"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "box.changes.watch", {}, chat_level=1)
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "box.sharing.invite", {
            "item_id": box.by_path("/Contracts")["id"], "recipients": [DANA]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {}

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "box.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Box account" in result["problem"]

    def test_box_asking_to_slow_down_is_its_own_kind(self, agents, box):
        box.rate_limited = True
        ex, _ = executor(box)
        result, status = invoke(agents, ex, "box.files.search", {"query": "lease"})
        assert status == "error" and result["kind"] == "rate_limited"
        assert "30 seconds" in result["error"]
