"""The Commitments & Tasks agent in a real worker. The notes are from
a fictional Sidra Office Supplies operations meeting on Monday 7 September
2026."""

import asyncio
import json

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

NOTES = """Ops meeting, 7 September 2026. Present: Sami, Dana, Omar.

Harbourline office: Dana will send the revised quotation to Harbourline by Thursday.
Omar said he will chase Northlight about the 15-day delivery and confirm by 11 September 2026.
We should probably review the chair warranty terms before the next order.
Sami to book the site visit once Dana has Harbourline's answer.
Someone needs to update the supplier list.
"""


def run(awaitable):
    return asyncio.run(awaitable)


def make(llm=None):
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider, llm=llm), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["tasks"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["tasks"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_overdue_is_schedulable_and_a_read(self, agents):
        _, function = agents["tasks"].manifest.function("tasks.tasks.overdue")
        assert function["schedulable"] is True and function["permission_level"] == 0


class TestTasks:
    def test_create_update_and_list(self, agents):
        ex, provider = make()
        made, status = invoke(agents, ex, "tasks.tasks.create", {
            "title": "Send revised quotation", "owner": "Dana", "due": "2026-09-10",
            "source": "note_ops_0907"})
        assert status == "success", made
        assert made["unassigned"] is False and made["status"] == "open"

        bad, status = invoke(agents, ex, "tasks.tasks.create",
                             {"title": "x", "due": "Thursday"})
        assert status == "error" and "YYYY-MM-DD" in bad["error"]

        # Done needs evidence; blocked needs a blocker.
        refused, status = invoke(agents, ex, "tasks.tasks.update",
                                 {"task_ref": made["task_ref"], "status": "done"})
        assert status == "error" and "evidence" in refused["error"]
        refused, status = invoke(agents, ex, "tasks.tasks.update",
                                 {"task_ref": made["task_ref"], "status": "blocked"})
        assert status == "error" and "blocker" in refused["error"]

        done, status = invoke(agents, ex, "tasks.tasks.update", {
            "task_ref": made["task_ref"], "status": "done",
            "evidence": "mail draft_ref rec_12 sent as m0042", "done_on": "2026-09-09"})
        assert status == "success", done
        assert done["status"] == "done" and done["evidence"].endswith("m0042")
        record = provider.data["tasks__task"][made["task_ref"]]
        assert record["keys"]["done_on"] == "2026-09-09"

        invoke(agents, ex, "tasks.tasks.create", {"title": "Update supplier list"})
        listed, _ = invoke(agents, ex, "tasks.tasks.list", {"status": "open"})
        assert [t["title"] for t in listed["tasks"]] == ["Update supplier list"]
        assert listed["tasks"][0]["owner"] == ""

    def test_overdue_is_arithmetic_on_dates(self, agents):
        ex, _ = make()
        create = lambda **fields: invoke(agents, ex, "tasks.tasks.create", fields)[0]
        late = create(title="Chase Northlight", owner="Omar", due="2026-09-11")
        soon = create(title="Book site visit", owner="Sami", due="2026-09-16")
        far = create(title="Review warranty", owner="Dana", due="2026-10-30")
        nobody = create(title="Update supplier list", due="2026-09-12")
        undated = create(title="Plan next order", owner="Dana")
        stuck = create(title="Confirm delivery", owner="Omar", due="2026-09-09")
        invoke(agents, ex, "tasks.tasks.update", {
            "task_ref": stuck["task_ref"], "status": "blocked",
            "blocker": "Northlight has not answered"})
        finished = create(title="Send quotation", owner="Dana", due="2026-09-10")
        invoke(agents, ex, "tasks.tasks.update", {
            "task_ref": finished["task_ref"], "status": "done", "evidence": "sent m0042"})

        result, status = invoke(agents, ex, "tasks.tasks.overdue",
                                {"as_of": "2026-09-14", "within_days": 5})
        assert status == "success", result
        assert result["as_of"] == "2026-09-14"
        assert [(t["title"], t["days_late"]) for t in result["overdue"]] == [
            ("Chase Northlight", 3), ("Update supplier list", 2)]
        assert [(t["title"], t["days_left"]) for t in result["due_soon"]] == [
            ("Book site visit", 2)]
        assert [t["title"] for t in result["blocked"]] == ["Confirm delivery"]
        assert [t["title"] for t in result["unassigned"]] == ["Update supplier list"]
        assert [t["title"] for t in result["undated"]] == ["Plan next order"]
        assert far["task_ref"] not in json.dumps(result)
        assert finished["task_ref"] not in json.dumps(result)

        # Once everything late is handled, the wake field is empty.
        for ref in (late["task_ref"], nobody["task_ref"]):
            invoke(agents, ex, "tasks.tasks.update", {
                "task_ref": ref, "status": "done", "evidence": "handled"})
        result, _ = invoke(agents, ex, "tasks.tasks.overdue", {"as_of": "2026-09-14"})
        assert result["overdue"] == []

    def test_overdue_by_owner_and_a_bad_date(self, agents):
        ex, _ = make()
        invoke(agents, ex, "tasks.tasks.create", {"title": "A", "owner": "Omar", "due": "2026-09-01"})
        invoke(agents, ex, "tasks.tasks.create", {"title": "B", "owner": "Dana", "due": "2026-09-01"})
        result, _ = invoke(agents, ex, "tasks.tasks.overdue", {"as_of": "2026-09-14", "owner": "Omar"})
        assert [t["title"] for t in result["overdue"]] == ["A"]
        result, status = invoke(agents, ex, "tasks.tasks.overdue", {"as_of": "Friday"})
        assert status == "error"


class TestExtraction:
    @staticmethod
    async def model(messages, max_tokens=None):
        return json.dumps({"items": [
            {"title": "Send revised quotation to Harbourline", "kind": "commitment",
             "owner": "Dana", "due": "Thursday",
             "quote": "Dana will send the revised quotation to Harbourline by Thursday."},
            {"title": "Chase Northlight about delivery", "kind": "commitment",
             "owner": "Omar", "due": "11 September 2026",
             "quote": "confirm by 11 September 2026"},
            {"title": "Review chair warranty terms", "kind": "suggestion",
             "owner": "", "due": "before the next order",
             "quote": "We should probably review the chair warranty terms before the next order."},
            {"title": "Book the site visit", "kind": "commitment",
             "owner": "Sami", "due": "",
             "quote": "Sami to book the site visit once Dana has Harbourline's answer."},
            {"title": "Update the supplier list", "kind": "suggestion",
             "owner": "Priya", "due": "",                       # invented: not in the notes
             "quote": "Someone needs to update the supplier list."},
            {"title": "Order more chairs", "kind": "commitment",
             "owner": "Omar", "due": "",
             "quote": "Omar will order forty more chairs next week."},   # not in the notes
        ]})

    def test_proposals_quote_verify_and_never_invent(self, agents):
        ex, provider = make(llm=self.model)
        result, status = invoke(agents, ex, "tasks.extract.propose", {
            "notes": NOTES, "source": "note_ops_0907", "meeting_date": "2026-09-07",
            "people": ["Sami", "Dana", "Omar"]})
        assert status == "success", result
        by_title = {p["title"]: p for p in result["proposals"]}
        assert result["commitments"] == 4 and result["suggestions"] == 2

        quotation = by_title["Send revised quotation to Harbourline"]
        assert quotation["kind"] == "commitment" and quotation["owner"] == "Dana"
        assert quotation["due"] == "2026-09-10"           # Thursday after Monday the 7th
        assert quotation["due_text"] == "Thursday" and quotation["verified"] is True

        assert by_title["Chase Northlight about delivery"]["due"] == "2026-09-11"

        warranty = by_title["Review chair warranty terms"]
        assert warranty["kind"] == "suggestion" and warranty["owner"] == ""
        assert warranty["due"] == "" and warranty["due_text"] == "before the next order"

        supplier = by_title["Update the supplier list"]
        assert supplier["owner"] == ""                     # Priya was never in the notes
        assert supplier["verified"] is True

        invented = by_title["Order more chairs"]
        assert invented["verified"] is False               # the quote is not in the notes
        assert result["unassigned"] == 2
        assert all(r["keys"]["status"] == "proposed"
                   for r in provider.data["tasks__extraction"].values())
        assert provider.data.get("tasks__task", {}) == {}  # nothing is a task yet

    def test_confirming_makes_tasks_and_rejecting_makes_nothing(self, agents):
        ex, provider = make(llm=self.model)
        proposed, _ = invoke(agents, ex, "tasks.extract.propose", {
            "notes": NOTES, "meeting_date": "2026-09-07"})
        by_title = {p["title"]: p for p in proposed["proposals"]}

        confirmed, status = invoke(agents, ex, "tasks.extract.confirm", {"items": [
            {"extraction_ref": by_title["Send revised quotation to Harbourline"]["extraction_ref"]},
            {"extraction_ref": by_title["Update the supplier list"]["extraction_ref"],
             "owner": "Omar", "due": "2026-09-18"},           # the user supplies what the notes lacked
            {"extraction_ref": by_title["Book the site visit"]["extraction_ref"]},
            {"extraction_ref": by_title["Review chair warranty terms"]["extraction_ref"],
             "due": "soon"},
            {"extraction_ref": "rec_nope"},
        ]})
        assert status == "success", confirmed
        titles = {t["title"]: t for t in confirmed["tasks"]}
        assert titles["Send revised quotation to Harbourline"]["owner"] == "Dana"
        assert titles["Send revised quotation to Harbourline"]["due"] == "2026-09-10"
        assert titles["Update the supplier list"]["owner"] == "Omar"
        assert titles["Update the supplier list"]["due"] == "2026-09-18"
        assert confirmed["unassigned"] == []
        reasons = {s["extraction_ref"]: s["reason"] for s in confirmed["skipped"]}
        assert reasons["rec_nope"] == "no such proposal"
        assert "YYYY-MM-DD" in reasons[by_title["Review chair warranty terms"]["extraction_ref"]]

        # Confirmed proposals point at their tasks and cannot be confirmed twice.
        record = provider.data["tasks__extraction"][
            by_title["Book the site visit"]["extraction_ref"]]
        assert record["keys"]["status"] == "confirmed"
        assert record["keys"]["task_ref"] == titles["Book the site visit"]["task_ref"]
        again, _ = invoke(agents, ex, "tasks.extract.confirm", {"items": [
            {"extraction_ref": by_title["Book the site visit"]["extraction_ref"]}]})
        assert again["tasks"] == [] and again["skipped"][0]["reason"] == "already confirmed"

        rejected, status = invoke(agents, ex, "tasks.extract.reject", {
            "extraction_ref": by_title["Order more chairs"]["extraction_ref"]})
        assert (status, rejected) == ("success", {"rejected": True})
        listed, _ = invoke(agents, ex, "tasks.tasks.list", {})
        assert listed["total"] == 3
        assert all("From notes:" in r["keys"]["note"]
                   for r in provider.data["tasks__task"].values())

    def test_extraction_needs_the_chats_model(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "tasks.extract.propose",
                                {"notes": NOTES, "meeting_date": "2026-09-07"})
        assert status == "error" and "model" in result["error"].lower()
