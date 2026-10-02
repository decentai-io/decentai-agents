"""Writing a workbook someone can open — and reading it back before it
is returned. Cells are written as what they were: a formula as a
formula, a date as a date, a number as a number, an identifier as
text. The last sheet is always the change log."""

from __future__ import annotations

import base64
import io
from typing import Any, Dict, List, Tuple

from .tables import Cell, Sheet


def _write_cell(ws, row: int, column: int, cell: Cell) -> None:
    if cell.formula:
        ws.cell(row=row, column=column, value=cell.formula)
    elif cell.value is not None:
        written = ws.cell(row=row, column=column, value=cell.value)
        if isinstance(cell.value, float):
            written.number_format = "#,##0.00"


def _dress(ws) -> None:
    """A header someone can find and columns wide enough to read: bold
    first row, kept in view, each column as wide as what it holds."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for index, column in enumerate(ws.iter_cols(values_only=True), 1):
        longest = max((len(str(v)) for v in column if v is not None), default=0)
        ws.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 8), 60)


def build(sheets: List[Tuple[str, List[str], List[List[Cell]]]],
          change_log: List[Tuple[str, str, str]]) -> bytes:
    """sheets: (name, columns, rows of Cells). change_log: (row, action,
    detail) tuples, written as the last sheet."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, columns, rows in sheets:
        ws = wb.create_sheet(title=name[:31])
        for column, header in enumerate(columns, 1):
            ws.cell(row=1, column=column, value=header)
        for r, row in enumerate(rows, 2):
            for column, cell in enumerate(row, 1):
                _write_cell(ws, r, column, cell)
        _dress(ws)
    log = wb.create_sheet(title="Change Log")
    for column, header in enumerate(("Row", "Action", "Detail"), 1):
        log.cell(row=1, column=column, value=header)
    for r, (row, action, detail) in enumerate(change_log, 2):
        log.cell(row=r, column=1, value=row)
        log.cell(row=r, column=2, value=action)
        log.cell(row=r, column=3, value=detail)
    _dress(log)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def verify(raw: bytes) -> Dict[str, Any]:
    """Open what was written and count what is there."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True)
    return {"sheets": wb.sheetnames,
            "rows": {name: wb[name].max_row for name in wb.sheetnames}}


async def deliver(call, filename: str, raw: bytes) -> Tuple[str, bool, Dict[str, Any]]:
    name = filename if filename.lower().endswith(".xlsx") else filename + ".xlsx"
    saved = await call.resources.create_file(
        "workbook", name, content_base64=base64.b64encode(raw).decode("ascii"))
    check = verify(raw)
    return saved["resource_ref"], True, {"filename": name, **check}


def text_rows(sheet: Sheet, rows: List[List[Cell]], extra: Dict[str, List[Any]] = None):
    """Rows as Cells again, with optional extra text columns appended
    (row numbers, reasons), for the sheets a reconciliation writes."""
    extra = extra or {}
    columns = list(sheet.columns) + list(extra)
    out = []
    for index, row in enumerate(rows):
        cells = list(row) + [Cell(None)] * (len(sheet.columns) - len(row))
        for name in extra:
            cells.append(Cell(extra[name][index]))
        out.append(cells)
    return columns, out
