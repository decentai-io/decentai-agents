"""The Google Drive agent in a real worker against a loopback Drive
holding Sidra Office Supplies' files: a lease under Contracts, a budget
workbook under Finance, a proposal draft kept as a Google Doc at the
top of My Drive, and a showroom floor plan Dana shared.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gdrive_stub import GoogleDriveStub

DANA = "dana@sidra.example"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
BUDGET = b"PK\x03\x04 q3 budget workbook"
FLOOR_PLAN = b"%PDF-1.4\n% showroom floor plan\n"
NOTICE = b"%PDF-1.4\n% renewal notice\n"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def drive():
    stub = GoogleDriveStub().start()
    stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.add_file("/Finance", "Q3 budget.xlsx", BUDGET)
    stub.add_native("/", "Proposal draft", "document")
    stub.share_with_me("Showroom floor plan.pdf", FLOOR_PLAN, DANA)
    yield stub
    stub.stop()


def executor(drive, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"google_drive__google": drive.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["google_drive"], name, inputs, chat_level=chat_level))


def find_one(agents, ex, query):
    result, status = invoke(agents, ex, "google_drive.files.search", {"query": query})
    assert status == "success", result
    assert len(result["items"]) == 1, result
    return result["items"][0]


def attach(provider, filename, raw):
    """A file a person attached to the chat."""
    return run(provider.create_file("chat_attachment", filename, raw))["resource_ref"]


def downloaded(provider, result):
    return provider.files["google_drive__download"][result["file_ref"]]["content"]


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_drive"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_one_connection_serves_the_three_google_agents(self, agents):
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        drive = agents["google_drive"].manifest.resource("secrets", "google")
        for other in ("gmail", "google_calendar"):
            theirs = agents[other].manifest.resource("secrets", "google")
            assert strip(theirs["fields"]) == strip(drive["fields"]), other
            assert theirs["oauth"] == drive["oauth"], other

    def test_it_changes_the_drive_where_onedrive_does(self, agents):
        """The twins keep the same contract: the same functions, and the
        same ones wait for the user."""
        def levels(agent_id):
            return {f"{t['id']}.{f['id']}": f["permission_level"]
                    for t in agents[agent_id].manifest.document["tools"]
                    for f in t["functions"]}
        onedrive = levels("onedrive")
        onedrive.pop("files.move_many")          # OneDrive's alone, so far
        assert levels("google_drive") == onedrive

    def test_status_reports_the_drive(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "google_drive.account.status", {})
        assert status == "success", result
        assert result["connected"] is True and result["email"] == GoogleDriveStub.ACCOUNT
        assert result["used_bytes"] == len(LEASE) + len(BUDGET)


class TestFinding:
    def test_search_finds_a_file_and_names_its_folder(self, agents, drive):
        ex, _ = executor(drive)
        row = find_one(agents, ex, "lease")
        assert row["name"] == "Riverside office lease.pdf"
        assert (row["folder"], row["kind"], row["mime_type"], row["size"]) == (
            "Contracts", "file", "application/pdf", len(LEASE))

    def test_a_query_with_a_quote_in_it_is_escaped(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "google_drive.files.search", {"query": "Dana's"})
        assert status == "success" and result["items"] == [], result

    def test_a_folder_is_listed_by_path_folders_first(self, agents, drive):
        ex, _ = executor(drive)
        top, _ = invoke(agents, ex, "google_drive.files.list", {})
        assert [(i["name"], i["kind"]) for i in top["items"]] == [
            ("Contracts", "folder"), ("Finance", "folder"), ("Proposal draft", "file")]
        contracts, _ = invoke(agents, ex, "google_drive.files.list", {"folder_path": "/Contracts/"})
        assert [i["name"] for i in contracts["items"]] == ["Riverside office lease.pdf"]

    def test_paging_follows_the_page_token(self, agents, drive):
        ex, _ = executor(drive)
        first, _ = invoke(agents, ex, "google_drive.files.list", {"max_results": 2})
        assert len(first["items"]) == 2 and first["next_page_token"]
        rest, _ = invoke(agents, ex, "google_drive.files.list", {
            "max_results": 2, "page_token": first["next_page_token"]})
        assert [i["name"] for i in rest["items"]] == ["Proposal draft"]

    def test_a_folder_that_is_not_there_is_named(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "google_drive.files.list",
                                {"folder_path": "/Contracts/Nope"})
        assert status == "error" and "/Contracts/Nope" in result["error"]

    def test_shared_with_me_says_who_shared(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "google_drive.files.shared_with_me", {})
        assert status == "success", result
        [row] = result["items"]
        assert (row["name"], row["shared_by"]) == ("Showroom floor plan.pdf", DANA)


class TestFetching:
    def test_a_download_is_a_platform_file_of_the_same_bytes(self, agents, drive):
        ex, provider = executor(drive)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "google_drive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Riverside office lease.pdf"
        assert downloaded(provider, result) == LEASE

    def test_a_google_doc_comes_in_as_word_or_as_pdf(self, agents, drive):
        ex, provider = executor(drive)
        row = find_one(agents, ex, "proposal")
        word, _ = invoke(agents, ex, "google_drive.files.download",
                         {"item_id": row["item_id"]}, chat_level=2)
        assert word["filename"] == "Proposal draft.docx"
        assert downloaded(provider, word).startswith(b"PK")
        pdf, _ = invoke(agents, ex, "google_drive.files.download",
                        {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert pdf["filename"] == "Proposal draft.pdf"
        assert downloaded(provider, pdf).startswith(b"%PDF")

    def test_only_googles_own_formats_convert(self, agents, drive):
        ex, provider = executor(drive)
        lease = find_one(agents, ex, "lease")
        same, status = invoke(agents, ex, "google_drive.files.download",
                              {"item_id": lease["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "success" and downloaded(provider, same) == LEASE
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "google_drive.files.download",
                                {"item_id": budget["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "error" and "Google Docs, Sheets" in result["error"]

    def test_a_file_too_large_to_store_is_refused_before_it_is_fetched(
            self, agents, drive):
        """The platform stores at most 25 MiB. Over the limit the worker was
        killed mid-call: a progress line, then nothing. It is refused with
        a sentence now, and the bytes are never asked for."""
        drive.add_file("/Finance", "Scanned archive.pdf",
                       b"%PDF-1.4 " + b"x" * (26 * 1024 * 1024))
        ex, provider = executor(drive)
        row = find_one(agents, ex, "Scanned archive")
        result, status = invoke(agents, ex, "google_drive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "too_large"
        assert result["limit"] == 25 * 1024 * 1024
        assert not provider.files.get("google_drive__download")

    def test_a_file_within_the_limit_still_comes_through(self, agents, drive):
        """The guard must not swallow an ordinary file."""
        ex, provider = executor(drive)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "google_drive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert downloaded(provider, result) == LEASE

    def test_a_folder_cannot_be_downloaded(self, agents, drive):
        ex, _ = executor(drive)
        top, _ = invoke(agents, ex, "google_drive.files.list", {})
        contracts = next(i for i in top["items"] if i["name"] == "Contracts")
        result, status = invoke(agents, ex, "google_drive.files.download",
                                {"item_id": contracts["item_id"]}, chat_level=2)
        assert status == "error" and "folder" in result["error"]

    def test_a_shared_file_is_fetched_by_its_id(self, agents, drive):
        ex, provider = executor(drive)
        shared, _ = invoke(agents, ex, "google_drive.files.shared_with_me", {})
        result, status = invoke(agents, ex, "google_drive.files.download", {
            "item_id": shared["items"][0]["item_id"]}, chat_level=2)
        assert status == "success", result
        assert downloaded(provider, result) == FLOOR_PLAN


class TestChanging:
    def test_an_upload_never_makes_a_second_file_of_the_same_name(self, agents, drive):
        ex, provider = executor(drive)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        names = []
        for _ in range(2):
            result, status = invoke(agents, ex, "google_drive.files.upload", {
                "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
            assert status == "success", result
            names.append(result["item"]["name"])
            assert result["item"]["folder"] == "Contracts"
        assert names == ["renewal notice.pdf", "renewal notice 1.pdf"]
        assert drive.by_path("/Contracts/renewal notice 1.pdf")["_content"] == NOTICE

    def test_replace_is_a_new_version_of_the_same_file(self, agents, drive):
        ex, provider = executor(drive)
        lease = find_one(agents, ex, "lease")
        ref = attach(provider, "Riverside office lease.pdf", NOTICE)
        result, status = invoke(agents, ex, "google_drive.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts", "on_conflict": "replace"}, chat_level=3)
        assert status == "success", result
        assert result["item"]["item_id"] == lease["item_id"]
        assert drive.files[lease["item_id"]]["_content"] == NOTICE

    def test_a_folder_is_made_and_a_file_moved_and_renamed_into_it(self, agents, drive):
        ex, _ = executor(drive)
        made, status = invoke(agents, ex, "google_drive.files.create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "success", made
        assert (made["item"]["kind"], made["item"]["folder"]) == ("folder", "Contracts")
        again, status = invoke(agents, ex, "google_drive.files.create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and "already" in again["error"]
        lease = find_one(agents, ex, "lease")
        moved, status = invoke(agents, ex, "google_drive.files.move", {
            "item_id": lease["item_id"], "to_folder_path": "/Contracts/2026",
            "new_name": "Riverside lease 2024-2027.pdf"}, chat_level=3)
        assert status == "success", moved
        assert (moved["item"]["name"], moved["item"]["folder"]) == (
            "Riverside lease 2024-2027.pdf", "2026")
        assert drive.by_path("/Contracts/2026/Riverside lease 2024-2027.pdf") is not None
        assert drive.by_path("/Contracts/Riverside office lease.pdf") is None

    def test_delete_goes_to_the_trash_never_further(self, agents, drive):
        ex, _ = executor(drive)
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "google_drive.files.delete",
                                {"item_id": budget["item_id"]}, chat_level=3)
        assert status == "success", result
        assert result["deleted"] is True and "trash" in result["note"]
        assert drive.files[budget["item_id"]]["trashed"] is True
        assert drive.deleted_for_good == []
        after, _ = invoke(agents, ex, "google_drive.files.search", {"query": "budget"})
        assert after["items"] == []

    def test_an_upload_that_gets_no_answer_is_unknown_and_not_retried(self, agents, drive):
        drive.drop_upload = True
        ex, provider = executor(drive)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        result, status = invoke(agents, ex, "google_drive.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert drive.uploads == ["renewal notice.pdf"]


class TestSharing:
    def test_a_person_given_access_shows_in_who_has_access_until_revoked(self, agents, drive):
        ex, provider = executor(drive)
        lease = find_one(agents, ex, "lease")
        shared, status = invoke(agents, ex, "google_drive.sharing.invite", {
            "item_id": lease["item_id"], "recipients": [DANA], "role": "write"}, chat_level=3)
        assert status == "success", shared
        assert shared["invited"] == [DANA]
        access, _ = invoke(agents, ex, "google_drive.sharing.permissions", {"item_id": lease["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("write", "invite")
        revoked, status = invoke(agents, ex, "google_drive.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "success" and revoked["revoked"] is True, revoked
        access, _ = invoke(agents, ex, "google_drive.sharing.permissions", {"item_id": lease["item_id"]})
        assert [p["who"] for p in access["permissions"]] == [GoogleDriveStub.ACCOUNT]
        statuses = [r["keys"]["status"] for r in provider.data["google_drive__share"].values()]
        assert statuses == ["granted", "revoked"]

    def test_a_link_opens_it_to_the_organization_unless_asked(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        made, status = invoke(agents, ex, "google_drive.sharing.link",
                              {"item_id": lease["item_id"]}, chat_level=3)
        assert status == "success", made
        assert made["reach"] == "people at sidra.example"
        assert made["link"] == lease["link"]
        granted = drive.perms[lease["item_id"]][-1]
        assert (granted["type"], granted["domain"], granted["role"]) == ("domain", "sidra.example", "reader")

    def test_the_owner_cannot_be_removed(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        access, _ = invoke(agents, ex, "google_drive.sharing.permissions", {"item_id": lease["item_id"]})
        owner = next(p for p in access["permissions"] if p["via"] == "owner")
        result, status = invoke(agents, ex, "google_drive.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": owner["permission_id"]}, chat_level=3)
        assert status == "error" and "owner" in result["error"]

    def test_a_name_is_not_an_address(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "google_drive.sharing.invite", {
            "item_id": lease["item_id"], "recipients": ["Dana"]}, chat_level=3)
        assert status == "error" and "email" in result["error"]

    def test_an_expired_connection_says_reconnect(self, agents, drive):
        ex, _ = executor(drive, access_token="stale")
        result, status = invoke(agents, ex, "google_drive.files.search", {"query": "lease"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"].lower()
