"""Producing files people can open — and reading each one back before
it is returned, so a corrupt file is never handed over as done."""

import base64
import io
import re

from decentai_sdk.base import ToolBase

from .reading import DOCX_MIME, load

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _rows(section):
    rows = section.get("table_rows") or []
    return [[str(cell) for cell in row] if isinstance(row, (list, tuple)) else [str(row)]
            for row in rows]


def _sections(inputs):
    out = []
    for section in inputs["sections"]:
        columns = [str(c) for c in section.get("table_columns") or []]
        rows = _rows(section)
        if rows and columns and any(len(r) != len(columns) for r in rows):
            raise ValueError("every table row must have one cell per column")
        out.append({"heading": str(section.get("heading") or ""),
                    "paragraphs": [str(p) for p in section.get("paragraphs") or []],
                    "columns": columns, "rows": rows})
    return out


class ProduceTool(ToolBase):
    id = "produce"

    async def docx(self, call):
        try:
            sections = _sections(call.inputs)
        except ValueError as exc:
            return {"error": str(exc)}, "error"
        raw = self._build_docx(str(call.inputs["title"]), sections)
        filename = self._named(str(call.inputs["filename"]), ".docx")
        return await self._deliver(call, "docx", filename, raw,
                                   str(call.inputs.get("purpose") or ""))

    async def pdf(self, call):
        try:
            sections = _sections(call.inputs)
        except ValueError as exc:
            return {"error": str(exc)}, "error"
        try:
            raw = self._build_pdf(str(call.inputs["title"]), sections)
        except Exception as exc:
            return {"error": f"The PDF could not be built: {exc}"}, "error"
        filename = self._named(str(call.inputs["filename"]), ".pdf")
        return await self._deliver(call, "pdf", filename, raw,
                                   str(call.inputs.get("purpose") or ""))

    async def fill_template(self, call):
        from docx import Document
        from docx.opc.exceptions import PackageNotFoundError

        record = await call.resources.read_file("source", str(call.inputs["template_ref"]))
        if not record.get("content_base64"):
            return {"error": "The template has no content."}, "error"
        try:
            document = Document(io.BytesIO(base64.b64decode(record["content_base64"])))
        except (PackageNotFoundError, KeyError, ValueError) as exc:
            return {"error": f"The template is not a Word file: {exc}"}, "error"

        values = {str(k): str(v) for k, v in (call.inputs.get("values") or {}).items()}
        filled, unfilled = set(), set()

        def fill(paragraph):
            text = paragraph.text
            names = PLACEHOLDER.findall(text)
            if not names:
                return
            for name in names:
                if name in values:
                    filled.add(name)
                else:
                    unfilled.add(name)
            new = PLACEHOLDER.sub(
                lambda m: values.get(m.group(1), m.group(0)), text)
            if new != text and paragraph.runs:
                # The replacement lands in the first run; the paragraph's
                # own style survives, character formatting within it may not.
                paragraph.runs[0].text = new
                for run in paragraph.runs[1:]:
                    run.text = ""

        for paragraph in document.paragraphs:
            fill(paragraph)
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        fill(paragraph)
        buffer = io.BytesIO()
        document.save(buffer)
        filename = self._named(str(call.inputs["filename"]), ".docx")
        result, status = await self._deliver(
            call, "docx", filename, buffer.getvalue(),
            str(call.inputs.get("purpose") or ""))
        if status != "success":
            return result, status
        result["filled"] = sorted(filled)
        result["unfilled"] = sorted(unfilled)
        return result, "success"

    # ------------------------------------------------------------------
    @staticmethod
    def _named(filename: str, suffix: str) -> str:
        name = re.sub(r"[^\w.\- ]", "_", filename).strip() or "document"
        return name if name.lower().endswith(suffix) else name + suffix

    @staticmethod
    def _build_docx(title, sections) -> bytes:
        from docx import Document

        document = Document()
        document.add_heading(title, level=0)
        for section in sections:
            if section["heading"]:
                document.add_heading(section["heading"], level=1)
            for text in section["paragraphs"]:
                document.add_paragraph(text)
            if section["columns"] and section["rows"]:
                table = document.add_table(rows=1, cols=len(section["columns"]))
                table.style = "Table Grid"
                for cell, name in zip(table.rows[0].cells, section["columns"]):
                    cell.text = name
                for row in section["rows"]:
                    for cell, value in zip(table.add_row().cells, row):
                        cell.text = value
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _build_pdf(title, sections) -> bytes:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos

        def latin(text: str) -> str:
            return text.encode("latin-1", "replace").decode("latin-1")

        # Every block starts at the left margin on the next line; the
        # library's default leaves the cursor at the right margin, and
        # the next full-width block then has no width at all.
        flow = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 9, latin(title), **flow)
        pdf.ln(2)
        for section in sections:
            if section["heading"]:
                pdf.set_font("Helvetica", "B", 12)
                pdf.multi_cell(0, 7, latin(section["heading"]), **flow)
            pdf.set_font("Helvetica", size=10)
            for text in section["paragraphs"]:
                pdf.multi_cell(0, 5.5, latin(text), **flow)
                pdf.ln(1)
            if section["columns"] and section["rows"]:
                with pdf.table() as table:
                    table.row([latin(c) for c in section["columns"]])
                    for row in section["rows"]:
                        table.row([latin(c) for c in row])
                pdf.ln(2)
        return bytes(pdf.output())

    async def _deliver(self, call, kind, filename, raw, purpose):
        """Store, read back through the same reader a person's upload
        goes through, and only then answer."""
        saved = await call.resources.create_file(
            "document", filename,
            content_base64=base64.b64encode(raw).decode("ascii"))
        file_ref = saved["resource_ref"]
        verified, pages, paragraphs = False, 0, 0
        try:
            check = self._verify(kind, raw)
            verified, pages, paragraphs = True, check["pages"], check["paragraphs"]
        except Exception as exc:
            return {"error": f"The produced {kind} did not read back: {exc}"}, "error"
        record = await call.resources.create_data("output", {
            "kind": kind, "filename": filename, "file_ref": file_ref,
            "purpose": purpose[:200], "pages": pages})
        result = {"file_ref": file_ref, "filename": filename,
                  "output_ref": record["resource_ref"], "verified": verified}
        if kind == "pdf":
            result["pages"] = pages
        else:
            result["paragraphs"] = paragraphs
        return result, "success"

    @staticmethod
    def _verify(kind, raw: bytes):
        if kind == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "".join((p.extract_text() or "") for p in reader.pages)
            if not text.strip():
                raise ValueError("no text came back from the PDF")
            return {"pages": len(reader.pages), "paragraphs": 0}
        from docx import Document
        document = Document(io.BytesIO(raw))
        count = sum(1 for p in document.paragraphs if p.text.strip())
        return {"pages": 0, "paragraphs": count}
