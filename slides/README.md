# Presentations (`slides`)

A forty-slide board deck arrives the evening before the meeting. Ask
what it says and the answer comes slide by slide, speaker notes
included, because what a deck means is usually in the notes. Ask for a
deck back and it is built in the company's own template.

Reads uploaded PowerPoint files, says which slides carry only pictures,
and produces decks from an outline that are read back before they are
returned.

No external service and no credential. Files come in as platform
files — a person's upload in the chat, or something another agent
saved — by `file_ref`.

## Supported formats

| Kind | Read | Produced |
|---|---|---|
| PowerPoint `.pptx` | yes, per slide: title, body, tables, speaker notes | yes (`produce.pptx`, `produce.fill_template`) |
| Slides that are only a picture, chart or diagram | no — those slides are listed as empty, never skipped silently | — |
| Legacy `.ppt`, Keynote, PDF | no — reported as unsupported | — |

## Functions

| Function | Level | What it does |
|---|---|---|
| `read.inspect` | 0 | Slide count, every slide's title, the slides with no text, whether there are notes, the layouts used |
| `read.text` | 0 | What the slides say, by slide, from a slide, up to a character budget; says where to continue |
| `produce.pptx` | 2 | A deck from a title and slides, each with bullets, an optional table and optional notes; `template_ref` builds it in an uploaded template's design; read back |
| `produce.fill_template` | 2 | A deck template's `{{placeholders}}` filled from values — titles, body, table cells and notes; unfilled ones are listed, never invented |

Records: `output` (every deck produced). Files: `source` (inputs and
templates) and `deck` (outputs).

## Templates

Pass `template_ref` to `produce.pptx` and the deck is built on that
file: its theme, fonts, colours and layouts are what every new slide
inherits. **The template's own slides are removed first** — a template
is a design, not content — so a brand deck with three example slides
produces a clean deck, not one with the examples still in it.

Layouts are chosen by name (`Title Slide`, `Title and Content`,
`Title Only`), falling back to position when a template names them
something else. A slide with bullets uses the content layout; a slide
with only a table uses the title-only one, and its empty body
placeholder is removed rather than left showing prompt text.

`produce.fill_template` is the other way round: it keeps the template's
slides exactly as they are and only swaps `{{placeholders}}`.

## Limits, and what is not verified

- Reading is text. A chart's underlying numbers, a SmartArt diagram's
  shape and an image's content are not read; a slide with nothing but
  those is reported as empty.
- Speaker notes are read and produced. Comments are not: PowerPoint
  keeps them outside the slide, and writing them into a file other
  people have open is not a safe substitute.
- A placeholder's replacement lands in the paragraph's first run, so
  character-level formatting inside that paragraph may be lost.
  Paragraph and shape styles survive.
- Animations, transitions, slide masters and speaker timings are
  carried over from a template untouched, and are never created.
- Produced decks are verified by reading them back with the same
  library that wrote them and insisting there are slides with words on
  them; rendered appearance is not inspected.
- The tests run the agent in a real worker; every fixture deck is one
  the agent produced itself.
