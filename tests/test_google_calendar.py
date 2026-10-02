"""The Google Calendar agent in a real worker against a loopback Google
Calendar. The week is 7–11 September 2026 (Monday to Friday), Dubai
time; Dana's calendar is shared, Omar's is not.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.gcal_stub import CalendarStub

DANA = "dana@harbourline.example"
OMAR = "omar@harbourline.example"
TZ = "+04:00"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def gcal():
    stub = CalendarStub().start()
    stub.add_event("Ops stand-up", f"2026-09-07T09:00:00{TZ}", f"2026-09-07T09:30:00{TZ}")
    stub.add_event("Supplier call", f"2026-09-08T14:00:00{TZ}", f"2026-09-08T15:00:00{TZ}",
                   attendees=["quotes@northlight-seating.example"])
    stub.share(DANA, [
        {"start": f"2026-09-07T10:00:00{TZ}", "end": f"2026-09-07T12:00:00{TZ}"},
        {"start": f"2026-09-08T09:00:00{TZ}", "end": f"2026-09-08T17:00:00{TZ}"},
    ])
    yield stub
    stub.stop()


def executor(gcal, access_token="at-1"):
    provider = InMemoryResourceProvider(secrets={"google_calendar__google": gcal.secret(access_token)})
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["google_calendar"], name, inputs, chat_level=chat_level))


WEEK = {"from_date": "2026-09-07", "to_date": "2026-09-11",
        "not_before": f"2026-09-06T08:00:00{TZ}"}


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["google_calendar"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_the_secret_has_the_same_shape_as_the_gmail_agents(self, agents):
        """One saved Google credential must be grantable to both agents,
        which the platform allows only when the fields match exactly."""
        mail = agents["gmail"].manifest.resource("secrets", "google")["fields"]
        cal = agents["google_calendar"].manifest.resource("secrets", "google")["fields"]
        strip = lambda fields: [{k: f[k] for k in ("name", "type", "storage", "required")}
                                for f in fields]
        assert strip(mail) == strip(cal)

    def test_status_reports_the_calendars_zone(self, agents, gcal):
        ex, _ = executor(gcal)
        result, status = invoke(agents, ex, "google_calendar.account.status", {})
        assert status == "success"
        assert result == {"connected": True, "email": CalendarStub.ACCOUNT,
                          "timezone": "Asia/Dubai"}


class TestReading:
    def test_events_are_listed_in_the_calendars_zone(self, agents, gcal):
        ex, _ = executor(gcal)
        result, status = invoke(agents, ex, "google_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12"})
        assert status == "success", result
        assert result["timezone"] == "Asia/Dubai"
        assert [e["summary"] for e in result["events"]] == ["Ops stand-up", "Supplier call"]
        assert result["events"][1]["attendees"] == "quotes@northlight-seating.example"
        assert result["events"][0]["link"].startswith("https://calendar.google.com/")

    def test_availability_names_who_could_not_be_checked(self, agents, gcal):
        ex, _ = executor(gcal)
        result, status = invoke(agents, ex, "google_calendar.events.availability", {
            "time_min": "2026-09-07", "time_max": "2026-09-09",
            "attendees": [DANA, OMAR]})
        assert status == "success", result
        assert result["unchecked"] == [{"email": OMAR, "reason": "notFound"}]
        emails = {b["email"] for b in result["busy"]}
        assert emails == {CalendarStub.ACCOUNT, DANA}

    def test_get_shows_real_responses_not_invitations(self, agents, gcal):
        ex, _ = executor(gcal)
        result, status = invoke(agents, ex, "google_calendar.events.get", {"event_id": "ev002"})
        assert status == "success"
        assert result["responses"] == [{"email": "quotes@northlight-seating.example",
                                        "response": "needsAction"}]


class TestFindingTimes:
    def test_slots_respect_hours_lunch_buffers_and_every_checked_calendar(self, agents, gcal):
        ex, provider = executor(gcal)
        result, status = invoke(agents, ex, "google_calendar.events.find_times", {
            **WEEK, "duration_minutes": 30, "attendees": [DANA, OMAR],
            "avoid": [{"start": "12:00", "end": "13:00"}], "buffer_minutes": 15,
            "max_candidates": 4})
        assert status == "success", result
        assert result["unchecked"] == [{"email": OMAR, "reason": "notFound"}]
        slots = [(c["weekday"], c["start"][11:16], c["end"][11:16]) for c in result["candidates"]]
        # Monday: the stand-up (09:00–09:30) and Dana's 10:00–12:00 each
        # carry a 15-minute buffer, so 09:45–10:15 collides with Dana's
        # padded start and the morning is gone; 12:00–13:00 is lunch, so
        # the first slot is 13:00, the next 13:45 (30 minutes plus the
        # buffer). Tuesday Dana is busy all day. Wednesday is open.
        assert slots == [("Monday", "13:00", "13:30"), ("Monday", "13:45", "14:15"),
                         ("Wednesday", "09:00", "09:30"), ("Wednesday", "09:45", "10:15")]
        # Every candidate is a proposal the user can pick by ref.
        assert all(c["proposal_ref"] in provider.data["google_calendar__proposal"]
                   for c in result["candidates"])
        first = provider.data["google_calendar__proposal"][result["candidates"][0]["proposal_ref"]]
        assert first["keys"]["status"] == "offered"
        assert first["keys"]["timezone"] == "Asia/Dubai"

    def test_nothing_is_proposed_before_now(self, agents, gcal):
        ex, _ = executor(gcal)
        result, _ = invoke(agents, ex, "google_calendar.events.find_times", {
            "from_date": "2026-09-07", "to_date": "2026-09-07",
            "not_before": f"2026-09-07T16:00:00{TZ}", "duration_minutes": 30})
        assert [c["start"][11:16] for c in result["candidates"]] == ["16:00", "16:30"]

    def test_weekends_and_a_bad_zone_are_refused_honestly(self, agents, gcal):
        ex, _ = executor(gcal)
        result, _ = invoke(agents, ex, "google_calendar.events.find_times", {
            "from_date": "2026-09-12", "to_date": "2026-09-13",   # Sat–Sun
            "not_before": f"2026-09-06T08:00:00{TZ}", "duration_minutes": 30})
        assert result["candidates"] == []
        result, status = invoke(agents, ex, "google_calendar.events.find_times", {
            **WEEK, "duration_minutes": 30, "timezone": "Mars/Olympus"})
        assert status == "error" and "IANA" in result["error"]


class TestBooking:
    def test_a_proposal_is_booked_only_with_the_chats_trust(self, agents, gcal):
        ex, provider = executor(gcal)
        found, _ = invoke(agents, ex, "google_calendar.events.find_times", {
            **WEEK, "duration_minutes": 30, "attendees": [DANA]})
        pick = found["candidates"][0]["proposal_ref"]

        refused, status = invoke(agents, ex, "google_calendar.events.create", {
            "summary": "Harbourline office furniture", "proposal_ref": pick})
        assert status == "error" and refused.get("denied") is True
        assert gcal.inserts == []

        booked, status = invoke(agents, ex, "google_calendar.events.create", {
            "summary": "Harbourline office furniture", "proposal_ref": pick,
            "location": "Call"}, chat_level=3)
        assert status == "success", booked
        assert booked["event_id"] in gcal.events
        assert booked["invited"] == [DANA]
        assert booked["start"] == found["candidates"][0]["start"]
        sent = gcal.inserts[-1]
        assert sent["attendees"] == [{"email": DANA}]
        assert sent["start"]["timeZone"] == "Asia/Dubai"
        assert provider.data["google_calendar__proposal"][pick]["keys"]["status"] == "booked"
        booking = provider.data["google_calendar__booking"][booked["booking_ref"]]
        assert booking["keys"]["status"] == "created"
        # Invited is not accepted.
        detail, _ = invoke(agents, ex, "google_calendar.events.get", {"event_id": booked["event_id"]})
        assert detail["responses"] == [{"email": DANA, "response": "needsAction"}]
        # The same proposal cannot be booked twice.
        again, status = invoke(agents, ex, "google_calendar.events.create", {
            "summary": "Again", "proposal_ref": pick}, chat_level=3)
        assert status == "error" and "already booked" in again["error"]

    def test_explicit_times_update_and_cancel(self, agents, gcal):
        ex, provider = executor(gcal)
        booked, status = invoke(agents, ex, "google_calendar.events.create", {
            "summary": "Site visit", "start": "2026-09-10T15:00",
            "end": "2026-09-10T16:00", "attendees": [DANA]}, chat_level=3)
        assert status == "success", booked
        assert booked["start"] == f"2026-09-10T15:00:00{TZ}"    # naive → calendar zone

        moved, status = invoke(agents, ex, "google_calendar.events.update", {
            "event_id": booked["event_id"], "start": "2026-09-10T16:00",
            "end": "2026-09-10T17:00"}, chat_level=3)
        assert status == "success", moved
        assert moved["start"] == f"2026-09-10T16:00:00{TZ}"

        gone, status = invoke(agents, ex, "google_calendar.events.cancel",
                              {"event_id": booked["event_id"]}, chat_level=3)
        assert status == "success" and gone["cancelled"] is True
        assert gcal.events[booked["event_id"]]["status"] == "cancelled"
        statuses = [b["keys"]["status"] for b in provider.data["google_calendar__booking"].values()]
        assert statuses == ["created", "updated", "cancelled"]

    def test_an_unanswered_create_is_unknown_and_not_retried(self, agents, gcal):
        ex, provider = executor(gcal)
        gcal.drop_insert = True
        result, status = invoke(agents, ex, "google_calendar.events.create", {
            "summary": "Maybe", "start": "2026-09-10T15:00", "end": "2026-09-10T16:00"},
            chat_level=3)
        assert status == "error" and result["kind"] == "unknown"
        assert len(gcal.inserts) == 1
        assert provider.data.get("google_calendar__booking", {}) == {}

    def test_an_expired_token_says_reconnect(self, agents, gcal):
        ex, _ = executor(gcal, access_token="expired")
        result, status = invoke(agents, ex, "google_calendar.events.list", {
            "time_min": "2026-09-07", "time_max": "2026-09-12"})
        assert status == "error" and result["kind"] == "auth"
        assert "reconnect" in result["error"]
