"""The Mail agent, run the way production runs it: in its own worker
over the real wire, against a loopback mail server holding fictional
mail for Sidra Office Supplies.
"""

import asyncio
import email

import pytest
import yaml

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.conftest import ROOT
from tests.mail_stub import MailStub

ME = MailStub.ACCOUNT
SUPPLIER = "quotes@northlight-seating.example"
CUSTOMER = "dana@harbourline.example"
QUOTE_PDF = b"%PDF-1.4 fictional quotation " + bytes(range(256))


def run(awaitable):
    return asyncio.run(awaitable)


def fill(stub):
    """Two conversations, in the order they arrived: a customer's
    request nobody has answered; and a quotation with its attachment,
    which Sara answered and the supplier answered back."""
    stub.add(
        "INBOX", f"Dana Haddad <{CUSTOMER}>",
        "New office — furniture for 20 desks",
        "We are moving in November and need desks and chairs for 20 people.",
        date="2026-09-02T15:40:00+04:00", message_id="office@harbourline.example")
    quote = stub.add(
        "INBOX", f"Northlight Seating <{SUPPLIER}>",
        "Quotation Q-2041 — 20 office chairs",
        "Please find our quotation attached. Delivery in 10 working days.",
        date="2026-09-03T10:12:00+04:00", message_id="q2041@northlight.example",
        attachments=[("Q-2041.pdf", "application/pdf", QUOTE_PDF)], seen=True)
    asked = stub.add(
        "Sent Items", ME, "Re: Quotation Q-2041 — 20 office chairs",
        "Thank you. Can you deliver in 7 working days instead?",
        date="2026-09-03T11:30:00+04:00", to=SUPPLIER,
        message_id="asked@sidra.example", in_reply_to=quote, seen=True)
    stub.add(
        "INBOX", f"Northlight Seating <{SUPPLIER}>",
        "Re: Quotation Q-2041 — 20 office chairs",
        "Seven working days is possible for an order placed this week.",
        date="2026-09-04T09:05:00+04:00", message_id="seven@northlight.example",
        in_reply_to=asked, references=f"{quote} {asked}")
    return stub


@pytest.fixture
def mail():
    stub = fill(MailStub().start())
    yield stub
    stub.stop()


def executor(stub, password=""):
    provider = InMemoryResourceProvider(
        secrets={"mail__account": stub.secret(password)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs=None, chat_level=1):
    return run(ex.invoke(agents["mail"], f"mail.{name}", inputs or {},
                         chat_level=chat_level))


def subjects(result):
    return [row["subject"] for row in result["messages"]]


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["mail"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_needs_no_packages_and_declares_every_server_with_its_port(self):
        manifest = yaml.safe_load(
            (ROOT / "mail" / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["implementation"]["dependencies"] == []
        hosts = manifest["network"]["hosts"]
        named = [host for host in hosts if isinstance(host, str)]
        assert all(host.rsplit(":", 1)[1] in ("993", "465", "587") for host in named)
        assert {"from_secret": "account.imap_host", "port": 993} in hosts

    def test_the_servers_the_code_knows_are_the_ones_the_manifest_declares(self):
        """A provider known by name and not declared would be refused
        where agents are confined; one declared and not known is a
        host nobody asked for."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "mail_servers", ROOT / "mail" / "tools" / "servers.py")
        servers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(servers)
        known = set()
        for imap, smtp, ports in servers.PROVIDERS.values():
            known.add(f"{imap}:{servers.IMAP_PORT}")
            known.update(f"{smtp}:{port}" for port in ports)
        manifest = yaml.safe_load(
            (ROOT / "mail" / "manifest.yaml").read_text(encoding="utf-8"))
        assert {host for host in manifest["network"]["hosts"]
                if isinstance(host, str)} == known

    def test_a_connection_encrypted_after_the_greeting_checks_the_servers_name(self):
        """STARTTLS checks the certificate against the name the
        connection was made for, which it has to be told."""
        import socket
        import sys

        sys.path.insert(0, str(ROOT))
        from mail.tools.sender import _Connection

        here, there = socket.socketpair()
        try:
            connection = _Connection(here, "smtp.mail.me.com")
            assert connection._host == "smtp.mail.me.com"
            assert connection._get_socket("ignored", 0, 5) is here
        finally:
            here.close()
            there.close()

    def test_sending_is_the_one_external_act(self, agents):
        manifest = agents["mail"].manifest
        levels = {name: manifest.function(f"mail.{name}")[1]["permission_level"]
                  for name in ("search.find", "search.thread", "draft.reply",
                               "draft.send", "search.save_attachment")}
        assert levels == {"search.find": 0, "search.thread": 0, "draft.reply": 1,
                          "draft.send": 3, "search.save_attachment": 2}


class TestTheAccount:
    def test_it_reports_its_address(self, agents, mail):
        ex, _ = executor(mail)
        assert invoke(agents, ex, "account.status") == (
            {"connected": True, "email": ME}, "success")

    def test_a_password_the_server_refuses_says_what_to_make(self, agents, mail):
        ex, _ = executor(mail, password="the-accounts-own-password")
        result, status = invoke(agents, ex, "account.status")
        assert status == "success" and result["connected"] is False
        assert "refused the sign-in" in result["problem"]
        assert "app password" in result["problem"]
        assert "the-accounts-own-password" not in result["problem"]

    def test_a_provider_known_by_name_is_told_where(self, agents, mail):
        provider = InMemoryResourceProvider(secrets={"mail__account": {
            **mail.secret("wrong"), "provider": "gmail"}})
        result, _ = invoke(agents, FunctionExecutor(provider=provider),
                           "account.status")
        assert "myaccount.google.com/apppasswords" in result["problem"]

    def test_an_account_that_names_no_server(self, agents):
        provider = InMemoryResourceProvider(secrets={"mail__account": {
            "account": ME, "password": "x", "provider": "other"}})
        result, _ = invoke(agents, FunctionExecutor(provider=provider),
                           "account.status")
        assert result["connected"] is False
        assert "names no mail server" in result["problem"]

    def test_the_accounts_are_listed_without_a_password(self, agents, mail):
        first, second = mail.secret(), {**mail.secret(), "account": "work@sidra.example"}
        provider = InMemoryResourceProvider(secrets={"mail__account": [
            {"resource_ref": "sec_first", "name": f"Mail — {ME}",
             "keys": {"account": ME, "provider": "other"},
             "values": first, "default": True},
            {"resource_ref": "sec_second", "name": "Mail — work",
             "keys": {"account": "work@sidra.example", "provider": "fastmail"},
             "values": second},
        ]})
        ex = FunctionExecutor(provider=provider)
        result, status = invoke(agents, ex, "account.list")
        assert status == "success", result
        assert [(a["account"], a["provider"], a["default"])
                for a in result["accounts"]] == [
            (ME, "other", True), ("work@sidra.example", "fastmail", False)]
        assert MailStub.PASSWORD not in str(result)
        # Named wrongly: told which are connected.
        result, status = invoke(agents, ex, "search.find",
                                {"account": "nobody@x.example"})
        assert status == "error" and "work@sidra.example" in result["error"]


class TestFinding:
    def test_nothing_asked_for_is_the_newest_mail(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search.find")
        assert status == "success", result
        assert result["folder"] == "INBOX" and result["total_estimate"] == 3
        assert subjects(result) == [
            "Re: Quotation Q-2041 — 20 office chairs",
            "Quotation Q-2041 — 20 office chairs",
            "New office — furniture for 20 desks"]
        newest = result["messages"][0]
        assert newest["from"] == f"Northlight Seating <{SUPPLIER}>"
        assert newest["message_id"] == "seven@northlight.example"
        assert newest["thread_id"] == "q2041@northlight.example"
        assert newest["date"] == "2026-09-04T09:05:00+04:00"
        assert newest["snippet"].startswith("Seven working days is possible")
        assert newest["unread"] is True
        assert result["messages"][1]["unread"] is False

    def test_by_sender_subject_and_words(self, agents, mail):
        ex, _ = executor(mail)
        assert subjects(invoke(agents, ex, "search.find",
                               {"from": "harbourline"})[0]) == [
            "New office — furniture for 20 desks"]
        assert len(invoke(agents, ex, "search.find",
                          {"subject": "Q-2041"})[0]["messages"]) == 2
        found, _ = invoke(agents, ex, "search.find",
                          {"query": "moving in November"})
        assert [row["message_id"] for row in found["messages"]] == [
            "office@harbourline.example"]
        assert invoke(agents, ex, "search.find",
                      {"from": "harbourline", "subject": "Q-2041"})[0]["messages"] == []

    def test_by_date_and_unread(self, agents, mail):
        ex, _ = executor(mail)
        assert subjects(invoke(agents, ex, "search.find",
                               {"since": "2026-09-04"})[0]) == [
            "Re: Quotation Q-2041 — 20 office chairs"]
        assert subjects(invoke(agents, ex, "search.find",
                               {"before": "2026-09-03T00:00:00+04:00"})[0]) == [
            "New office — furniture for 20 desks"]
        assert len(invoke(agents, ex, "search.find",
                          {"unread_only": True})[0]["messages"]) == 2
        result, status = invoke(agents, ex, "search.find", {"since": "last week"})
        assert status == "error" and "is not a date" in result["error"]

    def test_words_in_another_alphabet(self, agents, mail):
        mail.add("INBOX", "مكتب النور <office@alnoor.example>", "عرض سعر الكراسي",
                 "نرفق لكم عرض السعر المطلوب.", date="2026-09-05T08:00:00+04:00",
                 message_id="arabic@alnoor.example")
        ex, _ = executor(mail)
        by_subject, status = invoke(agents, ex, "search.find", {"subject": "الكراسي"})
        assert status == "success", by_subject
        assert [row["message_id"] for row in by_subject["messages"]] == [
            "arabic@alnoor.example"]
        assert by_subject["messages"][0]["from"] == "مكتب النور <office@alnoor.example>"
        by_words, status = invoke(agents, ex, "search.find", {"query": "عرض السعر"})
        assert status == "success", by_words
        assert [row["message_id"] for row in by_words["messages"]] == [
            "arabic@alnoor.example"]

    def test_what_was_sent_is_in_its_own_folder(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search.find", {"folder": "sent"})
        assert status == "success", result
        assert result["folder"] == "Sent Items"
        assert [row["message_id"] for row in result["messages"]] == [
            "asked@sidra.example"]

    def test_a_long_result_continues(self, agents, mail):
        ex, _ = executor(mail)
        first, _ = invoke(agents, ex, "search.find", {"max_results": 2})
        assert len(first["messages"]) == 2 and first["next_page_token"] == "2"
        rest, _ = invoke(agents, ex, "search.find",
                         {"max_results": 2, "page_token": first["next_page_token"]})
        assert subjects(rest) == ["New office — furniture for 20 desks"]
        assert "next_page_token" not in rest

    def test_an_account_with_an_archive_is_searched_there(self, agents):
        stub = fill(MailStub(archive=True).start())
        try:
            ex, _ = executor(stub)
            result, status = invoke(agents, ex, "search.find", {"subject": "Q-2041"})
            assert status == "success", result
            assert result["folder"] == "[Mail]/All Mail"
            # What Sara sent is among it: the archive holds everything.
            assert len(result["messages"]) == 3
        finally:
            stub.stop()


class TestAConversation:
    def test_it_is_read_whole_from_the_inbox_and_what_was_sent(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search.thread",
                                {"thread_id": "q2041@northlight.example"})
        assert status == "success", result
        assert result["thread_id"] == "q2041@northlight.example"
        assert result["subject"] == "Quotation Q-2041 — 20 office chairs"
        assert [(m["message_id"], m["from"]) for m in result["messages"]] == [
            ("q2041@northlight.example", f"Northlight Seating <{SUPPLIER}>"),
            ("asked@sidra.example", ME),
            ("seven@northlight.example", f"Northlight Seating <{SUPPLIER}>")]
        assert result["messages"][1]["body"] == (
            "Thank you. Can you deliver in 7 working days instead?")
        assert result["attachments"] == [{
            "message_id": "q2041@northlight.example", "attachment_id": "a1",
            "filename": "Q-2041.pdf", "mime_type": "application/pdf",
            "size": len(QUOTE_PDF)}]

    def test_any_message_of_it_finds_it(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search.thread",
                                {"thread_id": "seven@northlight.example"})
        assert status == "success", result
        assert result["thread_id"] == "q2041@northlight.example"
        assert len(result["messages"]) == 3

    def test_a_long_body_is_cut_and_said_to_be(self, agents, mail):
        mail.add("INBOX", CUSTOMER, "Terms", "term " * 2000,
                 message_id="terms@harbourline.example")
        ex, _ = executor(mail)
        result, _ = invoke(agents, ex, "search.thread", {
            "thread_id": "terms@harbourline.example", "max_body_chars": 200})
        [message] = result["messages"]
        assert len(message["body"]) == 200 and message["truncated"] is True

    def test_a_message_written_only_in_html_is_read_as_its_words(self, agents, mail):
        mail.add("INBOX", CUSTOMER, "Floor plan", "",
                 html="<html><head><style>p{color:red}</style></head><body>"
                      "<p>The plan has <b>20</b> desks.</p><p>Two rooms.</p>"
                      "<script>track()</script></body></html>",
                 message_id="plan@harbourline.example")
        ex, _ = executor(mail)
        result, _ = invoke(agents, ex, "search.thread",
                           {"thread_id": "plan@harbourline.example"})
        assert result["messages"][0]["body"] == "The plan has 20 desks.\nTwo rooms."

    def test_a_conversation_nobody_has(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "search.thread",
                                {"thread_id": "nothing@nowhere.example"})
        assert status == "error" and result["kind"] == "not_found"

    def test_an_attachment_is_saved_as_a_file(self, agents, mail):
        ex, provider = executor(mail)
        result, status = invoke(agents, ex, "search.save_attachment", {
            "message_id": "q2041@northlight.example", "attachment_id": "a1",
            "filename": "Q-2041.pdf"}, chat_level=2)
        assert status == "success", result
        assert result["size"] == len(QUOTE_PDF)
        stored = provider.files["mail__attachment"][result["file_ref"]]
        assert stored["content"] == QUOTE_PDF

    def test_an_attachment_the_message_does_not_have(self, agents, mail):
        ex, provider = executor(mail)
        result, status = invoke(agents, ex, "search.save_attachment", {
            "message_id": "q2041@northlight.example", "attachment_id": "a2",
            "filename": "x.pdf"}, chat_level=2)
        assert status == "error" and result["kind"] == "not_found"
        assert not provider.files.get("mail__attachment")


class TestDrafts:
    def drafted(self, agents, ex, **extra):
        result, status = invoke(agents, ex, "draft.reply", {
            "thread_id": "q2041@northlight.example",
            "body": "Please go ahead with seven working days.", **extra})
        assert status == "success", result
        return result

    def test_a_reply_goes_to_the_last_person_who_wrote_who_is_not_us(
            self, agents, mail):
        ex, provider = executor(mail)
        result = self.drafted(agents, ex)
        assert result["to"] == SUPPLIER
        assert result["subject"] == "Re: Quotation Q-2041 — 20 office chairs"
        keys = provider.data["mail__draft"][result["draft_ref"]]["keys"]
        assert keys["status"] == "prepared" and keys["kind"] == "reply"
        assert keys["in_reply_to"] == "seven@northlight.example"
        assert keys["body"] == "Please go ahead with seven working days."
        assert mail.sent == []

    def test_a_new_message_is_prepared_and_nothing_is_sent(self, agents, mail):
        ex, provider = executor(mail)
        result, status = invoke(agents, ex, "draft.compose", {
            "to": CUSTOMER, "subject": "Your furniture",
            "body": "We can furnish twenty desks by November."})
        assert status == "success", result
        assert provider.data["mail__draft"][result["draft_ref"]]["keys"]["kind"] == "new"
        assert mail.sent == []

    def test_sending_hands_the_server_what_the_person_was_shown(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex, cc="sales@sidra.example")
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", result
        assert (result["sent"], result["status"]) == (True, "sent")
        assert result["thread_id"] == "q2041@northlight.example"

        [(sender, recipients, raw)] = mail.sent
        assert recipients == [SUPPLIER, "sales@sidra.example"]
        message = email.message_from_bytes(raw)
        assert message["From"] == ME and message["To"] == SUPPLIER
        assert message["In-Reply-To"] == "<seven@northlight.example>"
        assert message["References"].split() == [
            "<q2041@northlight.example>", "<asked@sidra.example>",
            "<seven@northlight.example>"]
        assert message["Message-ID"] == f"<{result['message_id']}>"
        assert message.get_payload().strip() == "Please go ahead with seven working days."

        keys = provider.data["mail__draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "sent"
        assert keys["sent_message_id"] == result["message_id"]

    def test_a_copy_is_kept_where_the_server_kept_none(self, agents, mail):
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        before = len(mail.folders["Sent Items"].messages)
        result, _ = invoke(agents, ex, "draft.send",
                           {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert result["kept_in_sent"] is True
        kept = mail.folders["Sent Items"].messages
        assert len(kept) == before + 1
        assert kept[-1].message["Message-ID"] == f"<{result['message_id']}>"
        assert "\\Seen" in kept[-1].flags
        # And the conversation now ends with it.
        thread, _ = invoke(agents, ex, "search.thread",
                           {"thread_id": "q2041@northlight.example"})
        assert thread["messages"][-1]["message_id"] == result["message_id"]

    def test_a_server_that_keeps_what_it_sends_is_not_given_a_second_copy(
            self, agents, mail):
        mail.keeps_sent = True
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        before = len(mail.folders["Sent Items"].messages)
        result, _ = invoke(agents, ex, "draft.send",
                           {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert result["kept_in_sent"] is True
        assert len(mail.folders["Sent Items"].messages) == before + 1

    def test_a_recipient_the_server_refuses_sends_nothing(self, agents, mail):
        mail.refused_recipients.add(SUPPLIER)
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "server"
        assert SUPPLIER in result["error"] and "Nothing was sent" in result["error"]
        assert mail.sent == []
        keys = provider.data["mail__draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "prepared"

    def test_an_answer_that_never_comes_is_unknown_and_not_tried_again(
            self, agents, mail):
        mail.never_answers = True
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success", result
        assert (result["sent"], result["status"]) == (False, "unknown")
        assert "is not known" in result["problem"]
        assert len(mail.sent) == 1
        keys = provider.data["mail__draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "unknown"
        # Asked again, it is not sent again.
        mail.never_answers = False
        again, status = invoke(agents, ex, "draft.send",
                               {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and "not sent again" in again["error"]
        assert len(mail.sent) == 1

    def test_a_password_the_server_refuses_sends_nothing(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        # The password was changed at the provider since the draft was made.
        mail.PASSWORD = "app-pass-2"
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert "app password" in result["error"]
        assert mail.sent == []
        keys = provider.data["mail__draft"][drafted["draft_ref"]]["keys"]
        assert keys["status"] == "prepared"

    def test_a_draft_is_discarded_and_never_sent(self, agents, mail):
        ex, provider = executor(mail)
        drafted = self.drafted(agents, ex)
        assert invoke(agents, ex, "draft.discard",
                      {"draft_ref": drafted["draft_ref"]}) == (
            {"discarded": True}, "success")
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and "discarded" in result["error"]
        assert mail.sent == []

    def test_sending_needs_the_persons_go_ahead(self, agents, mail):
        ex, _ = executor(mail)
        drafted = self.drafted(agents, ex)
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=2)
        assert status != "success", result
        assert mail.sent == []


class TestWaitingForAReply:
    def test_a_conversation_is_unanswered_until_somebody_else_writes(
            self, agents, mail):
        ex, provider = executor(mail)
        watch, status = invoke(agents, ex, "watch.await_reply", {
            "thread_id": "office@harbourline.example", "note": "desks for Dana"})
        assert status == "success", watch
        assert watch["since_message_id"] == "office@harbourline.example"

        checked, _ = invoke(agents, ex, "watch.check")
        assert checked["answered"] == []
        assert checked["unanswered"] == [{
            "watch_ref": watch["watch_ref"],
            "thread_id": "office@harbourline.example",
            "subject": "New office — furniture for 20 desks",
            "note": "desks for Dana"}]

        # Sara writes again: that is no reply.
        mail.add("Sent Items", ME, "Re: New office — furniture for 20 desks",
                 "Any news?", to=CUSTOMER, message_id="news@sidra.example",
                 in_reply_to="office@harbourline.example")
        assert len(invoke(agents, ex, "watch.check")[0]["unanswered"]) == 1

        mail.add("INBOX", f"Dana Haddad <{CUSTOMER}>",
                 "Re: New office — furniture for 20 desks", "Yes: twenty of each.",
                 date="2026-09-06T10:00:00+04:00",
                 message_id="yes@harbourline.example",
                 in_reply_to="news@sidra.example",
                 references="office@harbourline.example news@sidra.example")
        checked, _ = invoke(agents, ex, "watch.check")
        assert checked["unanswered"] == []
        [answered] = checked["answered"]
        assert answered["message_id"] == "yes@harbourline.example"
        assert answered["reply_from"] == f"Dana Haddad <{CUSTOMER}>"
        assert provider.data["mail__watch"][watch["watch_ref"]]["keys"]["status"] == "replied"
        # Answered once, it is not checked again.
        assert invoke(agents, ex, "watch.check")[0]["checked"] == 0

    def test_the_check_can_run_on_the_clock(self, agents):
        manifest = agents["mail"].manifest
        for name in ("watch.check", "watch.new_mail"):
            assert manifest.function(f"mail.{name}")[1]["schedulable"] is True


class TestWatchingTheInbox:
    def started(self, agents, ex, **inputs):
        result, status = invoke(agents, ex, "watch.inbox", inputs)
        assert status == "success", result
        return result

    def test_what_is_there_already_is_not_news(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex)
        assert invoke(agents, ex, "watch.new_mail") == (
            {"checked": 1, "messages": [], "more": False}, "success")

    def test_what_arrives_is_handed_on_once_oldest_first(self, agents, mail):
        ex, _ = executor(mail)
        watch = self.started(agents, ex)
        mail.add("INBOX", CUSTOMER, "First", "one", message_id="first@h.example")
        mail.add("INBOX", SUPPLIER, "Second", "two", message_id="second@n.example")
        result, status = invoke(agents, ex, "watch.new_mail")
        assert status == "success", result
        assert [(row["message_id"], row["inbox_ref"]) for row in result["messages"]] == [
            ("first@h.example", watch["inbox_ref"]),
            ("second@n.example", watch["inbox_ref"])]
        assert result["messages"][0]["thread_id"] == "first@h.example"
        assert invoke(agents, ex, "watch.new_mail")[0]["messages"] == []

    def test_senders_are_narrowed_and_our_own_mail_is_not_news(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex, only_from="@harbourline.example",
                     skip_from="noreply@harbourline.example")
        mail.add("INBOX", CUSTOMER, "Wanted", "x", message_id="wanted@h.example")
        mail.add("INBOX", SUPPLIER, "Another sender", "x", message_id="other@n.example")
        mail.add("INBOX", "noreply@harbourline.example", "Newsletter", "x",
                 message_id="news@h.example")
        mail.add("INBOX", ME, "Note to self", "x", message_id="self@sidra.example")
        result, _ = invoke(agents, ex, "watch.new_mail")
        assert [row["message_id"] for row in result["messages"]] == ["wanted@h.example"]
        # What was left out is not shown later either.
        assert invoke(agents, ex, "watch.new_mail")[0]["messages"] == []

    def test_more_than_asked_for_continues_at_the_next_check(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex)
        for number in range(5):
            mail.add("INBOX", CUSTOMER, f"Mail {number}", "x",
                     message_id=f"m{number}@h.example")
        first, _ = invoke(agents, ex, "watch.new_mail", {"max_results": 3})
        assert [row["subject"] for row in first["messages"]] == [
            "Mail 0", "Mail 1", "Mail 2"] and first["more"] is True
        rest, _ = invoke(agents, ex, "watch.new_mail", {"max_results": 3})
        assert [row["subject"] for row in rest["messages"]] == ["Mail 3", "Mail 4"]
        assert rest["more"] is False

    def test_a_watch_may_start_in_the_past(self, agents, mail):
        ex, _ = executor(mail)
        self.started(agents, ex, since="2026-09-03T12:00:00+04:00")
        result, _ = invoke(agents, ex, "watch.new_mail")
        assert [row["message_id"] for row in result["messages"]] == [
            "seven@northlight.example"]

    def test_a_start_in_the_future_is_from_now_and_said(self, agents, mail):
        ex, _ = executor(mail)
        watch = self.started(agents, ex, since="2099-01-01T00:00:00")
        assert "starts from now" in watch["note"]
        mail.add("INBOX", CUSTOMER, "Arrived", "x", message_id="arrived@h.example")
        assert len(invoke(agents, ex, "watch.new_mail")[0]["messages"]) == 1

    def test_a_time_that_is_not_one(self, agents, mail):
        ex, _ = executor(mail)
        result, status = invoke(agents, ex, "watch.inbox", {"since": "yesterday"})
        assert status == "error" and "ISO 8601" in result["error"]

    def test_an_inbox_the_server_renumbered_starts_again_and_says_so(
            self, agents, mail):
        ex, provider = executor(mail)
        watch = self.started(agents, ex)
        mail.renumber()
        assert invoke(agents, ex, "watch.new_mail")[0]["messages"] == []
        keys = provider.data["mail__inbox"][watch["inbox_ref"]]["keys"]
        assert "renumbered" in keys["note"]
        mail.add("INBOX", CUSTOMER, "After", "x", message_id="after@h.example")
        result, _ = invoke(agents, ex, "watch.new_mail")
        assert [row["message_id"] for row in result["messages"]] == ["after@h.example"]


class TestBehindThePlatformsProxy:
    """Where the platform confines an agent its one way out is the
    platform's proxy, which opens the ports a manifest declared. Mail
    is not the web's protocol: the connection is a tunnel, and what
    passes through it is the agent's and the mail server's."""

    @pytest.fixture
    def proxy(self, mail, monkeypatch):
        from ai_runtime.agents.egress import EgressProxy

        monkeypatch.setattr(EgressProxy, "resolver",
                            staticmethod(lambda host, port: ["127.0.0.1"]))
        found = EgressProxy(0, allow_loopback=True)
        assert found.start() == []
        found.admitted = lambda hosts: monkeypatch.setenv(
            "DECENTAI_PROXY", found.address(found.admit("Mail", {
                "declared": True, "any": False, "hosts": hosts,
                "from_secrets": []})))
        yield found
        found.stop()

    def by_name(self, mail):
        secret = {**mail.secret(),
                  "test_imap": f"imap.sidra.example:{mail.imap_port}",
                  "test_smtp": f"smtp.sidra.example:{mail.smtp_port}"}
        provider = InMemoryResourceProvider(secrets={"mail__account": secret})
        return FunctionExecutor(provider=provider), provider

    def test_mail_is_read_and_sent_through_it(self, agents, mail, proxy):
        proxy.admitted([f"imap.sidra.example:{mail.imap_port}",
                        f"smtp.sidra.example:{mail.smtp_port}"])
        ex, _ = self.by_name(mail)
        found, status = invoke(agents, ex, "search.find", {"subject": "Q-2041"})
        assert status == "success", found
        assert len(found["messages"]) == 2
        drafted, _ = invoke(agents, ex, "draft.compose", {
            "to": CUSTOMER, "subject": "Through", "body": "the proxy"})
        sent, status = invoke(agents, ex, "draft.send",
                              {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "success" and sent["sent"] is True, sent
        assert len(mail.sent) == 1 and proxy.refusals == 0

    def test_a_server_it_did_not_declare_is_refused_and_told_why(
            self, agents, mail, proxy):
        proxy.admitted([f"smtp.sidra.example:{mail.smtp_port}"])
        ex, _ = self.by_name(mail)
        result, status = invoke(agents, ex, "search.find")
        assert status == "error" and result["kind"] == "refused"
        assert "did not declare imap.sidra.example" in result["error"]
        assert mail.logins == 0

    def test_a_port_it_did_not_declare_is_refused(self, agents, mail, proxy):
        proxy.admitted([f"imap.sidra.example:{mail.imap_port}",
                        "smtp.sidra.example:465"])
        ex, _ = self.by_name(mail)
        drafted, _ = invoke(agents, ex, "draft.compose", {
            "to": CUSTOMER, "subject": "Refused", "body": "on this port"})
        result, status = invoke(agents, ex, "draft.send",
                                {"draft_ref": drafted["draft_ref"]}, chat_level=3)
        assert status == "error" and result["kind"] == "refused"
        assert f"is reached on port 465, not {mail.smtp_port}" in result["error"]
        assert mail.sent == []
