"""The Documents agent in a real worker. The fixtures are files the
agent produces itself (Word and PDF), plain text, and a hand-written
one-page PDF with no text layer — a scan, as far as a reader can tell.
"""

import asyncio
import base64
import json

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from ai_runtime.sinks import ChatSinks
from sim.resources import InMemoryResourceProvider

def scanned_pdf() -> bytes:
    """A minimal, valid one-page PDF with no text layer — what a scanned
    page looks like to a text extractor. Written by hand, cross-reference
    table included, so a strict reader accepts it."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


SCANNED_PDF = scanned_pdf()

QUOTE_A = {
    "filename": "northlight-q2041.pdf", "title": "Quotation Q-2041 — Northlight Seating",
    "sections": [
        {"heading": "Offer", "paragraphs": [
            "20 x Aria task chair, black mesh, adjustable arms.",
            "Unit price USD 185.00, total USD 3,700.00 excluding shipping.",
            "Delivery within 10 working days of order. Shipping USD 120.00.",
            "Warranty 5 years on mechanism, 2 years on fabric."]},
        {"heading": "Terms", "paragraphs": ["Payment 30 days from invoice."]},
    ],
}
QUOTE_B = {
    "filename": "northlight-q2041-rev2.pdf", "title": "Quotation Q-2041 rev 2 — Northlight Seating",
    "sections": [
        {"heading": "Offer", "paragraphs": [
            "20 x Aria task chair, black mesh, adjustable arms.",
            "Unit price USD 179.00, total USD 3,580.00 excluding shipping.",
            "Delivery within 15 working days of order. Shipping USD 120.00.",
            "Warranty 5 years on mechanism, 2 years on fabric."]},
        {"heading": "Terms", "paragraphs": ["Payment 30 days from invoice.",
                                            "Prices valid until 30 September 2026."]},
    ],
}


def run(awaitable):
    return asyncio.run(awaitable)


def make(llm=None):
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider, sinks=ChatSinks(llm=llm)), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["documents"], name, inputs, chat_level=chat_level))


def upload(provider, filename, raw: bytes) -> str:
    """A person's upload, as the sim stores it: bytes under a ref."""
    record = run(provider.create_file("chat_attachment", filename, raw))
    return record["resource_ref"]


@pytest.fixture
def quotes(agents):
    """Two quotation PDFs, produced by the agent itself."""
    ex, provider = make()
    a, status = invoke(agents, ex, "documents.produce.pdf", QUOTE_A)
    assert status == "success", a
    b, status = invoke(agents, ex, "documents.produce.pdf", QUOTE_B)
    assert status == "success", b
    return ex, provider, a["file_ref"], b["file_ref"]


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["documents"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []


class TestProducing:
    def test_a_pdf_is_produced_and_read_back(self, agents):
        ex, provider = make()
        result, status = invoke(agents, ex, "documents.produce.pdf", QUOTE_A)
        assert status == "success", result
        assert result["verified"] is True and result["pages"] == 1
        assert result["filename"] == "northlight-q2041.pdf"
        raw = provider.files["documents__document"][result["file_ref"]]["content"]
        assert raw[:5] == b"%PDF-"
        record = provider.data["documents__output"][result["output_ref"]]
        assert record["keys"]["kind"] == "pdf"

    def test_a_word_file_is_produced_with_its_table(self, agents):
        ex, provider = make()
        result, status = invoke(agents, ex, "documents.produce.docx", {
            "filename": "comparison", "title": "Chair quotations compared",
            "purpose": "demo A", "sections": [
                {"heading": "Summary", "paragraphs": ["Three quotations were received."]},
                {"heading": "Prices", "table_columns": ["Supplier", "Unit USD", "Delivery"],
                 "table_rows": [["Northlight", "185.00", "10 working days"],
                                ["Cedar Office", "172.00", "unknown"]]}]})
        assert status == "success", result
        assert result["filename"] == "comparison.docx" and result["verified"] is True
        assert result["paragraphs"] >= 2
        raw = provider.files["documents__document"][result["file_ref"]]["content"]
        assert raw[:2] == b"PK"
        # And the agent can read what it wrote, table included.
        ref = upload(provider, "comparison.docx", raw)
        text, status = invoke(agents, ex, "documents.read.text", {"file_ref": ref})
        assert status == "success", text
        page = text["pages"][0]["text"]
        assert "Three quotations were received." in page
        assert "Cedar Office | 172.00 | unknown" in page

    def test_a_ragged_table_is_refused(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "documents.produce.docx", {
            "filename": "x", "title": "t", "sections": [
                {"table_columns": ["a", "b"], "table_rows": [["1"]]}]})
        assert status == "error" and "one cell per column" in result["error"]

    def test_producing_a_file_needs_level_two(self, agents):
        ex, _ = make()
        result, status = invoke(agents, ex, "documents.produce.pdf", QUOTE_A, chat_level=1)
        assert status == "error" and result.get("denied") is True


class TestReading:
    def test_inspect_and_text_read_a_pdf_by_page(self, agents, quotes):
        ex, _, a, _ = quotes
        info, status = invoke(agents, ex, "documents.read.inspect", {"file_ref": a})
        assert status == "success", info
        assert info["kind"] == "pdf" and info["pages"] == 1
        assert info["readable_pages"] == [1] and info["unreadable_pages"] == []
        text, status = invoke(agents, ex, "documents.read.text", {"file_ref": a})
        assert status == "success"
        assert text["pages"][0]["page"] == 1
        assert "Unit price USD 185.00" in text["pages"][0]["text"]
        assert text["truncated"] is False

    def test_a_scanned_page_is_reported_not_skipped(self, agents):
        ex, provider = make()
        ref = upload(provider, "scan.pdf", SCANNED_PDF)
        info, status = invoke(agents, ex, "documents.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["pages"] == 1 and info["unreadable_pages"] == [1]
        assert info["readable_pages"] == []
        text, _ = invoke(agents, ex, "documents.read.text", {"file_ref": ref})
        assert text["unreadable_pages"] == [1] and text["pages"][0]["text"] == ""
        result, status = invoke(agents, ex, "documents.read.extract", {
            "file_ref": ref, "fields": [{"name": "total"}]})
        assert status == "error" and "readable" in result["error"]

    def test_text_and_csv_are_one_page_and_unsupported_is_named(self, agents):
        ex, provider = make()
        ref = upload(provider, "notes.csv", b"item,qty\nchair,20\n")
        info, _ = invoke(agents, ex, "documents.read.inspect", {"file_ref": ref})
        assert info["kind"] == "csv" and info["pages"] == 1
        text, _ = invoke(agents, ex, "documents.read.text", {"file_ref": ref})
        assert text["pages"][0]["text"] == "item | qty\nchair | 20"
        ref = upload(provider, "photo.jpg", b"\xff\xd8\xff\xe0 not a document")
        info, _ = invoke(agents, ex, "documents.read.inspect", {"file_ref": ref})
        assert info["kind"] == "unsupported" and "Not a PDF" in info["problem"]

    def test_text_is_paged_by_a_character_budget(self, agents):
        ex, provider = make()
        ref = upload(provider, "long.txt", ("word " * 400).encode())
        text, _ = invoke(agents, ex, "documents.read.text",
                         {"file_ref": ref, "max_chars": 500})
        assert text["truncated"] is True and len(text["pages"][0]["text"]) == 500


class TestExtracting:
    def test_fields_come_with_verified_quotes_and_honest_gaps(self, agents, quotes):
        ex, provider, a, _ = quotes
        seen = {}

        async def llm(messages, max_tokens=None):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps({
                "unit_price": {"found": True, "value": "USD 185.00",
                               "quote": "Unit price USD 185.00, total USD 3,700.00"},
                "delivery": {"found": True, "value": "10 working days",
                             "quote": "Delivery within 10 working days of order."},
                "installation": {"found": False},
                "warranty": {"found": True, "value": "10 years",
                             "quote": "Warranty 10 years on everything."},   # not in the text
            })

        ex.sinks.llm = llm
        result, status = invoke(agents, ex, "documents.read.extract", {
            "file_ref": a, "fields": [
                {"name": "unit_price", "hint": "price per chair"},
                {"name": "delivery"}, {"name": "installation"}, {"name": "warranty"}]})
        assert status == "success", result
        assert "[page 1]" in seen["prompt"] and "price per chair" in seen["prompt"]
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["unit_price"] == {
            "name": "unit_price", "found": True, "value": "USD 185.00",
            "quote": "Unit price USD 185.00, total USD 3,700.00",
            "page": 1, "verified": True}
        assert by_name["installation"] == {"name": "installation", "found": False}
        assert by_name["warranty"]["verified"] is False       # a misquote is flagged
        assert result["found"] == 3 and result["missing"] == 1
        record = provider.data["documents__extract"][result["extract_ref"]]
        assert record["keys"]["missing"] == 1

    def test_extraction_needs_the_chats_model(self, agents, quotes):
        ex, _, a, _ = quotes            # no llm on this executor
        result, status = invoke(agents, ex, "documents.read.extract", {
            "file_ref": a, "fields": [{"name": "total"}]})
        assert status == "error" and "model" in result["error"].lower()


class TestComparing:
    def test_changes_carry_pages_and_focus_words_come_first(self, agents, quotes):
        ex, _, a, b = quotes
        result, status = invoke(agents, ex, "documents.compare.versions", {
            "file_ref_a": a, "file_ref_b": b, "focus": ["price", "delivery"]})
        assert status == "success", result
        assert result["identical"] is False
        assert result["unreadable_a"] == [] and result["unreadable_b"] == []
        assert result["changed"] >= 2 and result["added"] >= 1
        first = result["changes"][0]
        assert first["focus"] is True and first["page_a"] == 1 and first["page_b"] == 1
        texts = [(c["kind"], c["before"], c["after"]) for c in result["changes"]]
        assert any(k == "changed" and "185.00" in bf and "179.00" in af for k, bf, af in texts)
        assert any(k == "changed" and "10 working" in bf and "15 working" in af for k, bf, af in texts)
        assert any(k == "added" and "valid until" in af for k, bf, af in texts)

    def test_the_same_file_twice_is_identical(self, agents, quotes):
        ex, _, a, _ = quotes
        result, _ = invoke(agents, ex, "documents.compare.versions",
                           {"file_ref_a": a, "file_ref_b": a})
        assert result == {"identical": True, "added": 0, "removed": 0, "changed": 0,
                          "truncated": False, "focus_hits": 0,
                          "unreadable_a": [], "unreadable_b": [], "changes": []}


class TestTemplates:
    def test_placeholders_are_filled_and_the_missing_named(self, agents):
        ex, provider = make()
        made, status = invoke(agents, ex, "documents.produce.docx", {
            "filename": "quotation-template", "title": "Quotation for {{customer}}",
            "sections": [{"heading": "Offer", "paragraphs": [
                "Prepared for {{customer}} on {{date}}.",
                "Total: {{total}} (valid {{validity}} days)."],
                "table_columns": ["Item", "Qty"], "table_rows": [["{{item}}", "{{qty}}"]]}]})
        assert status == "success", made
        raw = provider.files["documents__document"][made["file_ref"]]["content"]
        template = upload(provider, "quotation-template.docx", raw)

        result, status = invoke(agents, ex, "documents.produce.fill_template", {
            "template_ref": template, "filename": "harbourline-quotation",
            "values": {"customer": "Harbourline Consulting", "date": "7 September 2026",
                       "total": "USD 4,120.00", "item": "Aria task chair", "qty": "20"}})
        assert status == "success", result
        assert result["filled"] == ["customer", "date", "item", "qty", "total"]
        assert result["unfilled"] == ["validity"]              # asked for, never invented
        assert result["verified"] is True
        filled = provider.files["documents__document"][result["file_ref"]]["content"]
        ref = upload(provider, "out.docx", filled)
        text, _ = invoke(agents, ex, "documents.read.text", {"file_ref": ref})
        page = text["pages"][0]["text"]
        assert "Prepared for Harbourline Consulting on 7 September 2026." in page
        assert "Total: USD 4,120.00 (valid {{validity}} days)." in page
        assert "Aria task chair | 20" in page
