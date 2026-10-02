"""The OneDrive agent in a real worker against a loopback Microsoft
Graph drive holding Sidra Office Supplies' files: a lease under
Contracts, a budget workbook under Finance, a proposal draft at the top
level, and a showroom floor plan Dana shared from her own drive.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_drive_stub import GraphDriveStub

DANA = "dana@sidra.example"
LEASE = b"%PDF-1.4\n% Riverside office lease\n"
BUDGET = b"PK\x03\x04 q3 budget workbook"
PROPOSAL = b"PK\x03\x04 proposal draft"
FLOOR_PLAN = b"%PDF-1.4\n% showroom floor plan\n"
NOTICE = b"%PDF-1.4\n% renewal notice\n"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def drive():
    stub = GraphDriveStub().start()
    stub.add_file("/Contracts", "Riverside office lease.pdf", LEASE)
    stub.add_file("/Finance", "Q3 budget.xlsx", BUDGET)
    stub.add_file("/", "Proposal draft.docx", PROPOSAL)
    stub.share_with_me("Showroom floor plan.pdf", FLOOR_PLAN, DANA)
    yield stub
    stub.stop()


def executor(drive, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"onedrive__microsoft": drive.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["onedrive"], name, inputs, chat_level=chat_level))


def find_one(agents, ex, query):
    result, status = invoke(agents, ex, "onedrive.files.search", {"query": query})
    assert status == "success", result
    assert len(result["items"]) == 1, result
    return result["items"][0]


def attach(provider, filename, raw):
    """A file a person attached to the chat."""
    return run(provider.create_file("chat_attachment", filename, raw))["resource_ref"]


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["onedrive"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_one_connection_serves_the_three_microsoft_agents(self, agents):
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        drive = agents["onedrive"].manifest.resource("secrets", "microsoft")
        for other in ("outlook", "microsoft_calendar"):
            theirs = agents[other].manifest.resource("secrets", "microsoft")
            assert strip(theirs["fields"]) == strip(drive["fields"]), other
            assert theirs["oauth"] == drive["oauth"], other

    def test_every_change_to_the_drive_is_level_three(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["onedrive"].manifest.document["tools"]
                  for f in t["functions"]}
        assert {name for name, level in levels.items() if level == 3} == {
            "files.upload", "files.create_folder", "files.move", "files.move_many", "files.delete",
            "sharing.invite", "sharing.link", "sharing.revoke"}

    def test_status_reports_the_drive(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "onedrive.account.status", {})
        assert status == "success", result
        assert result["connected"] is True and result["email"] == GraphDriveStub.ACCOUNT
        assert result["drive_type"] == "business"
        assert result["used_bytes"] == len(LEASE) + len(BUDGET) + len(PROPOSAL)


class TestFinding:
    def test_search_finds_a_file_and_says_where_it_is(self, agents, drive):
        ex, _ = executor(drive)
        row = find_one(agents, ex, "lease")
        assert row["name"] == "Riverside office lease.pdf"
        assert (row["folder"], row["kind"], row["mime_type"], row["size"]) == (
            "/Contracts", "file", "application/pdf", len(LEASE))

    def test_a_folder_is_listed_by_path(self, agents, drive):
        ex, _ = executor(drive)
        top, _ = invoke(agents, ex, "onedrive.files.list", {})
        assert [(i["name"], i["kind"]) for i in top["items"]] == [
            ("Contracts", "folder"), ("Finance", "folder"), ("Proposal draft.docx", "file")]
        contracts, _ = invoke(agents, ex, "onedrive.files.list", {"folder_path": "/Contracts/"})
        assert [i["name"] for i in contracts["items"]] == ["Riverside office lease.pdf"]

    def test_paging_follows_microsofts_next_link(self, agents, drive):
        ex, _ = executor(drive)
        first, _ = invoke(agents, ex, "onedrive.files.list", {"max_results": 2})
        assert len(first["items"]) == 2 and first["next_page_token"].startswith("http")
        rest, _ = invoke(agents, ex, "onedrive.files.list", {"page_token": first["next_page_token"]})
        assert [i["name"] for i in rest["items"]] == ["Proposal draft.docx"]

    def test_a_folder_that_is_not_there_is_named(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "onedrive.files.list", {"folder_path": "/Nope"})
        assert status == "error" and "/Nope" in result["error"]

    def test_shared_with_me_carries_the_owners_drive(self, agents, drive):
        ex, _ = executor(drive)
        result, status = invoke(agents, ex, "onedrive.files.shared_with_me", {})
        assert status == "success", result
        [row] = result["items"]
        assert (row["name"], row["drive_id"], row["shared_by"]) == (
            "Showroom floor plan.pdf", "b!dana", DANA)


class TestFetching:
    def test_a_download_is_a_platform_file_of_the_same_bytes(self, agents, drive):
        ex, provider = executor(drive)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Riverside office lease.pdf"
        assert provider.files["onedrive__download"][result["file_ref"]]["content"] == LEASE

    def test_an_office_file_comes_in_as_pdf_on_request(self, agents, drive):
        ex, provider = executor(drive)
        row = find_one(agents, ex, "proposal")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "success", result
        assert result["filename"] == "Proposal draft.pdf"
        assert provider.files["onedrive__download"][result["file_ref"]]["content"].startswith(b"%PDF")

    def test_a_pdf_is_not_converted_again(self, agents, drive):
        ex, provider = executor(drive)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": row["item_id"], "as_pdf": True}, chat_level=2)
        assert status == "success", result
        assert provider.files["onedrive__download"][result["file_ref"]]["content"] == LEASE

    def test_a_file_too_large_to_store_is_refused_before_it_is_fetched(
            self, agents, drive):
        """The platform stores at most 25 MiB. Over the limit the worker was
        killed mid-call: a progress line, then nothing. It is refused with
        a sentence now, and the bytes are never asked for."""
        drive.add_file("/Finance", "Scanned archive.pdf",
                       b"%PDF-1.4 " + b"x" * (26 * 1024 * 1024))
        ex, provider = executor(drive)
        row = find_one(agents, ex, "Scanned archive")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "error", result
        assert result["kind"] == "too_large"
        assert result["limit"] == 25 * 1024 * 1024
        assert not provider.files.get("onedrive__download")

    def test_a_file_within_the_limit_still_comes_through(self, agents, drive):
        """The guard must not swallow an ordinary file."""
        ex, provider = executor(drive)
        row = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": row["item_id"]}, chat_level=2)
        assert status == "success", result
        assert provider.files["onedrive__download"][result["file_ref"]]["content"] == LEASE

    def test_a_folder_cannot_be_downloaded(self, agents, drive):
        ex, _ = executor(drive)
        top, _ = invoke(agents, ex, "onedrive.files.list", {})
        contracts = next(i for i in top["items"] if i["name"] == "Contracts")
        result, status = invoke(agents, ex, "onedrive.files.download",
                                {"item_id": contracts["item_id"]}, chat_level=2)
        assert status == "error" and "folder" in result["error"]

    def test_a_shared_file_is_fetched_from_its_owners_drive(self, agents, drive):
        ex, provider = executor(drive)
        shared, _ = invoke(agents, ex, "onedrive.files.shared_with_me", {})
        row = shared["items"][0]
        result, status = invoke(agents, ex, "onedrive.files.download", {
            "item_id": row["item_id"], "drive_id": row["drive_id"]}, chat_level=2)
        assert status == "success", result
        assert provider.files["onedrive__download"][result["file_ref"]]["content"] == FLOOR_PLAN


class TestChanging:
    def test_an_upload_never_overwrites_by_default(self, agents, drive):
        ex, provider = executor(drive)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        names = []
        for _ in range(2):
            result, status = invoke(agents, ex, "onedrive.files.upload", {
                "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
            assert status == "success", result
            names.append(result["item"]["name"])
        assert names == ["renewal notice.pdf", "renewal notice 1.pdf"]
        assert drive.by_path("/Contracts/renewal notice 1.pdf")["_content"] == NOTICE

    def test_a_folder_is_made_and_a_file_moved_and_renamed_into_it(self, agents, drive):
        ex, _ = executor(drive)
        made, status = invoke(agents, ex, "onedrive.files.create_folder", {
            "name": "2026", "folder_path": "/Contracts"}, chat_level=3)
        assert status == "success", made
        assert (made["item"]["kind"], made["item"]["folder"]) == ("folder", "/Contracts")
        lease = find_one(agents, ex, "lease")
        moved, status = invoke(agents, ex, "onedrive.files.move", {
            "item_id": lease["item_id"], "to_folder_path": "/Contracts/2026",
            "new_name": "Riverside lease 2024-2027.pdf"}, chat_level=3)
        assert status == "success", moved
        assert (moved["item"]["name"], moved["item"]["folder"]) == (
            "Riverside lease 2024-2027.pdf", "/Contracts/2026")
        assert drive.by_path("/Contracts/2026/Riverside lease 2024-2027.pdf") is not None

    def test_several_files_are_moved_in_one_go_and_a_failure_is_said(self, agents, drive):
        ex, _ = executor(drive)
        invoke(agents, ex, "onedrive.files.create_folder", {"name": "Archive"}, chat_level=3)
        lease, budget = find_one(agents, ex, "lease"), find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "onedrive.files.move_many", {
            "item_ids": [lease["item_id"], "no-such-item", budget["item_id"], lease["item_id"]],
            "to_folder_path": "/Archive"}, chat_level=3)
        assert status == "success", result
        assert result["folder"] == "/Archive"
        assert sorted(i["name"] for i in result["moved"]) == sorted([lease["name"], budget["name"]])
        assert [(f["item_id"], f["kind"]) for f in result["failed"]] == [("no-such-item", "not_found")]
        assert drive.by_path("/Archive/" + budget["name"]) is not None

        nowhere, status = invoke(agents, ex, "onedrive.files.move_many", {
            "item_ids": [lease["item_id"]], "to_folder_path": "/Nowhere"}, chat_level=3)
        assert status == "error" and nowhere["kind"] == "not_found"
        asked, status = invoke(agents, ex, "onedrive.files.move_many", {
            "item_ids": [lease["item_id"]], "to_folder_path": "/Archive"}, chat_level=2)
        assert status == "error" and asked.get("denied") is True

    def test_delete_goes_to_the_recycle_bin(self, agents, drive):
        ex, _ = executor(drive)
        budget = find_one(agents, ex, "budget")
        result, status = invoke(agents, ex, "onedrive.files.delete",
                                {"item_id": budget["item_id"]}, chat_level=3)
        assert status == "success", result
        assert result["deleted"] is True and "recycle bin" in result["note"]
        assert [i["name"] for i in drive.recycle] == ["Q3 budget.xlsx"]
        after, _ = invoke(agents, ex, "onedrive.files.search", {"query": "budget"})
        assert after["items"] == []

    def test_an_upload_that_gets_no_answer_is_unknown_and_not_retried(self, agents, drive):
        drive.drop_upload = True
        ex, provider = executor(drive)
        ref = attach(provider, "renewal notice.pdf", NOTICE)
        result, status = invoke(agents, ex, "onedrive.files.upload", {
            "file_ref": ref, "folder_path": "/Contracts"}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert drive.uploads == ["renewal notice.pdf"]


class TestSharing:
    def test_a_person_given_access_shows_in_who_has_access_until_revoked(self, agents, drive):
        ex, provider = executor(drive)
        lease = find_one(agents, ex, "lease")
        shared, status = invoke(agents, ex, "onedrive.sharing.invite", {
            "item_id": lease["item_id"], "recipients": [DANA], "role": "write"}, chat_level=3)
        assert status == "success", shared
        assert shared["invited"] == [DANA]
        access, _ = invoke(agents, ex, "onedrive.sharing.permissions", {"item_id": lease["item_id"]})
        dana = next(p for p in access["permissions"] if p["who"] == DANA)
        assert (dana["role"], dana["via"]) == ("write", "invite")
        revoked, status = invoke(agents, ex, "onedrive.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": dana["permission_id"]}, chat_level=3)
        assert status == "success" and revoked["revoked"] is True, revoked
        access, _ = invoke(agents, ex, "onedrive.sharing.permissions", {"item_id": lease["item_id"]})
        assert [p["who"] for p in access["permissions"]] == [GraphDriveStub.ACCOUNT]
        statuses = [r["keys"]["status"] for r in provider.data["onedrive__share"].values()]
        assert statuses == ["granted", "revoked"]

    def test_a_link_is_for_the_organization_unless_asked(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        made, status = invoke(agents, ex, "onedrive.sharing.link",
                              {"item_id": lease["item_id"]}, chat_level=3)
        assert status == "success", made
        assert made["reach"] == "people in your organization" and made["link"].startswith("https://")
        assert drive.permissions[lease["item_id"]][-1]["link"]["scope"] == "organization"

    def test_the_owner_cannot_be_removed(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "onedrive.sharing.revoke", {
            "item_id": lease["item_id"], "permission_id": "owner"}, chat_level=3)
        assert status == "error" and "owner" in result["error"]

    def test_a_name_is_not_an_address(self, agents, drive):
        ex, _ = executor(drive)
        lease = find_one(agents, ex, "lease")
        result, status = invoke(agents, ex, "onedrive.sharing.invite", {
            "item_id": lease["item_id"], "recipients": ["Dana"]}, chat_level=3)
        assert status == "error" and "email" in result["error"]

    def test_an_expired_connection_says_reconnect(self, agents, drive):
        ex, _ = executor(drive, access_token="stale")
        result, status = invoke(agents, ex, "onedrive.files.search", {"query": "lease"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"].lower()
