"""The Teams agent in a real worker against a loopback Microsoft Graph:
a one-to-one chat with Dana, the Ops group chat with Omar and Priya,
and the Sidra Office team with its General and Showroom channels.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_teams_stub import DM_DANA, GENERAL, OPS, SHOWROOM, TEAM, GraphTeamsStub

DANA = "dana@sidra.example"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def graph():
    stub = GraphTeamsStub().start()
    yield stub
    stub.stop()


def executor(graph, access_token="at-1"):
    provider = InMemoryResourceProvider(secrets={"teams__microsoft": graph.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=0):
    return run(ex.invoke(agents["teams"], name, inputs, chat_level=chat_level))


def ok(agents, ex, name, inputs, chat_level=0):
    result, status = invoke(agents, ex, name, inputs, chat_level)
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["teams"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_one_connection_serves_the_four_microsoft_agents(self, agents):
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        teams = agents["teams"].manifest.resource("secrets", "microsoft")
        for other in ("outlook", "microsoft_calendar", "onedrive"):
            theirs = agents[other].manifest.resource("secrets", "microsoft")
            assert strip(theirs["fields"]) == strip(teams["fields"]), other
            assert theirs["oauth"] == teams["oauth"], other

    def test_it_asks_for_nothing_an_administrator_must_grant(self, agents):
        """Reading channel messages would need admin consent for the one
        consent all four agents share; it is left out on purpose."""
        scopes = agents["teams"].manifest.resource("secrets", "microsoft")["oauth"]["scopes"]
        assert "ChannelMessage.Read.All" not in scopes
        assert {"Chat.ReadWrite", "ChannelMessage.Send"} <= set(scopes)

    def test_only_sending_and_posting_are_level_three(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["teams"].manifest.document["tools"] for f in t["functions"]}
        assert {name for name, level in levels.items() if level == 3} == {"chats.send", "teams.post"}

    def test_status_says_whose_account(self, agents, graph):
        ex, _ = executor(graph)
        assert ok(agents, ex, "teams.account.status", {}) == {
            "connected": True, "email": GraphTeamsStub.ACCOUNT, "name": "Demo"}


class TestReading:
    def test_chats_come_most_recent_first_with_who_is_in_them(self, agents, graph):
        ex, _ = executor(graph)
        chats = ok(agents, ex, "teams.chats.list", {})["chats"]
        assert [(c["chat_id"], c["kind"], c["topic"]) for c in chats] == [
            (OPS, "group", "Ops"), (DM_DANA, "oneOnOne", "Dana Harbour")]
        assert chats[0]["members"] == "omar@sidra.example, priya@sidra.example"
        with_dana = ok(agents, ex, "teams.chats.list", {"with": DANA})["chats"]
        assert [c["chat_id"] for c in with_dana] == [DM_DANA]

    def test_a_chat_reads_as_text_without_system_notices(self, agents, graph):
        ex, _ = executor(graph)
        read = ok(agents, ex, "teams.chats.messages", {"chat_id": DM_DANA})
        [message] = read["messages"]
        assert (message["from"], message["from_name"], message["mine"]) == (DANA, "Dana Harbour", False)
        assert message["text"] == ("Can you send the showroom drawings by Thursday?\n"
                                   "Harbourline & I want to sign off the layout.")

    def test_an_unknown_chat_is_named_not_invented(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "teams.chats.messages", {"chat_id": "19:nope@thread.v2"})
        assert status == "error" and result["kind"] == "not_found"


class TestSending:
    def test_a_message_to_a_person_goes_to_their_existing_chat(self, agents, graph):
        ex, provider = executor(graph)
        sent = ok(agents, ex, "teams.chats.send", {
            "to": DANA, "text": "The drawings are coming Thursday."}, chat_level=3)
        assert sent["chat_id"] == DM_DANA and len(graph.chats) == 2
        assert graph.sends[-1]["body"] == {"contentType": "text",
                                           "content": "The drawings are coming Thursday."}
        record = provider.data["teams__sent"][sent["sent_ref"]]["keys"]
        assert (record["kind"], record["to"], record["message_id"]) == ("chat", DANA, sent["message_id"])
        read = ok(agents, ex, "teams.chats.messages", {"chat_id": DM_DANA})
        assert read["messages"][-1]["mine"] is True

    def test_a_person_with_no_chat_gets_one(self, agents, graph):
        ex, _ = executor(graph)
        sent = ok(agents, ex, "teams.chats.send", {
            "to": "priya@sidra.example", "text": "Can you check the Cloudledger export?"}, chat_level=3)
        assert len(graph.chats) == 3 and sent["chat_id"] not in (DM_DANA, OPS)

    def test_a_name_is_not_an_address_and_a_message_needs_somewhere_to_go(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "teams.chats.send", {"to": "Dana", "text": "Hi"}, chat_level=3)
        assert status == "error" and "email address" in result["error"]
        result, status = invoke(agents, ex, "teams.chats.send", {"text": "Hi"}, chat_level=3)
        assert status == "error" and "chat_id" in result["error"]
        assert graph.sends == []

    def test_replies_are_what_came_after_from_someone_else(self, agents, graph):
        ex, _ = executor(graph)
        sent = ok(agents, ex, "teams.chats.send", {
            "chat_id": DM_DANA, "text": "The drawings are coming Thursday."}, chat_level=3)
        before = ok(agents, ex, "teams.chats.replies", {"sent_ref": sent["sent_ref"]})
        assert before == {"replied": False, "chat_id": DM_DANA, "replies": []}
        graph.reply(DM_DANA, DANA, "Perfect, thank you.")
        after = ok(agents, ex, "teams.chats.replies", {"sent_ref": sent["sent_ref"]})
        assert after["replied"] is True
        assert [(r["from"], r["text"]) for r in after["replies"]] == [(DANA, "Perfect, thank you.")]

    def test_a_send_that_gets_no_answer_is_unknown_and_not_retried(self, agents, graph):
        graph.drop_send = True
        ex, provider = executor(graph)
        result, status = invoke(agents, ex, "teams.chats.send", {
            "chat_id": DM_DANA, "text": "The drawings are coming Thursday."}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert len(graph.sends) == 1
        assert not provider.data.get("teams__sent")


class TestChannels:
    def test_teams_channels_and_a_post(self, agents, graph):
        ex, provider = executor(graph)
        assert [t["name"] for t in ok(agents, ex, "teams.teams.list", {})["teams"]] == ["Sidra Office"]
        channels = ok(agents, ex, "teams.teams.channels", {"team_id": TEAM})["channels"]
        assert [(c["channel_id"], c["name"]) for c in channels] == [(GENERAL, "General"), (SHOWROOM, "Showroom")]
        posted = ok(agents, ex, "teams.teams.post", {
            "team_id": TEAM, "channel_id": SHOWROOM, "subject": "Chairs",
            "text": "Northlight now deliver on the 24th."}, chat_level=3)
        assert graph.posts[-1]["subject"] == "Chairs" and graph.posts[-1]["channel_id"] == SHOWROOM
        record = provider.data["teams__sent"][posted["sent_ref"]]["keys"]
        assert (record["kind"], record["to"]) == ("channel", "Sidra Office / Showroom")
        result, status = invoke(agents, ex, "teams.chats.replies", {"sent_ref": posted["sent_ref"]})
        assert status == "error" and "administrator" in result["error"]

    def test_a_channel_the_team_does_not_have_is_refused(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "teams.teams.post", {
            "team_id": TEAM, "channel_id": "19:nope@thread.tacv2", "text": "Hi"}, chat_level=3)
        assert status == "error" and "no such channel" in result["error"]
        assert graph.posts == []

    def test_an_expired_connection_says_reconnect(self, agents, graph):
        ex, _ = executor(graph, access_token="stale")
        result, status = invoke(agents, ex, "teams.chats.list", {})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"].lower()
