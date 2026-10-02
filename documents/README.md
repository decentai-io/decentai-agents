# Documents (`documents`)

A forty-page supplier contract arrives as a PDF. Ask what the notice
period is and the answer comes with the page and the sentence it was
read from. Ask what changed since the last version and the differences
are listed, not guessed.

Reads uploaded PDF, Word and text documents page by page, says which
pages could not be read, extracts named fields with a quoted passage
behind each, compares two versions deterministically, fills Word
templates, and produces Word and PDF files that are read back before
they are returned.

No external service and no credential. Files come in as platform
files — a person's upload in the chat, or something another agent
saved (Email's `search.save_attachment`) — by `file_ref`.

## Supported formats

| Kind | Read | Produced |
|---|---|---|
| PDF with a text layer | yes, per page | yes (`produce.pdf`) |
| Scanned PDF, image-only pages | no — those pages are listed as unreadable, never skipped silently | — |
| Word `.docx` | yes, paragraphs and tables, headings listed | yes (`produce.docx`, `produce.fill_template`) |
| Plain text, Markdown, CSV | yes, as one page | — |
| Legacy `.doc`, spreadsheets, images | no — reported as unsupported | — |

## Functions

| Function | Level | What it does |
|---|---|---|
| `read.inspect` | 0 | Kind, page count, characters per page, readable and unreadable page numbers, Word headings |
| `read.text` | 0 | The text by page, from a page, up to a character budget; says where to continue |
| `read.extract` | 1, uses the chat's model | Named fields with the passage that states each and its page. A field the document does not state is `found: false`; a quote not found verbatim in the text is `verified: false`. Recorded |
| `compare.versions` | 0 | Added, removed and changed passages between two files with their pages, focus words first |
| `produce.docx` | 2 | A Word file from a title and sections with paragraphs and tables; read back |
| `produce.pdf` | 2 | A PDF from the same data; read back and its page count reported |
| `produce.fill_template` | 2 | A Word template's `{{placeholders}}` filled from values; unfilled ones are listed, never invented |

Records: `extract` (what was read from which file) and `output` (every
file produced). Files: `source` (inputs) and `document` (outputs).

## Evidence, not opinion

`read.extract` asks the chat's model for the value *and* the passage
that states it, then checks the passage against the document's text
itself. A field comes back in one of three states: found and verified
on a page; found but unverified, meaning the model's quote is not in
the text verbatim and the value should be checked; or not found,
meaning the document does not state it. The agent never fills in a
price, a date or a term the document lacks.

`compare.versions` is sequence matching over paragraphs. It reports
what changed and where; what the change *means* is the assistant's to
say, with the passages in front of it.

## Limits, and what is not verified

- Reading depends on a text layer. OCR is not part of this agent; a
  scanned page is reported as unreadable.
- Extraction quality depends on the chat's model; the verification
  step catches a misquote, not a misreading of a verified passage.
- Templates: a placeholder's replacement lands in the paragraph's
  first run, so character-level formatting inside that paragraph may
  be lost. Paragraph styles survive.
- PDFs are produced with core fonts; characters outside Latin-1 are
  replaced.
- Produced files are verified by reading them back with the same
  library that wrote them and, for PDFs, extracting text; rendered
  appearance is not inspected.
- The tests run the agent in a real worker; every fixture file is
  produced by the agent itself or is a hand-written minimal PDF.
