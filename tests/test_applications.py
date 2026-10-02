"""The Applications agent in a real worker: a travel authorization asked
one step at a time, every answer and document checked as it arrives,
the record the truth, submission the person's own step. The model is
scripted; the person is a scripted asker."""
import asyncio
import json
from datetime import date, timedelta

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

TODAY = date.today()
DEPARTURE = (TODAY + timedelta(days=30)).isoformat()
RETURN = (TODAY + timedelta(days=37)).isoformat()
EXPIRY_OK = (TODAY + timedelta(days=4 * 365)).isoformat()
EXPIRY_SOON = (TODAY + timedelta(days=37 + 90)).isoformat()      # three months after return
PHOTO = b"\x89PNG\r\n\x1a\n fictional passport pixels"


def pdf_with_text(lines):
    """A one-page PDF with a real text layer, built by hand."""
    def escape(text):
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 12 Tf 72 720 Td " + " ".join(
        f"({escape(line)}) Tj 0 -16 Td" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


PASSPORT_OK = pdf_with_text(["PASSPORT", "Number P7654321", f"Expiry {EXPIRY_OK}"])
PASSPORT_SOON = pdf_with_text(["PASSPORT", "Number P0000001", f"Expiry {EXPIRY_SOON}"])


async def model(messages, max_tokens=None, images=None):
    """The chat's model, scripted: reads a PDF by its text, a photo by
    its pixels, and misreads one number on purpose."""
    prompt = messages[-1]["content"]
    if images:
        model.pictures.append(images)
        return json.dumps({"passport_number": "P9988776", "expiry": EXPIRY_OK})
    if "DOCUMENT (passport-soon.pdf)" in prompt:
        return json.dumps({"passport_number": "P0000001", "expiry": EXPIRY_SOON})
    if "DOCUMENT (passport.pdf)" in prompt:
        return json.dumps({"passport_number": "P7654321", "expiry": EXPIRY_OK})
    if "DOCUMENT (passport-misread.pdf)" in prompt:
        # The number is not what the document says: assumed, not verified.
        return json.dumps({"passport_number": "P7654329", "expiry": EXPIRY_OK})
    return "{}"


model.pictures = []


def run(awaitable):
    return asyncio.run(awaitable)


def make(asker=None):
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider, llm=model, asker=asker), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["applications"], name, inputs, chat_level=chat_level))


def upload(provider, filename, raw):
    return run(provider.create_file("chat_attachment", filename, raw))["resource_ref"]


def started(agents, ex, form="travel_authorization"):
    record, status = invoke(agents, ex, "applications.applications.start", {"form": form})
    assert status == "success", record
    return record["application_ref"]


def answer(agents, ex, ref, field, value):
    return invoke(agents, ex, "applications.applications.answer",
                  {"application_ref": ref, "field": field, "value": value})


TRIP = [("traveller", "Omar Haddad"), ("employee_id", "SO-12345"),
        ("destination", "Riyadh, Saudi Arabia"), ("departure", DEPARTURE),
        ("return_date", RETURN), ("purpose", "Client visit"),
        ("estimated_cost", "3,450.5"), ("manager_email", "Dana@Sidra.example")]


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle
        agent = agents["applications"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_the_forms_are_listed_with_their_steps(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "applications.forms.list", {})
        assert status == "success", result
        forms = {f["id"]: f for f in result["forms"]}
        assert set(forms) == {"travel_authorization", "equipment_request"}
        travel = forms["travel_authorization"]["steps"]
        assert [s["field"] for s in travel][:3] == ["traveller", "employee_id", "destination"]
        assert [s for s in travel if s["field"] == "itinerary"][0]["required"] is False


class TestAnswers:
    def test_each_answer_is_checked_and_the_next_step_follows(self, agents):
        ex, provider = make()
        ref = started(agents, ex)
        first, _ = invoke(agents, ex, "applications.applications.next", {"application_ref": ref})
        assert (first["done"], first["step"]["field"], first["remaining"]) == (False, "traveller", 10)

        # A pattern refused with its hint; nothing recorded.
        refused, status = answer(agents, ex, ref, "employee_id", "12345")
        assert status == "success" and refused["accepted"] is False
        assert "two capital letters" in refused["problems"][0]
        assert refused["next"]["step"]["field"] == "traveller"

        for field, value in TRIP:
            accepted, status = answer(agents, ex, ref, field, value)
            assert status == "success" and accepted["accepted"] is True, (field, accepted)
        # Normalised as the form says: to the cent, lower-case, canonical.
        answers = {row["keys"]["field"]: row["keys"]["value"]
                   for row in provider.data["applications__answer"].values()}
        assert answers["estimated_cost"] == "3450.50"
        assert answers["manager_email"] == "dana@sidra.example"
        assert answers["purpose"] == "Client visit"
        record = provider.data["applications__application"][ref]
        assert record["keys"]["progress"] == "8 of 10"
        after, _ = invoke(agents, ex, "applications.applications.next", {"application_ref": ref})
        assert after["step"]["field"] == "passport" and after["remaining"] == 2

    def test_dates_choices_and_numbers_are_held_to_the_form(self, agents):
        ex, _ = make()
        ref = started(agents, ex)
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        assert "past" in answer(agents, ex, ref, "departure", yesterday)[0]["problems"][0]
        assert "YYYY-MM-DD" in answer(agents, ex, ref, "departure", "12/09/2026")[0]["problems"][0]
        assert answer(agents, ex, ref, "departure", DEPARTURE)[0]["accepted"]
        before = (TODAY + timedelta(days=29)).isoformat()
        assert "after departure" in answer(agents, ex, ref, "return_date", before)[0]["problems"][0]
        assert "not one of the choices" in answer(agents, ex, ref, "purpose", "Holiday")[0]["problems"][0]
        assert answer(agents, ex, ref, "purpose", "conference")[0]["value"] == "Conference"
        assert "at most 50000" in answer(agents, ex, ref, "estimated_cost", "70000")[0]["problems"][0]
        assert "not a number" in answer(agents, ex, ref, "estimated_cost", "lots")[0]["problems"][0]
        assert "not an email" in answer(agents, ex, ref, "manager_email", "dana")[0]["problems"][0]
        assert "takes a document" in answer(agents, ex, ref, "passport", "here")[0]["problems"][0]

    def test_an_optional_step_can_be_skipped_and_a_required_one_cannot(self, agents):
        ex, _ = make()
        ref = started(agents, ex, "equipment_request")
        assert "required" in answer(agents, ex, ref, "requester", "")[0]["problems"][0]
        skipped, _ = answer(agents, ex, ref, "details", "")
        assert skipped["accepted"] is True and skipped["value"] == ""
        review, _ = invoke(agents, ex, "applications.applications.review", {"application_ref": ref})
        assert [a for a in review["answers"] if a["field"] == "details"][0]["skipped"] is True


class TestDocuments:
    def test_a_document_is_checked_read_and_held_to_the_rules(self, agents):
        ex, provider = make()
        ref = started(agents, ex)
        for field, value in TRIP:
            answer(agents, ex, ref, field, value)

        # The wrong kind of file is refused before anything is read.
        wrong = upload(provider, "notes.txt", b"just words")
        refused, status = invoke(agents, ex, "applications.applications.attach",
                                 {"application_ref": ref, "field": "passport", "file_ref": wrong})
        assert status == "success" and refused["accepted"] is False
        assert "this step takes application/pdf" in refused["problems"][0]

        # Read by the model from the text, and held to the six-month rule.
        soon = upload(provider, "passport-soon.pdf", PASSPORT_SOON)
        refused, _ = invoke(agents, ex, "applications.applications.attach",
                            {"application_ref": ref, "field": "passport", "file_ref": soon})
        assert refused["accepted"] is False
        assert refused["problems"] == ["The passport must be valid for at least six months after the return date."]
        assert refused["document"]["read"]["expiry"] == {"value": EXPIRY_SOON, "status": "verified"}
        assert not provider.data.get("applications__attachment")

        # A valid one: verified from the text, recorded, and the form moves on.
        good = upload(provider, "passport.pdf", PASSPORT_OK)
        accepted, _ = invoke(agents, ex, "applications.applications.attach",
                             {"application_ref": ref, "field": "passport", "file_ref": good})
        assert accepted["accepted"] is True, accepted
        document = accepted["document"]
        assert (document["pages"], document["type"]) == (1, "application/pdf")
        assert document["read"]["passport_number"] == {"value": "P7654321", "status": "verified"}
        assert accepted["next"]["step"]["field"] == "itinerary"
        [kept] = provider.data["applications__attachment"].values()
        assert kept["keys"]["file_ref"] == good
        assert json.loads(kept["keys"]["read"])["expiry"]["status"] == "verified"

    def test_a_reading_the_text_does_not_carry_is_assumed_not_verified(self, agents):
        ex, provider = make()
        ref = started(agents, ex)
        for field, value in TRIP:
            answer(agents, ex, ref, field, value)
        misread = upload(provider, "passport-misread.pdf", PASSPORT_OK)
        accepted, _ = invoke(agents, ex, "applications.applications.attach",
                             {"application_ref": ref, "field": "passport", "file_ref": misread})
        assert accepted["accepted"] is True
        assert accepted["document"]["read"]["passport_number"]["status"] == "assumed"
        assert accepted["document"]["read"]["expiry"]["status"] == "verified"

    def test_a_photo_is_shown_to_the_model_and_everything_read_is_assumed(self, agents):
        ex, provider = make()
        model.pictures.clear()
        ref = started(agents, ex)
        for field, value in TRIP:
            answer(agents, ex, ref, field, value)
        photo = upload(provider, "passport.png", PHOTO)
        accepted, status = invoke(agents, ex, "applications.applications.attach",
                                  {"application_ref": ref, "field": "passport", "file_ref": photo})
        assert status == "success" and accepted["accepted"] is True, accepted
        assert accepted["document"]["type"] == "image/png"
        assert accepted["document"]["read"]["passport_number"] == {"value": "P9988776", "status": "assumed"}
        # The model was shown the picture itself — its bytes, by the platform.
        [[shown]] = model.pictures
        assert shown["mime"] == "image/png"


class TestTheAgentAsks:
    def test_the_agent_asks_each_step_itself_and_records_what_came_back(self, agents):
        """applications.applications.step is the intake in one function: the question
        as a card of the right shape, the answer checked and recorded,
        one step per call — a restart between two loses nothing."""
        script = iter([
            "Omar Haddad", "SO-12345", "Riyadh, Saudi Arabia", DEPARTURE,
            "too soon",            # refused: not a date — asked again
            RETURN, "Client visit", "3450.50", "dana@sidra.example",
            "PASSPORT_FILE",       # the attach button: a file ref comes back
            "skip",                # the optional itinerary
        ])
        asked = []
        provider_holder = {}

        async def asker(question, choices, source, expects=""):
            asked.append((question, choices, expects))
            answer_text = next(script)
            if answer_text == "PASSPORT_FILE":
                created = await provider_holder["provider"].create_file(
                    "chat_attachment", "passport.pdf", PASSPORT_OK)
                return created["resource_ref"]
            return answer_text

        ex, provider = make(asker=asker)
        provider_holder["provider"] = provider
        ref = started(agents, ex)
        outcomes = []
        for _ in range(12):
            outcome, status = invoke(agents, ex, "applications.applications.step", {"application_ref": ref})
            assert status == "success", outcome
            outcomes.append(outcome)
            if outcome["done"]:
                break
        assert outcomes[-1]["done"] is True
        assert len(outcomes) == 12 - 1                      # eleven asks, the last says done
        refused = [o for o in outcomes if not o["accepted"]]
        assert [o["field"] for o in refused] == ["return_date"]
        assert "YYYY-MM-DD" in refused[0]["problems"][0]

        # The cards had the right shape: choices as buttons, a date's
        # format in the question, the passport as an attach button.
        purpose = next(q for q in asked if q[0].startswith("What is the trip for"))
        assert purpose[1] == ["Client visit", "Conference", "Training", "Other"]
        departure = next(q for q in asked if q[0].startswith("Departure date"))
        assert "(YYYY-MM-DD)" in departure[0] and departure[2] == ""
        passport = next(q for q in asked if q[0].startswith("A copy of the traveller"))
        assert passport[2] == "file" and passport[1] == []
        assert "attach a PDF or a PNG photo or a JPEG photo" in passport[0]

        review, _ = invoke(agents, ex, "applications.applications.review", {"application_ref": ref})
        assert review["ready"] is True and review["missing"] == []
        assert review["documents"][0]["read"]["expiry"]["status"] == "verified"

    def test_with_nobody_to_ask_the_step_says_so(self, agents):
        ex, _ = make()
        ref = started(agents, ex)
        result, status = invoke(agents, ex, "applications.applications.step", {"application_ref": ref})
        assert status == "error" and result["kind"] == "no_audience"


class TestSubmission:
    def test_submission_is_the_persons_own_step_and_closes_the_record(self, agents):
        ex, provider = make()
        ref = started(agents, ex)
        for field, value in TRIP:
            answer(agents, ex, ref, field, value)

        # Not ready: the passport is missing.
        review, _ = invoke(agents, ex, "applications.applications.review", {"application_ref": ref})
        assert review["ready"] is False and review["missing"] == ["passport"]
        result, status = invoke(agents, ex, "applications.applications.submit",
                                {"application_ref": ref}, chat_level=3)
        assert status == "error" and result["kind"] == "incomplete"

        good = upload(provider, "passport.pdf", PASSPORT_OK)
        invoke(agents, ex, "applications.applications.attach",
               {"application_ref": ref, "field": "passport", "file_ref": good})
        # Level 3: refused at the standard trust with nobody to approve.
        result, status = invoke(agents, ex, "applications.applications.submit", {"application_ref": ref})
        assert status != "success"
        submitted, status = invoke(agents, ex, "applications.applications.submit",
                                   {"application_ref": ref}, chat_level=3)
        assert status == "success", submitted
        assert submitted["reference"].startswith("APP-")
        keys = provider.data["applications__application"][ref]["keys"]
        assert (keys["status"], keys["reference"]) == ("submitted", submitted["reference"])

        # Closed: nothing more is recorded.
        closed, status = answer(agents, ex, ref, "destination", "Jeddah")
        assert status == "error" and closed["kind"] == "closed"
        listed, _ = invoke(agents, ex, "applications.applications.list", {"status": "submitted"})
        assert [a["reference"] for a in listed["applications"]] == [submitted["reference"]]

    def test_a_draft_can_be_withdrawn(self, agents):
        ex, _ = make()
        ref = started(agents, ex, "equipment_request")
        result, status = invoke(agents, ex, "applications.applications.withdraw", {"application_ref": ref})
        assert status == "success" and result["status"] == "withdrawn"
        assert invoke(agents, ex, "applications.applications.step", {"application_ref": ref})[1] == "error"
