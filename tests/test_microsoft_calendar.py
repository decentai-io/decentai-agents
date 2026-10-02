"""The Microsoft Calendar agent in a real worker against a loopback
Microsoft Graph. The week is 7–11 September 2026 (Monday to Friday),
Dubai time, which the mailbox reports as "Arabian Standard Time"; Dana's
calendar is shared with the account, Omar's is outside the organization.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.graph_calendar_stub import GraphCalendarStub

DANA = "dana@sidra.example"
OMAR = "omar@harbourline.example"
SUPPLIER = "quotes@northlight-seating.example"
TZ = "+04:00"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def graph():
    stub = GraphCalendarStub().start()
    stub.add_event("Ops stand-up", f"2026-09-07T09:00:00{TZ}", f"2026-09-07T09:30:00{TZ}")
    stub.add_event("Supplier call", f"2026-09-08T14:00:00{TZ}", f"2026-09-08T15:00:00{TZ}",
                   attendees=[SUPPLIER])
    stub.share(DANA, [
        {"start": f"2026-09-07T10:00:00{TZ}", "end": f"2026-09-07T12:00:00{TZ}"},
        {"start": f"2026-09-08T09:00:00{TZ}", "end": f"2026-09-08T17:00:00{TZ}"},
    ])
    yield stub
    stub.stop()


def executor(graph, access_token="at-1"):
    provider = InMemoryResourceProvider(
        secrets={"microsoft_calendar__microsoft": graph.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["microsoft_calendar"], name, inputs, chat_level=chat_level))


WEEK = {"from_date": "2026-09-07", "to_date": "2026-09-11",
        "not_before": f"2026-09-06T08:00:00{TZ}"}


def find(agents, ex, **overrides):
    inputs = {"duration_minutes": 60, **WEEK, "attendees": [DANA, OMAR],
              "avoid": [{"start": "12:00", "end": "13:00"}], **overrides}
    return invoke(agents, ex, "microsoft_calendar.events.find_times", inputs)


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["microsoft_calendar"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_one_connection_serves_outlook_and_the_calendar(self, agents):
        """One saved Microsoft account must be grantable to both, which
        needs the same fields — and one consent must cover both, which
        needs the same scopes."""
        mail = agents["outlook"].manifest.resource("secrets", "microsoft")
        cal = agents["microsoft_calendar"].manifest.resource("secrets", "microsoft")
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        assert strip(mail["fields"]) == strip(cal["fields"])
        assert mail["oauth"] == cal["oauth"]

    def test_only_booking_moving_and_cancelling_are_level_three(self, agents):
        levels = {f"{t['id']}.{f['id']}": f["permission_level"]
                  for t in agents["microsoft_calendar"].manifest.document["tools"]
                  for f in t["functions"]}
        assert {name for name, level in levels.items() if level == 3} == {
            "events.create", "events.update", "events.cancel"}

    def test_status_translates_the_mailboxs_windows_zone(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "microsoft_calendar.account.status", {})
        assert status == "success"
        assert result == {"connected": True, "email": GraphCalendarStub.ACCOUNT,
                          "timezone": "Asia/Dubai", "mailbox_timezone": "Arabian Standard Time"}

    def test_a_zone_it_cannot_map_is_said_not_guessed(self, agents, graph):
        graph.mailbox_zone = "Middle Earth Standard Time"
        ex, _ = executor(graph)
        status_result, _ = invoke(agents, ex, "microsoft_calendar.account.status", {})
        assert status_result["timezone"] == "" and "IANA" in status_result["problem"]
        listed, status = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12"})
        assert status == "error" and "IANA" in listed["error"]
        named, status = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12", "timezone": "Asia/Dubai"})
        assert status == "success" and len(named["events"]) == 2


class TestReading:
    def test_events_are_listed_in_the_calendars_zone(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12"})
        assert status == "success", result
        assert result["timezone"] == "Asia/Dubai"
        assert [(e["summary"], e["start"]) for e in result["events"]] == [
            ("Ops stand-up", "2026-09-07T09:00:00+04:00"),
            ("Supplier call", "2026-09-08T14:00:00+04:00"),
        ]

    def test_paging_follows_microsofts_next_link(self, agents, graph):
        ex, _ = executor(graph)
        first, _ = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12", "max_results": 1})
        assert len(first["events"]) == 1 and first["next_page_token"].startswith("http")
        second, _ = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12", "max_results": 1,
            "page_token": first["next_page_token"]})
        assert [e["summary"] for e in second["events"]] == ["Supplier call"]

    def test_an_invitation_is_not_an_acceptance(self, agents, graph):
        ex, _ = executor(graph)
        listed, _ = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-08", "time_max": "2026-09-09"})
        event_id = listed["events"][0]["event_id"]
        assert "/" in event_id          # ids with a slash must survive the path
        result, status = invoke(agents, ex, "microsoft_calendar.events.get", {"event_id": event_id})
        assert status == "success", result
        assert result["responses"] == [{"email": SUPPLIER, "response": "none"}]

    def test_availability_names_who_it_could_not_check(self, agents, graph):
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "microsoft_calendar.events.availability", {
            "time_min": f"2026-09-07T00:00:00{TZ}", "time_max": f"2026-09-08T00:00:00{TZ}",
            "attendees": [DANA, OMAR]})
        assert status == "success", result
        assert {"email": DANA, "start": f"2026-09-07T10:00:00{TZ}",
                "end": f"2026-09-07T12:00:00{TZ}"} in result["busy"]
        assert [u["email"] for u in result["unchecked"]] == [OMAR]


class TestFindingATime:
    def test_slots_keep_clear_of_everyone_who_could_be_checked(self, agents, graph):
        ex, provider = executor(graph)
        result, status = find(agents, ex, max_candidates=10)
        assert status == "success", result
        starts = [c["start"] for c in result["candidates"]]
        assert starts, result
        assert not any(s.startswith("2026-09-08") for s in starts)        # Dana busy all day
        for s in starts:
            assert not (s.startswith("2026-09-07T09:") or s.startswith("2026-09-07T10:")
                        or s.startswith("2026-09-07T11:") or "T12:" in s)
        assert [u["email"] for u in result["unchecked"]] == [OMAR]
        assert len(provider.data["microsoft_calendar__proposal"]) == len(result["candidates"])

    def test_a_window_the_wrong_way_round_is_refused(self, agents, graph):
        ex, _ = executor(graph)
        result, status = find(agents, ex, from_date="2026-09-11", to_date="2026-09-07")
        assert status == "error" and "before" in result["error"]


class TestWriting:
    def test_a_proposal_is_booked_once(self, agents, graph):
        ex, provider = executor(graph)
        found, _ = find(agents, ex, attendees=[DANA])
        proposal_ref = found["candidates"][0]["proposal_ref"]
        made, status = invoke(agents, ex, "microsoft_calendar.events.create", {
            "summary": "Office furniture", "proposal_ref": proposal_ref,
            "location": "Riverside, unit 4B"}, chat_level=3)
        assert status == "success", made
        assert made["invited"] == [DANA]
        stored = graph.events[made["event_id"]]
        assert stored["subject"] == "Office furniture"
        assert [a["emailAddress"]["address"] for a in stored["attendees"]] == [DANA]
        assert graph.creates[0]["start"]["timeZone"] == "UTC"
        proposal = provider.data["microsoft_calendar__proposal"][proposal_ref]["keys"]
        assert proposal["status"] == "booked" and proposal["event_id"] == made["event_id"]
        again, status = invoke(agents, ex, "microsoft_calendar.events.create", {
            "summary": "Office furniture", "proposal_ref": proposal_ref}, chat_level=3)
        assert status == "error" and "already booked" in again["error"]

    def test_an_event_is_moved_and_then_cancelled(self, agents, graph):
        ex, provider = executor(graph)
        made, _ = invoke(agents, ex, "microsoft_calendar.events.create", {
            "summary": "Review", "start": f"2026-09-10T15:00:00{TZ}",
            "end": f"2026-09-10T15:30:00{TZ}"}, chat_level=3)
        moved, status = invoke(agents, ex, "microsoft_calendar.events.update", {
            "event_id": made["event_id"], "start": f"2026-09-10T16:00:00{TZ}",
            "end": f"2026-09-10T16:30:00{TZ}"}, chat_level=3)
        assert status == "success", moved
        assert moved["start"] == f"2026-09-10T16:00:00{TZ}"
        cancelled, status = invoke(agents, ex, "microsoft_calendar.events.cancel", {
            "event_id": made["event_id"]}, chat_level=3)
        assert status == "success" and cancelled["cancelled"] is True
        assert made["event_id"] not in graph.events
        statuses = [r["keys"]["status"] for r in provider.data["microsoft_calendar__booking"].values()]
        assert statuses == ["created", "updated", "cancelled"]

    def test_a_create_that_gets_no_answer_is_unknown_and_not_retried(self, agents, graph):
        graph.drop_create = True
        ex, _ = executor(graph)
        result, status = invoke(agents, ex, "microsoft_calendar.events.create", {
            "summary": "Review", "start": f"2026-09-10T15:00:00{TZ}",
            "end": f"2026-09-10T15:30:00{TZ}"}, chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert len(graph.creates) == 1

    def test_an_expired_connection_says_reconnect(self, agents, graph):
        ex, _ = executor(graph, access_token="stale")
        result, status = invoke(agents, ex, "microsoft_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"].lower()
