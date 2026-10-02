"""The Presentations agent in a real worker. Every fixture deck is one
the agent produced itself, so a test that reads proves what a test that
writes claimed.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

DECK = {
    "filename": "q3-review",
    "title": "Q3 review — Northlight Seating",
    "subtitle": "Prepared for the board, 14 October",
    "purpose": "board pack",
    "slides": [
        {"title": "Where we stand",
         "bullets": ["Revenue up 12% on Q2.",
                     "Two accounts still unsigned.",
                     "Delivery times unchanged at 10 working days."],
         "notes": "Do not promise the Aria refresh before January."},
        {"title": "Orders by region",
         "table_columns": ["Region", "Orders", "Value USD"],
         "table_rows": [["North", "128", "23,400"],
                        ["South", "96", "18,900"]]},
        {"title": "A picture goes here"},
    ],
}


def run(awaitable):
    return asyncio.run(awaitable)


def make():
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["slides"], name, inputs, chat_level=chat_level))


def upload(provider, filename, raw: bytes) -> str:
    """A person's upload, as the sim stores it: bytes under a ref."""
    record = run(provider.create_file("chat_attachment", filename, raw))
    return record["resource_ref"]


@pytest.fixture
def deck(agents):
    ex, provider = make()
    result, status = invoke(agents, ex, "slides.produce.pptx", DECK)
    assert status == "success", result
    raw = provider.files["slides__deck"][result["file_ref"]]["content"]
    return ex, provider, result, upload(provider, result["filename"], raw)


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["slides"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []


class TestProducing:
    def test_a_deck_is_produced_and_read_back(self, agents, deck):
        _, provider, result, _ = deck
        assert result["verified"] is True
        assert result["filename"] == "q3-review.pptx"
        assert result["slides"] == 4          # a title slide, then three
        assert result["templated"] is False
        raw = provider.files["slides__deck"][result["file_ref"]]["content"]
        assert raw[:2] == b"PK"
        record = provider.data["slides__output"][result["output_ref"]]
        assert record["keys"]["slides"] == 4
        assert record["keys"]["purpose"] == "board pack"

    def test_a_ragged_table_is_refused(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "x", "title": "t",
            "slides": [{"title": "s", "table_columns": ["a", "b"],
                        "table_rows": [["1"]]}]})
        assert status == "error" and "one cell per column" in result["error"]

    def test_a_table_without_columns_is_refused(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "x", "title": "t",
            "slides": [{"title": "s", "table_rows": [["1"]]}]})
        assert status == "error" and "table_columns" in result["error"]

    def test_producing_a_deck_needs_level_two(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "slides.produce.pptx", DECK,
                                chat_level=1)
        assert status == "error" and result.get("denied") is True


class TestReading:
    def test_inspect_names_every_slide_and_the_empty_one(self, agents, deck):
        ex, _, _, ref = deck
        info, status = invoke(agents, ex, "slides.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "pptx" and info["slides"] == 4
        titles = [row["title"] for row in info["titles"]]
        assert titles[0] == "Q3 review — Northlight Seating"
        assert "Orders by region" in titles
        # The slide with only a title still has words; nothing here is
        # picture-only, so no slide is reported empty.
        assert info["empty_slides"] == []
        assert info["has_notes"] is True

    def test_text_reads_bullets_tables_and_notes_by_slide(self, agents, deck):
        ex, _, _, ref = deck
        text, status = invoke(agents, ex, "slides.read.text", {"file_ref": ref})
        assert status == "success", text
        by_number = {s["slide"]: s for s in text["slides"]}
        assert "Revenue up 12% on Q2." in by_number[2]["text"]
        assert by_number[2]["notes"].startswith("Do not promise")
        assert "North | 128 | 23,400" in by_number[3]["text"]
        assert text["truncated"] is False

    def test_notes_can_be_left_out(self, agents, deck):
        ex, _, _, ref = deck
        text, _ = invoke(agents, ex, "slides.read.text",
                         {"file_ref": ref, "with_notes": False})
        assert all("notes" not in slide for slide in text["slides"])

    def test_reading_is_paged_by_a_character_budget(self, agents):
        """A deck too long to read at once comes back in pieces, and says
        which slide to carry on from."""
        ex, provider = make()
        built, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "long", "title": "A long deck",
            "slides": [{"title": f"Slide {n}", "bullets": ["word " * 90]}
                       for n in range(1, 7)]})
        assert status == "success", built
        raw = provider.files["slides__deck"][built["file_ref"]]["content"]
        ref = upload(provider, "long.pptx", raw)

        text, _ = invoke(agents, ex, "slides.read.text",
                         {"file_ref": ref, "max_chars": 500})
        assert text["truncated"] is True
        assert text["next_slide"] == len(text["slides"]) + 1
        rest, _ = invoke(agents, ex, "slides.read.text",
                         {"file_ref": ref, "from_slide": text["next_slide"]})
        assert rest["slides"][0]["slide"] == text["next_slide"]

    def test_a_slide_with_no_words_is_reported_empty(self, agents):
        """What a picture-only slide looks like to a text reader: the
        slide exists, and it says nothing."""
        ex, provider = make()
        built, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "gaps", "title": "A deck with a silent slide",
            "slides": [{"title": "Said something", "bullets": ["A point."]},
                       {}]})
        assert status == "success", built
        raw = provider.files["slides__deck"][built["file_ref"]]["content"]
        ref = upload(provider, "gaps.pptx", raw)
        info, _ = invoke(agents, ex, "slides.read.inspect", {"file_ref": ref})
        assert info["slides"] == 3 and info["empty_slides"] == [3]
        text, _ = invoke(agents, ex, "slides.read.text", {"file_ref": ref})
        silent = [s for s in text["slides"] if s["slide"] == 3][0]
        assert silent["title"] == "" and silent["text"] == ""

    def test_a_file_that_is_not_a_deck_is_named_not_guessed(self, agents):
        ex, provider = make()
        ref = upload(provider, "notes.txt", b"not a deck at all")
        info, status = invoke(agents, ex, "slides.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "unsupported" and "PowerPoint" in info["problem"]
        text, status = invoke(agents, ex, "slides.read.text", {"file_ref": ref})
        assert status == "error" and "PowerPoint" in text["error"]


class TestTemplates:
    def test_a_deck_built_on_a_template_drops_the_templates_own_slides(
            self, agents, deck):
        """The produced deck doubles as a template: building on it must
        carry over its design, not its content."""
        ex, provider, _, template = deck
        result, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "built-on-template", "title": "New deck",
            "template_ref": template,
            "slides": [{"title": "Only this", "bullets": ["One point."]}]})
        assert status == "success", result
        assert result["templated"] is True and result["slides"] == 2
        raw = provider.files["slides__deck"][result["file_ref"]]["content"]
        ref = upload(provider, result["filename"], raw)
        text, _ = invoke(agents, ex, "slides.read.text", {"file_ref": ref})
        said = " ".join(s["title"] + s["text"] for s in text["slides"])
        assert "Only this" in said
        assert "Orders by region" not in said

    def test_placeholders_are_filled_and_the_unfilled_are_named(self, agents):
        ex, provider = make()
        built, status = invoke(agents, ex, "slides.produce.pptx", {
            "filename": "template", "title": "Report for {{client}}",
            "slides": [{"title": "Prepared by {{author}}",
                        "bullets": ["Total: {{total}}", "Date: {{date}}"],
                        "notes": "Chase {{client}} on Monday."}]})
        assert status == "success", built
        raw = provider.files["slides__deck"][built["file_ref"]]["content"]
        template = upload(provider, "template.pptx", raw)

        result, status = invoke(agents, ex, "slides.produce.fill_template", {
            "template_ref": template, "filename": "filled",
            "values": {"client": "Harbourline", "total": "USD 3,700"}})
        assert status == "success", result
        assert result["filled"] == ["client", "total"]
        assert result["unfilled"] == ["author", "date"]
        assert result["verified"] is True

        filled_raw = provider.files["slides__deck"][result["file_ref"]]["content"]
        ref = upload(provider, "filled.pptx", filled_raw)
        text, _ = invoke(agents, ex, "slides.read.text", {"file_ref": ref})
        said = " ".join(s["title"] + s["text"] + s.get("notes", "")
                        for s in text["slides"])
        assert "Report for Harbourline" in said
        assert "Total: USD 3,700" in said
        assert "{{author}}" in said          # left alone, not invented
        assert "Chase Harbourline on Monday." in said

    def test_a_template_that_is_not_a_deck_is_refused(self, agents):
        ex, provider = make()
        ref = upload(provider, "notes.txt", b"not a deck")
        result, status = invoke(agents, ex, "slides.produce.fill_template", {
            "template_ref": ref, "filename": "x", "values": {}})
        assert status == "error" and "PowerPoint" in result["error"]
