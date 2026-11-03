"""Assemble the .xlsx workbook.

Three sheets per source file, in this order:

``Original Layout``
    the page rebuilt in cells, with fills, fonts and embedded images;
``Clean Data``
    one record per row, real dates and numbers, frozen header, autofilter;
``Validation``
    the financial checks and their verdicts.

A combined workbook (many files in, one workbook out) adds a ``Summary`` sheet
and one data/layout block per file.
"""
from __future__ import annotations

import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.converter.extract import ExtractResult, parse_date, parse_number
from app.converter.layout import SheetLayout
from app.converter.validation import ERROR, NA, PASSED, WARNING, Check, summarise

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

INK = "1B2A41"
INK_SOFT = "334E68"
HEADER_FILL = PatternFill("solid", fgColor=INK)
HEADER_FONT = Font(name="Calibri", size=10.5, bold=True, color="FFFFFF")
STATUS_STYLE = {
    PASSED: ("E4F3E7", "1E6B33"),
    WARNING: ("FDF3D8", "8A5A00"),
    ERROR: ("FBE3E4", "9C1C1C"),
    NA: ("EEF1F4", "5A6772"),
}
THIN = Side(style="thin", color="D5DBE3")
BORDER = Border(bottom=THIN)


# -------------------------------------------------------------------- building
def build(result: ExtractResult, layouts: list[SheetLayout], checks: list[Check], options: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    sheets = options.get("sheets") or {}

    if sheets.get("layout", True):
        for layout in layouts:
            _layout_sheet(wb.create_sheet(_sheet_name(layout.name)), layout)
    if sheets.get("data", True):
        _data_sheet(wb.create_sheet("Clean Data"), result)
    if sheets.get("validation", True):
        _validation_sheet(wb.create_sheet("Validation"), checks)

    if not wb.sheetnames:
        _data_sheet(wb.create_sheet("Clean Data"), result)

    wb.properties.title = result.doc_label
    wb.properties.creator = "Firm PDF Workspace"
    return _serialise(wb)


def build_combined(analyses: list[dict], options: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    sheets = options.get("sheets") or {}

    _summary_sheet(wb.create_sheet("Summary"), analyses)

    for index, analysis in enumerate(analyses, start=1):
        stem = _stem(analysis["filename"])
        if sheets.get("layout", True):
            for page, layout in enumerate(analysis["layouts"], start=1):
                name = _sheet_name(f"Layout {index}.{page} {stem}")
                _layout_sheet(wb.create_sheet(name), layout)
        if sheets.get("data", True):
            _data_sheet(wb.create_sheet(_sheet_name(f"Data {index} {stem}")), analysis["extract"])

    if sheets.get("validation", True):
        _combined_validation_sheet(wb.create_sheet("Validation"), analyses)

    wb.properties.title = f"Combined workbook ({len(analyses)} files)"
    wb.properties.creator = "Firm PDF Workspace"
    return _serialise(wb)


def output_name(filename: str) -> str:
    stem = _stem(filename)
    return f"{stem}.xlsx"


# ---------------------------------------------------------------------- sheets
def _layout_sheet(ws, layout: SheetLayout) -> None:
    for (row, col), cell in layout.cells.items():
        target = ws.cell(row=row + 1, column=col + 1, value=_safe_text(cell.value))
        target.font = Font(name="Calibri", size=cell.size or 10, bold=cell.bold, color=INK_SOFT)
        target.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
        if cell.fill:
            target.fill = PatternFill("solid", fgColor=cell.fill)

    for col, width in layout.col_widths.items():
        ws.column_dimensions[get_column_letter(col + 1)].width = width
    for row, height in layout.row_heights.items():
        ws.row_dimensions[row + 1].height = height

    for image in layout.images:
        try:
            xl_image = XLImage(io.BytesIO(image.data))
            xl_image.width = image.width
            xl_image.height = image.height
            ws.add_image(xl_image, f"{get_column_letter(image.col + 1)}{image.row + 1}")
        except Exception:  # a broken crop must not lose the workbook
            continue

    ws.sheet_view.showGridLines = False


def _data_sheet(ws, result: ExtractResult) -> None:
    columns = result.columns
    if not columns:
        ws["A1"] = "No tabular rows were found on these pages."
        ws["A1"].font = Font(name="Calibri", size=11, italic=True, color=INK_SOFT)
        ws.column_dimensions["A"].width = 60
        return

    number_columns = {
        index
        for role, index in result.roles.items()
        if index is not None and role in ("amount", "debit", "credit", "balance")
    }
    date_column = result.roles.get("date")
    text_column = result.roles.get("description")

    for col, column in enumerate(columns, start=1):
        header = ws.cell(row=1, column=col, value=column)
        header.font = HEADER_FONT
        header.fill = HEADER_FILL
        header.alignment = Alignment(horizontal="left", vertical="center")
        header.border = BORDER
    ws.row_dimensions[1].height = 18

    for row_index, record in enumerate(result.records, start=2):
        is_total = result.total_flags[row_index - 2] if row_index - 2 < len(result.total_flags) else False
        for col, column in enumerate(columns, start=1):
            raw = record.get(column, "")
            value, number_format = _typed(raw, col - 1 == date_column, col - 1 in number_columns, result.date_order)
            cell = ws.cell(row=row_index, column=col, value=value)
            cell.font = Font(name="Calibri", size=10, bold=is_total, color=INK_SOFT)
            if number_format:
                cell.number_format = number_format
                cell.alignment = Alignment(horizontal="right", vertical="top")
            elif col - 1 == text_column:
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
            else:
                cell.alignment = Alignment(horizontal="left", vertical="top")
            cell.border = BORDER

    ws.freeze_panes = "A2"
    if result.records:
        last_column = get_column_letter(len(columns))
        ws.auto_filter.ref = f"A1:{last_column}{len(result.records) + 1}"

    for col, column in enumerate(columns, start=1):
        longest = len(column)
        for record in result.records[:400]:
            longest = max(longest, len(str(record.get(column, ""))))
        ws.column_dimensions[get_column_letter(col)].width = float(min(max(10, longest + 2), 52))


def _validation_sheet(ws, checks: list[Check]) -> None:
    headers = ("Check", "Status", "Detail")
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 18

    for row, check in enumerate(checks, start=2):
        ws.cell(row=row, column=1, value=check.name).font = Font(name="Calibri", size=10, bold=True, color=INK)
        status = ws.cell(row=row, column=2, value=check.status)
        fill, colour = STATUS_STYLE.get(check.status, STATUS_STYLE[NA])
        status.fill = PatternFill("solid", fgColor=fill)
        status.font = Font(name="Calibri", size=10, bold=True, color=colour)
        detail = ws.cell(row=row, column=3, value=check.detail)
        detail.font = Font(name="Calibri", size=10, color=INK_SOFT)
        detail.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        ws.row_dimensions[row].height = 30
        for col in range(1, 4):
            ws.cell(row=row, column=col).border = BORDER

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 92
    ws.freeze_panes = "A2"


def _combined_validation_sheet(ws, analyses: list[dict]) -> None:
    headers = ("File", "Check", "Status", "Detail")
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    ws.row_dimensions[1].height = 18

    row = 2
    for analysis in analyses:
        for check in analysis["checks"]:
            ws.cell(row=row, column=1, value=_stem(analysis["filename"])).font = Font(name="Calibri", size=10, color=INK_SOFT)
            ws.cell(row=row, column=2, value=check.name).font = Font(name="Calibri", size=10, bold=True, color=INK)
            status = ws.cell(row=row, column=3, value=check.status)
            fill, colour = STATUS_STYLE.get(check.status, STATUS_STYLE[NA])
            status.fill = PatternFill("solid", fgColor=fill)
            status.font = Font(name="Calibri", size=10, bold=True, color=colour)
            detail = ws.cell(row=row, column=4, value=check.detail)
            detail.font = Font(name="Calibri", size=10, color=INK_SOFT)
            detail.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            row += 1

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 80
    ws.freeze_panes = "A2"


def _summary_sheet(ws, analyses: list[dict]) -> None:
    headers = ("File", "Type", "Rows", "Columns", "Pages", "OCR pages", "Passed", "Warning", "Error", "N/A")
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    ws.row_dimensions[1].height = 18

    for row, analysis in enumerate(analyses, start=2):
        result = analysis["extract"]
        counts = summarise(analysis["checks"])
        values = (
            _stem(analysis["filename"]),
            result.doc_label,
            result.row_count,
            len(result.columns),
            result.pages_total,
            result.ocr_pages,
            counts.get(PASSED, 0),
            counts.get(WARNING, 0),
            counts.get(ERROR, 0),
            counts.get(NA, 0),
        )
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.font = Font(name="Calibri", size=10, color=INK_SOFT)

    widths = (34, 20, 8, 9, 8, 10, 8, 9, 8, 8)
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


# --------------------------------------------------------------------- helpers
def _typed(raw: str, is_date: bool, is_number: bool, date_order: str):
    text = "" if raw is None else str(raw)
    if not text.strip():
        return None, None
    if is_date:
        parsed = parse_date(text, date_order)
        if parsed:
            return datetime(parsed.year, parsed.month, parsed.day), "yyyy-mm-dd"
        return text, None
    if is_number:
        number = parse_number(text)
        if number is not None:
            return (int(number) if float(number).is_integer() else round(number, 2)), "#,##0.00;[Red]-#,##0.00"
        return text, None
    return text, None


def _safe_text(value: str) -> str:
    if value and value.startswith("="):
        return "'" + value
    return value


def _sheet_name(name: str) -> str:
    cleaned = "".join(" " if char in "[]:*?/\\" else char for char in (name or "Sheet")).strip()
    return (cleaned or "Sheet")[:31]


def _stem(filename: str) -> str:
    stem = str(filename or "document").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return stem[:60] or "document"


def _serialise(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# Kept for callers that want the dates as real date objects (tests).
def excel_date(value) -> date | None:
    return parse_date(value)
