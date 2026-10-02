"""The Slack agent in a real worker against a loopback Slack Web API:
the Sidra Office Supplies workspace, with #general, #showroom, a private
#finance, DMs with Dana and Omar, and a group DM with Omar and Priya.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.slack_stub import (DANA, DM_DANA, DM_OMAR, FINANCE, GENERAL, GROUP, ME, OMAR,
                              PRIYA, REVOKED_TOKEN, SHOWROOM, TEAM_URL, SlackStub)


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def slack():
    stub = SlackStub().start()
    yield stub
    stub.stop()


def executor(slack, access_token="xoxp-demo"):
    provider = InMemoryResourceProvider(secrets={"slack__slack": slack.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["slack"], name, inputs, chat_level=chat_level))


def ok(agents, ex, name, inputs, chat_level=1):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["slack"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_only_sending_is_level_three_and_the_clock_runs_the_watch(self, agents):
        functions = {f"{t['id']}.{f['id']}": f
                     for t in agents["slack"].manifest.document["tools"] for f in t["functions"]}
        assert {n for n, f in functions.items() if f["permission_level"] == 3} == {"messages.send"}
        assert {n for n, f in functions.items() if f.get("schedulable")} == {"watch.new"}
        assert functions["watch.new"]["permission_level"] == 1
        assert not functions["watch.new"].get("llm")
        assert functions["watch.start"]["permission_level"] == 1

    def test_it_signs_in_for_a_user_token(self, agents):
        oauth = agents["slack"].manifest.resource("secrets", "slack")["oauth"]
        assert (oauth["provider"], oauth["scope_param"], oauth["scope_separator"],
                oauth["token_path"]) == ("slack", "user_scope", ",", "authed_user")
        assert {"search:read", "chat:write", "im:history"} <= set(oauth["scopes"])

    def test_status_says_whose_account(self, agents, slack):
        ex, _ = executor(slack)
        assert ok(agents, ex, "slack.account.status", {}) == {
            "connected": True, "team": "Sidra Office Supplies", "user": "demo", "user_id": ME}


class TestReading:
    def test_conversations_are_named_for_people(self, agents, slack):
        ex, _ = executor(slack)
        listed = ok(agents, ex, "slack.conversations.list", {})
        rows = {c["conversation_id"]: (c["kind"], c["name"]) for c in listed["conversations"]}
        assert rows == {
            GENERAL: ("channel", "general"), SHOWROOM: ("channel", "showroom"),
            FINANCE: ("private_channel", "finance"), DM_DANA: ("dm", "Dana Harbour"),
            DM_OMAR: ("dm", "Omar Nasser"), GROUP: ("group_dm", "Omar Nasser, Priya Menon")}
        assert listed["more"] is False
        dms = ok(agents, ex, "slack.conversations.list", {"kind": "dm", "max_results": 1})
        assert len(dms["conversations"]) == 1 and dms["more"] is True

    def test_a_conversation_reads_oldest_first_as_text(self, agents, slack):
        ex, _ = executor(slack)
        read = ok(agents, ex, "slack.conversations.messages", {"conversation_id": SHOWROOM})
        assert read["name"] == "showroom" and read["more"] is False
        first, second = read["messages"]
        assert (first["author"], first["mine"], first["reply_count"]) == ("Demo", True, 1)
        assert first["ts"] == slack.chairs_ts
        assert first["link"] == (f"{TEAM_URL}archives/{SHOWROOM}/"
                                 f"p{slack.chairs_ts.replace('.', '')}")
        assert second["author"] == "Dana Harbour"
        assert second["text"] == "Harbourline want the layout signed off & the drawings by Thursday."
        # Only the newest one, and it says there is more.
        latest = ok(agents, ex, "slack.conversations.messages",
                    {"conversation_id": SHOWROOM, "max_results": 1})
        assert [m["author"] for m in latest["messages"]] == ["Dana Harbour"]
        assert latest["more"] is True

    def test_join_notices_are_left_out_and_mentions_read_as_names(self, agents, slack):
        ex, _ = executor(slack)
        general = ok(agents, ex, "slack.conversations.messages", {"conversation_id": GENERAL})
        assert [m["text"] for m in general["messages"]] == ["Office closed on the 24th for the move."]
        group = ok(agents, ex, "slack.conversations.messages", {"conversation_id": GROUP})
        assert group["messages"][0]["text"] == "@Demo can you check the Cloudledger export?"

    def test_a_thread_reads_parent_then_replies(self, agents, slack):
        ex, _ = executor(slack)
        thread = ok(agents, ex, "slack.conversations.replies",
                    {"conversation_id": SHOWROOM, "thread_ts": slack.chairs_ts})
        assert [(m["author"], m["text"]) for m in thread["messages"]] == [
            ("Demo", "Has Northlight confirmed the chair delivery?"),
            ("Omar Nasser", "Not yet, chasing them today.")]
        reply = thread["messages"][1]
        assert reply["thread_ts"] == slack.chairs_ts and "?thread_ts=" in reply["link"]

    def test_search_finds_newest_first_with_where(self, agents, slack):
        ex, _ = executor(slack)
        found = ok(agents, ex, "slack.messages.search", {"query": "drawings"})
        assert [(m["conversation_id"], m["conversation"], m["kind"]) for m in found["messages"]] == [
            (DM_DANA, "Dana Harbour", "dm"), (SHOWROOM, "showroom", "channel")]
        assert found["total"] == 2 and found["more"] is False
        assert found["messages"][0]["author"] == "Dana Harbour"

    def test_an_unknown_conversation_is_named_not_invented(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "slack.conversations.messages",
                                {"conversation_id": "C0NOPE"})
        assert status == "error" and result["kind"] == "not_found"

    def test_a_ts_that_is_not_one_is_refused(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "slack.conversations.replies",
                                {"conversation_id": SHOWROOM, "thread_ts": "yesterday"})
        assert status == "error" and result["kind"] == "invalid"


class TestAnswered:
    def test_a_reply_in_the_thread_is_an_answer(self, agents, slack):
        ex, _ = executor(slack)
        answered = ok(agents, ex, "slack.messages.answered",
                      {"conversation_id": SHOWROOM, "ts": slack.chairs_ts})
        assert answered["answered"] is True and answered["by"] == ["Omar Nasser", "Dana Harbour"]
        assert [(r["where"], r["author"]) for r in answered["replies"]] == [
            ("thread", "Omar Nasser"), ("conversation", "Dana Harbour")]

    def test_only_someone_else_writing_later_counts(self, agents, slack):
        ex, _ = executor(slack)
        mine = slack.say(DM_OMAR, ME, "Did the chairs ship?")
        slack.say(DM_OMAR, ME, "Any news?")
        before = ok(agents, ex, "slack.messages.answered", {"conversation_id": DM_OMAR, "ts": mine})
        assert (before["answered"], before["by"], before["replies"]) == (False, [], [])
        slack.say(DM_OMAR, OMAR, "Yes, they left this morning.", thread_ts=mine)
        after = ok(agents, ex, "slack.messages.answered", {"conversation_id": DM_OMAR, "ts": mine})
        assert after["answered"] is True and after["by"] == ["Omar Nasser"]
        assert after["replies"][0]["text"] == "Yes, they left this morning."

    def test_a_message_that_is_not_there_is_not_found(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "slack.messages.answered",
                                {"conversation_id": SHOWROOM, "ts": "1700000000.000001"})
        assert status == "error" and result["kind"] == "not_found"


class TestSending:
    def test_a_send_posts_as_the_person_and_is_recorded(self, agents, slack):
        ex, provider = executor(slack)
        sent = ok(agents, ex, "slack.messages.send", {
            "conversation_id": SHOWROOM, "thread_ts": slack.chairs_ts,
            "text": "The drawings are coming Thursday."}, chat_level=3)
        assert slack.posts == [{"channel": SHOWROOM, "text": "The drawings are coming Thursday.",
                                "thread_ts": slack.chairs_ts}]
        posted = slack.find(SHOWROOM, sent["ts"])
        assert posted["user"] == ME and posted["thread_ts"] == slack.chairs_ts
        record = provider.data["slack__sent"][sent["sent_ref"]]["keys"]
        assert (record["conversation_id"], record["conversation"], record["ts"],
                record["thread_ts"]) == (SHOWROOM, "showroom", sent["ts"], slack.chairs_ts)
        assert record["link"] == sent["link"] and "?thread_ts=" in sent["link"]

    def test_sending_needs_the_chats_trust(self, agents, slack):
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "slack.messages.send",
                                {"conversation_id": DM_DANA, "text": "Hi"}, chat_level=1)
        assert status == "error" and slack.posts == [] and provider.data == {}

    def test_a_send_that_gets_no_answer_is_unknown_and_not_retried(self, agents, slack):
        slack.drop_send = True
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "slack.messages.send", {
            "conversation_id": DM_DANA, "text": "The drawings are coming Thursday."},
            chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert len(slack.posts) == 1 and slack.calls.count("chat.postMessage") == 1
        assert not provider.data.get("slack__sent")

    def test_an_unknown_conversation_is_refused_before_anything_is_posted(self, agents, slack):
        ex, _ = executor(slack)
        result, status = invoke(agents, ex, "slack.messages.send",
                                {"conversation_id": "C0NOPE", "text": "Hi"}, chat_level=3)
        assert status == "error" and result["kind"] == "not_found"
        assert slack.posts == []


class TestWatching:
    def test_a_mention_and_a_dm_are_handed_on_exactly_once(self, agents, slack):
        ex, provider = executor(slack)
        watch = ok(agents, ex, "slack.watch.start", {"note": "tell me what needs an answer"})
        assert watch["which"] == "both"
        # What was already there is not news.
        quiet = ok(agents, ex, "slack.watch.new", {})
        assert quiet == {"checked": 1, "messages": [], "more": False, "unchecked": 0}

        slack.say(SHOWROOM, ME, f"<@{ME}> note to self")                  # our own
        slack.say(DM_DANA, ME, "Sending them now.")                       # our own
        mention = slack.say(SHOWROOM, PRIYA, f"<@{ME}> can you approve the layout?",
                            thread_ts=slack.chairs_ts)
        dm = slack.say(DM_DANA, DANA, "Thanks! Are the chairs sorted?")
        slack.say(GENERAL, OMAR, "Lunch is here.")                        # neither

        news = ok(agents, ex, "slack.watch.new", {})
        assert [(m["kind"], m["conversation_id"], m["ts"]) for m in news["messages"]] == [
            ("mention", SHOWROOM, mention), ("dm", DM_DANA, dm)]
        first, second = news["messages"]
        assert (first["author"], first["text"]) == ("Priya Menon", "@Demo can you approve the layout?")
        assert first["thread_ts"] == slack.chairs_ts and first["conversation"] == "showroom"
        assert (second["author"], second["conversation"]) == ("Dana Harbour", "Dana Harbour")
        assert first["watch_ref"] == watch["watch_ref"] and news["more"] is False
        assert provider.data["slack__watch"][watch["watch_ref"]]["keys"]["cursor"] == dm

        again = ok(agents, ex, "slack.watch.new", {})
        assert again["messages"] == []

    def test_a_group_dm_counts_and_a_mention_in_a_dm_is_one_row(self, agents, slack):
        ex, _ = executor(slack)
        ok(agents, ex, "slack.watch.start", {"which": "both"})
        said = slack.say(GROUP, PRIYA, f"<@{ME}> the export is ready")
        news = ok(agents, ex, "slack.watch.new", {})
        assert [(m["kind"], m["ts"], m["conversation"]) for m in news["messages"]] == [
            ("dm", said, "Omar Nasser, Priya Menon")]

    def test_mentions_only_and_conversations_left_out(self, agents, slack):
        ex, _ = executor(slack)
        watch = ok(agents, ex, "slack.watch.start", {"which": "mentions",
                                                     "skip_conversations": GENERAL})
        slack.say(DM_DANA, DANA, "Quick question")
        slack.say(GENERAL, OMAR, f"<@{ME}> everyone, the office moves on the 24th")
        wanted = slack.say(FINANCE, PRIYA, f"<@{ME}> the Cloudledger invoice needs you")
        news = ok(agents, ex, "slack.watch.new", {"watch_ref": watch["watch_ref"]})
        assert [(m["kind"], m["ts"]) for m in news["messages"]] == [("mention", wanted)]

    def test_a_busy_check_pages_and_says_more(self, agents, slack):
        ex, _ = executor(slack)
        watch = ok(agents, ex, "slack.watch.start", {"which": "dms"})
        said = [slack.say(DM_DANA, DANA, f"Update {n}") for n in range(3)]
        page = ok(agents, ex, "slack.watch.new", {"max_results": 2})
        assert [m["ts"] for m in page["messages"]] == said[:2] and page["more"] is True
        page = ok(agents, ex, "slack.watch.new", {"watch_ref": watch["watch_ref"], "max_results": 2})
        assert [m["ts"] for m in page["messages"]] == said[2:] and page["more"] is False

    def test_a_bad_skip_list_is_refused_and_records_nothing(self, agents, slack):
        ex, provider = executor(slack)
        result, status = invoke(agents, ex, "slack.watch.start",
                                {"skip_conversations": "#general"})
        assert status == "error" and result["kind"] == "invalid"
        assert provider.data == {}


class TestTheConnection:
    def test_rate_limiting_is_reported_with_the_wait(self, agents, slack):
        ex, _ = executor(slack)
        slack.rate_limit = 1
        result, status = invoke(agents, ex, "slack.conversations.list", {})
        assert status == "error" and result["kind"] == "rate_limited"
        assert "30 seconds" in result["error"]

    def test_a_revoked_token_says_reconnect_and_writes_nothing(self, agents, slack):
        ex, provider = executor(slack, access_token=REVOKED_TOKEN)
        result, status = invoke(agents, ex, "slack.conversations.list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
        result, status = invoke(agents, ex, "slack.watch.start", {})
        assert status == "error" and result["kind"] == "auth"
        result, status = invoke(agents, ex, "slack.messages.send",
                                {"conversation_id": DM_DANA, "text": "Hi"}, chat_level=3)
        assert status == "error" and result["kind"] == "auth"
        assert provider.data == {} and slack.posts == []

    def test_no_credential_is_a_plain_answer(self, agents):
        ex = FunctionExecutor(provider=InMemoryResourceProvider())
        result, status = invoke(agents, ex, "slack.account.status", {})
        assert status == "success" and result["connected"] is False
        assert "No Slack account" in result["problem"]
        result, status = invoke(agents, ex, "slack.watch.new", {})
        assert status == "error" and result["kind"] == "auth"
