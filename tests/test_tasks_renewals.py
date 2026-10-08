"""The renewals half of the Tasks agent, in a real worker: a subscription
renewing on 1 December with 60 days' notice, an agreement whose
clause says "December" and "sixty (60) days" and nothing more, and
the difference between a reminder and a cancellation."""

import asyncio
import json

from ai_runtime.execution.executor import FunctionExecutor
from ai_runtime.sinks import ChatSinks
from sim.resources import InMemoryResourceProvider

AGREEMENT_TEXT = """SERVICE AGREEMENT between Meridian Furnishings LLC ("Customer") and Cloudledger Ltd ("Provider").

1. Term. This Agreement commences on 1 December 2025 and continues for an initial term of twelve (12) months.
2. Renewal. This Agreement renews in December of each year for a further twelve (12) months unless terminated in accordance with clause 3.
3. Termination. Either party may terminate this Agreement at the end of the then-current term by giving the other party not less than sixty (60) days' written notice.
4. Fees. USD 4,800 per year, payable in advance.
"""


def run(awaitable):
    return asyncio.run(awaitable)


def make(llm=None):
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider, sinks=ChatSinks(llm=llm)), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["tasks"], name, inputs, chat_level=chat_level))


def cloudledger(agents, ex, **overrides):
    inputs = {"name": "Cloudledger accounting subscription", "counterparty": "Cloudledger Ltd",
              "kind": "subscription", "owner": "Dana", "reference": "SA-2025-118",
              "start_date": "2025-12-01", "renewal_date": "2026-12-01", "notice_days": 60,
              "notice_basis": "calendar", "auto_renews": "yes", "value": 4800,
              "clauses": [{"topic": "notice", "reference": "clause 3", "verified": True,
                           "quote": "not less than sixty (60) days' written notice",
                           "reading": "Notice must reach them 60 days before the term ends."}]}
    inputs.update(overrides)
    inputs = {k: v for k, v in inputs.items() if v is not None}
    result, status = invoke(agents, ex, "tasks.agreements.create", inputs)
    assert status == "success", result
    return result


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["tasks"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []


class TestTwoDates:
    def test_calendar_days_give_two_separate_deadlines(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex)
        assert made["renewal_date"] == "2026-12-01" and made["notice_deadline"] == "2026-10-02"
        assert made["asks"] == []
        assert [(d["kind"], d["date"], d["basis"]) for d in made["deadlines"]] == [
            ("renewal", "2026-12-01", "calendar"), ("notice", "2026-10-02", "calendar")]
        got, _ = invoke(agents, ex, "tasks.agreements.get", {"agreement_ref": made["agreement_ref"]})
        assert got["status"] == "active" and got["decision"] == "undecided"
        assert got["clauses"][0]["verified"] == "yes" and got["clauses"][0]["reference"] == "clause 3"
        assert len(got["deadlines"]) == 2

    def test_business_days_are_counted_monday_to_friday(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex, notice_basis="business")
        assert made["notice_deadline"] == "2026-09-08"      # 60 weekdays = 12 weeks before Tue 1 Dec

    def test_an_unknown_basis_or_missing_date_means_no_deadline_and_an_ask(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex, notice_basis="unknown")
        assert made["notice_deadline"] == ""
        assert made["asks"] == ["whether the 60 days' notice are calendar or business days"]
        assert [d["kind"] for d in made["deadlines"]] == ["renewal"]
        undated = cloudledger(agents, ex, name="Pier 9 warehouse lease", renewal_date=None,
                              notice_days=None, notice_basis="unknown", auto_renews="unknown")
        assert undated["deadlines"] == []
        assert undated["asks"] == ["the exact renewal date (YYYY-MM-DD)", "the notice period in days",
                                   "whether the agreement renews automatically"]
        bad, status = invoke(agents, ex, "tasks.agreements.create", {
            "name": "X", "counterparty": "Y", "kind": "other", "renewal_date": "Dec 2026"})
        assert status == "error" and "YYYY-MM-DD" in bad["error"]
        listed, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2026-09-01"})
        assert [(i["agreement"], i["reason"]) for i in listed["incomplete"]] == [
            ("Cloudledger accounting subscription", "60 days' notice, calendar or business unknown"),
            ("Pier 9 warehouse lease", "no renewal date")]

    def test_updating_the_basis_moves_the_same_deadline(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex)
        notice_ref = made["deadlines"][1]["deadline_ref"]
        changed, status = invoke(agents, ex, "tasks.agreements.update", {
            "agreement_ref": made["agreement_ref"], "notice_basis": "business", "owner": "Omar"})
        assert status == "success", changed
        assert changed["notice_deadline"] == "2026-09-08"
        got, _ = invoke(agents, ex, "tasks.agreements.get", {"agreement_ref": made["agreement_ref"]})
        notice = next(d for d in got["deadlines"] if d["kind"] == "notice")
        assert notice["deadline_ref"] == notice_ref and notice["date"] == "2026-09-08"
        assert notice["basis"] == "business" and got["owner"] == "Omar"


class TestExtraction:
    @staticmethod
    async def model(messages, max_tokens=None):
        return json.dumps({
            "counterparty": "Cloudledger Ltd", "kind": "subscription",
            "start_text": "1 December 2025", "renewal_text": "renews in December of each year",
            "term_text": "twelve (12) months",
            "notice_text": "not less than sixty (60) days' written notice", "auto_renews": "yes",
            "clauses": [
                {"topic": "renewal", "reading": "It renews every December.",
                 "quote": "This Agreement renews in December of each year for a further twelve (12) months unless terminated in accordance with clause 3."},
                {"topic": "notice", "reading": "Sixty days' written notice ends it at the term's end.",
                 "quote": "by giving the other party not less than sixty (60) days' written notice"},
                {"topic": "price", "reading": "Fees rise yearly.",
                 "quote": "Provider may raise fees by 5% annually."},          # not in the text
            ]})

    def test_the_text_is_read_and_checked_and_nothing_is_guessed(self, agents):
        ex, provider = make(llm=self.model)
        result, status = invoke(agents, ex, "tasks.agreements.extract", {
            "text": AGREEMENT_TEXT, "reference": "SA-2025-118"}, chat_level=0)
        assert status == "success", result
        proposal = result["proposal"]
        assert proposal["counterparty"] == "Cloudledger Ltd" and proposal["kind"] == "subscription"
        assert proposal["start_date"] == "2025-12-01"
        assert proposal["renewal_date"] == ""                  # "December" is not a date
        assert proposal["notice_days"] == 60 and proposal["notice_basis"] == "unknown"
        assert proposal["notice_deadline"] == "" and proposal["auto_renews"] == "yes"
        assert result["ambiguities"] == [
            "the renewal date is given as a month only: 'renews in December of each year'",
            "the notice period 'not less than sixty (60) days' written notice' does not say "
            "whether the days are calendar or business days"]
        assert result["asks"] == ["the exact renewal date (YYYY-MM-DD)",
                                  "whether the 60 days' notice are calendar or business days"]
        by_topic = {c["topic"]: c for c in result["clauses"]}
        assert by_topic["renewal"]["verified"] is True and by_topic["renewal"]["ambiguous"] is True
        assert by_topic["notice"]["verified"] is True and by_topic["notice"]["ambiguous"] is True
        assert by_topic["price"]["verified"] is False
        assert result["disclaimer"] == "A reading of the text as supplied, not legal advice."
        assert provider.data.get("tasks__agreement", {}) == {}   # nothing recorded


class TestReminders:
    def test_upcoming_lists_what_is_due_or_passed_and_wakes_only_then(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex)
        cloudledger(agents, ex, name="Old printer maintenance", counterparty="Pier 9 Supply",
                    kind="maintenance", owner="Omar", renewal_date="2026-09-25", notice_days=10)
        quiet, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2026-08-01", "within_days": 10})
        assert quiet["due"] == [] and quiet["awaiting_decision"] == []      # nothing wakes

        soon, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2026-09-20", "within_days": 30})
        assert [(d["agreement"], d["kind"], d["date"], d["days_left"], d["business_days_left"], d["passed"])
                for d in soon["due"]] == [
            ("Old printer maintenance", "notice", "2026-09-15", -5, -3, True),
            ("Old printer maintenance", "renewal", "2026-09-25", 5, 5, False),
            ("Cloudledger accounting subscription", "notice", "2026-10-02", 12, 10, False)]
        assert [a["agreement"] for a in soon["awaiting_decision"]] == [
            "Cloudledger accounting subscription", "Old printer maintenance"]
        mine, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {
            "as_of": "2026-09-20", "within_days": 30, "owner": "Dana"})
        assert [d["agreement"] for d in mine["due"]] == ["Cloudledger accounting subscription"]
        bad, status = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "Friday"})
        assert status == "error"

    def test_a_further_deadline_can_be_added(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex, kind="warranty", name="Aria chairs warranty",
                           counterparty="Northlight Seating", renewal_date=None, notice_days=None,
                           auto_renews="no")
        added, status = invoke(agents, ex, "tasks.deadlines.add", {
            "agreement_ref": made["agreement_ref"], "kind": "warranty_end",
            "title": "Chair mechanism warranty ends", "date": "2031-09-15"})
        assert status == "success" and added["kind"] == "warranty_end"
        listed, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2031-09-01", "within_days": 30})
        assert [d["title"] for d in listed["due"]] == ["Chair mechanism warranty ends"]
        assert listed["incomplete"] == [{"agreement_ref": made["agreement_ref"],
                                         "agreement": "Aria chairs warranty", "reason": "no renewal date"}]


class TestStates:
    def test_a_reminder_or_a_completed_notice_is_not_a_cancellation(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex)
        notice_ref = made["deadlines"][1]["deadline_ref"]
        done, status = invoke(agents, ex, "tasks.deadlines.complete", {
            "deadline_ref": notice_ref, "evidence": "gmail m0142, notice sent to billing@cloudledger.example",
            "done_on": "2026-09-28"})
        assert status == "success" and done["status"] == "done"
        assert "stays active" in done["note"]
        got, _ = invoke(agents, ex, "tasks.agreements.get", {"agreement_ref": made["agreement_ref"]})
        assert got["status"] == "active"
        again, status = invoke(agents, ex, "tasks.deadlines.complete", {"deadline_ref": notice_ref, "evidence": "x"})
        assert status == "error" and "not open" in again["error"]

        refused, status = invoke(agents, ex, "tasks.agreements.set_status", {
            "agreement_ref": made["agreement_ref"], "status": "cancelled"})
        assert status == "error" and "does not make it cancelled" in refused["error"]
        cancelled, status = invoke(agents, ex, "tasks.agreements.set_status", {
            "agreement_ref": made["agreement_ref"], "status": "cancelled", "by": "Dana",
            "evidence": "Cloudledger confirmation CL-88231 of 2026-10-01"})
        assert status == "success", cancelled
        assert cancelled["status"] == "cancelled" and cancelled["deadlines_closed"] == 1   # the renewal
        got, _ = invoke(agents, ex, "tasks.agreements.get", {"agreement_ref": made["agreement_ref"]})
        assert got["status"] == "cancelled" and got["evidence"].startswith("Cloudledger confirmation")
        assert {d["kind"]: d["status"] for d in got["deadlines"]} == {"notice": "done", "renewal": "cancelled"}
        listed, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2026-11-20", "within_days": 30})
        assert listed["due"] == [] and listed["awaiting_decision"] == []
        later, status = invoke(agents, ex, "tasks.decisions.record", {
            "agreement_ref": made["agreement_ref"], "decision": "renew", "by": "Dana"})
        assert status == "error"

    def test_a_decision_is_an_intention_and_a_renewal_needs_the_next_date(self, agents):
        ex, _ = make()
        made = cloudledger(agents, ex)
        decided, status = invoke(agents, ex, "tasks.decisions.record", {
            "agreement_ref": made["agreement_ref"], "decision": "renew", "by": "Sami",
            "decided_on": "2026-09-22", "note": "Price held for 2027."})
        assert status == "success" and decided["status"] == "active"
        soon, _ = invoke(agents, ex, "tasks.deadlines.upcoming", {"as_of": "2026-09-25", "within_days": 30})
        assert soon["awaiting_decision"] == []
        assert [(d["kind"], d["decision"]) for d in soon["due"]] == [("notice", "renew")]

        short, status = invoke(agents, ex, "tasks.agreements.set_status", {
            "agreement_ref": made["agreement_ref"], "status": "renewed", "by": "Sami",
            "evidence": "Cloudledger order confirmation 2027-1"})
        assert status == "error" and "renewal_date" in short["error"]
        renewed, status = invoke(agents, ex, "tasks.agreements.set_status", {
            "agreement_ref": made["agreement_ref"], "status": "renewed", "by": "Sami",
            "evidence": "Cloudledger order confirmation 2027-1", "renewal_date": "2027-12-01"})
        assert status == "success", renewed
        assert renewed["status"] == "active" and renewed["renewal_date"] == "2027-12-01"
        assert renewed["notice_deadline"] == "2027-10-02"
        got, _ = invoke(agents, ex, "tasks.agreements.get", {"agreement_ref": made["agreement_ref"]})
        assert got["decision"] == "undecided" and got["note"].startswith("Renewed (Cloudledger order")
        assert [(d["kind"], d["date"]) for d in got["deadlines"] if d["status"] == "open"] == [
            ("notice", "2027-10-02"), ("renewal", "2027-12-01")]
        assert len(got["decisions"]) == 1 and got["decisions"][0]["note"] == "Price held for 2027."
