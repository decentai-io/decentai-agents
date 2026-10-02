"""Write the deck the Presentations agent's samples ship.

Run once when the content changes; the .pptx is committed, so loading a
sample never depends on a PowerPoint library being present:

    python tests/make_sample_decks.py
"""

from pathlib import Path

from pptx import Presentation
from pptx.util import Pt

HERE = Path(__file__).resolve().parent.parent / "slides" / "samples"

TITLE = "Q3 review — Sidra Office Supplies"
SUBTITLE = "Prepared for the quarterly review, 14 October 2026"

SLIDES = [
    {"title": "Where we stand",
     "bullets": ["Revenue 412,000 USD, up 12% on Q2.",
                 "Two accounts still unsigned: Harbourline and Cedar Office.",
                 "Delivery times unchanged at 10 working days."],
     "notes": "Do not promise the Aria refresh before January. "
              "Harbourline asked twice."},
    {"title": "Orders by region",
     "table": (["Region", "Orders", "Value USD"],
               [["North", "128", "23,400"],
                ["South", "96", "18,900"],
                ["Coast", "54", "11,250"]]),
     "notes": "Coast is new this quarter; one customer, one large order."},
    {"title": "What we are asking for",
     "bullets": ["Approve the Aria refresh budget of 40,000 USD.",
                 "Hire one more account manager for the Coast region.",
                 "Agree a price review with Northlight Seating before December."],
     "notes": "The chair will want the payback period: 14 months."},
    {"title": "Questions"},
]


def build() -> Presentation:
    presentation = Presentation()

    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    slide.shapes.title.text = TITLE
    slide.placeholders[1].text = SUBTITLE

    for entry in SLIDES:
        bullets = entry.get("bullets") or []
        table = entry.get("table")
        layout = presentation.slide_layouts[1 if bullets else 5]
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = entry["title"]

        if bullets:
            frame = slide.placeholders[1].text_frame
            frame.clear()
            for index, bullet in enumerate(bullets):
                paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                paragraph.text = bullet
        elif not table:
            for placeholder in list(slide.placeholders):
                if placeholder.placeholder_format.idx != 0:
                    placeholder._element.getparent().remove(placeholder._element)

        if table:
            columns, rows = table
            from pptx.util import Inches

            shape = slide.shapes.add_table(
                len(rows) + 1, len(columns), Inches(0.8), Inches(1.9),
                presentation.slide_width - Inches(1.6),
                Inches(0.4) * (len(rows) + 1))
            for index, name in enumerate(columns):
                cell = shape.table.cell(0, index)
                cell.text = name
                for run in cell.text_frame.paragraphs[0].runs:
                    run.font.bold = True
                    run.font.size = Pt(12)
            for r, row in enumerate(rows, 1):
                for c, value in enumerate(row):
                    cell = shape.table.cell(r, c)
                    cell.text = value
                    for run in cell.text_frame.paragraphs[0].runs:
                        run.font.size = Pt(12)

        if entry.get("notes"):
            slide.notes_slide.notes_text_frame.text = entry["notes"]

    return presentation


if __name__ == "__main__":
    HERE.mkdir(parents=True, exist_ok=True)
    path = HERE / "sidra-q3-review.pptx"
    build().save(str(path))
    print(path.name, path.stat().st_size, "bytes")
