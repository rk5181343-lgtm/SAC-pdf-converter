"""Assemble the .xlsx workbook.

Three sheets per source document, in this order:

``Original Layout``
    every page of the document rebuilt in cells, one after another on a single
    sheet (with a page marker between pages), fills, fonts and images included;
``Clean Data``
    one record per row, real dates and numbers, frozen header, autofilter, and a
    last ``Source`` column naming the page each row came from. The opening and
    closing balance rows are highlighted here rather than reported separately;
``Validation``
    the financial checks and their verdicts.

A combined workbook (many files in, one workbook out) adds a ``Summary`` sheet,
one stacked layout sheet per file and one ``Clean Data`` sheet holding every
file's rows, with ``Source`` naming both the file and the page.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.converter.extract import ExtractResult, parse_date, parse_number
from app.converter.layout import GridCell, GridImage, SheetLayout
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
# Opening and closing balance rows: tint plus a dark text colour of the same hue.
BALANCE_STYLE = {
    "opening": ("FFF1D6", "7A4B00"),
    "closing": ("DDF0E8", "0F5A50"),
}
SOURCE_HEADER = "Source"
THIN = Side(style="thin", color="D5DBE3")
BORDER = Border(bottom=THIN)

OPENING_PATTERN = re.compile(r"opening balance|balance b/?f\b|brought forward", re.IGNORECASE)
CLOSING_PATTERN = re.compile(r"closing balance|balance c/?f\b|carried forward", re.IGNORECASE)


# -------------------------------------------------------------------- building
def build(result: ExtractResult, layouts: list[SheetLayout], checks: list[Check], options: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    sheets = options.get("sheets") or {}

    if sheets.get("layout", True):
        stacked = _stack_layouts(layouts)
        if stacked is not None and not stacked.is_empty():
            _layout_sheet(wb.create_sheet("Original Layout"), stacked)
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
        if not sheets.get("layout", True):
            break
        stacked = _stack_layouts(analysis["layouts"])
        if stacked is None or stacked.is_empty():
            continue
        stem = _stem(analysis["filename"])
        _layout_sheet(wb.create_sheet(_sheet_name(f"Layout {index} {stem}")), stacked)

    if sheets.get("data", True):
        _combined_data_sheet(wb.create_sheet("Clean Data"), analyses)
    if sheets.get("validation", True):
        _combined_validation_sheet(wb.create_sheet("Validation"), analyses)

    wb.properties.title = f"Combined workbook ({len(analyses)} files)"
    wb.properties.creator = "Firm PDF Workspace"
    return _serialise(wb)


def output_name(filename: str) -> str:
    stem = _stem(filename)
    return f"{stem}.xlsx"


# ---------------------------------------------------------------------- sheets
def _stack_layouts(layouts: list[SheetLayout]) -> SheetLayout | None:
    """Every page of one document on a single sheet, in page order."""
    if not layouts:
        return None
    stacked = SheetLayout(name="Original Layout")
    offset = 0
    for index, layout in enumerate(layouts):
        if index:
            # One blank row, then a page marker, so pages never run together.
            marker = offset + 1
            stacked.cells[(marker, 0)] = GridCell(
                value=f"Page {index + 1}", bold=True, size=9, fill="E8EDF3"
            )
            stacked.row_heights[marker] = 15.0
            offset = marker + 1
        for (row, col), cell in layout.cells.items():
            stacked.cells[(row + offset, col)] = cell
        for row, height in layout.row_heights.items():
            stacked.row_heights[row + offset] = height
        for col, width in layout.col_widths.items():
            stacked.col_widths[col] = max(stacked.col_widths.get(col, 0.0), width)
        for image in layout.images:
            stacked.images.append(
                GridImage(
                    row=image.row + offset,
                    col=image.col,
                    data=image.data,
                    width=image.width,
                    height=image.height,
                )
            )
        stacked.rows = max(stacked.rows, offset + layout.rows)
        stacked.cols = max(stacked.cols, layout.cols)
        offset += layout.rows
    return stacked


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


def _data_sheet(ws, result: ExtractResult, source_prefix: str = "") -> None:
    marks = _balance_marks(result)
    rows = [
        {
            "record": record,
            "result": result,
            "source": _source_label(result, index, source_prefix),
            "is_total": is_total,
            "mark": marks.get(index),
        }
        for index, (record, is_total) in enumerate(zip(result.records, result.total_flags))
    ]
    _write_data(ws, list(result.columns), rows)


def _combined_data_sheet(ws, analyses: list[dict]) -> None:
    """One sheet for every file: shared columns where they agree, then extras."""
    columns: list[str] = []
    for analysis in analyses:
        for column in analysis["extract"].columns:
            if column not in columns:
                columns.append(column)

    rows: list[dict] = []
    for analysis in analyses:
        result = analysis["extract"]
        marks = _balance_marks(result)
        prefix = _stem(analysis["filename"])
        for index, (record, is_total) in enumerate(zip(result.records, result.total_flags)):
            rows.append(
                {
                    "record": record,
                    "result": result,
                    "source": _source_label(result, index, prefix),
                    "is_total": is_total,
                    "mark": marks.get(index),
                }
            )
    _write_data(ws, columns, rows)


def _write_data(ws, columns: list[str], rows: list[dict]) -> None:
    if not columns:
        ws["A1"] = "No tabular rows were found on these pages."
        ws["A1"].font = Font(name="Calibri", size=11, italic=True, color=INK_SOFT)
        ws.column_dimensions["A"].width = 60
        return

    headers = list(columns) + [SOURCE_HEADER]
    for col, name in enumerate(headers, start=1):
        header = ws.cell(row=1, column=col, value=name)
        header.font = HEADER_FONT
        header.fill = HEADER_FILL
        header.alignment = Alignment(horizontal="left", vertical="center")
        header.border = BORDER
    ws.row_dimensions[1].height = 18

    widths = {name: len(name) for name in headers}

    for row_index, row in enumerate(rows, start=2):
        result = row["result"]
        number_columns = _numeric_columns(result)
        date_column = _date_column(result)
        balance_column = _role_column(result, "balance")
        mark = row.get("mark")
        source = str(row.get("source") or "")

        for col, column in enumerate(columns, start=1):
            raw = row["record"].get(column, "")
            value, number_format = _typed(
                raw, column == date_column, column in number_columns, result.date_order
            )
            cell = ws.cell(row=row_index, column=col, value=value)
            if number_format:
                cell.number_format = number_format
                cell.alignment = Alignment(horizontal="right", vertical="top")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="top")
            if mark:
                fill, colour = BALANCE_STYLE[mark]
                cell.fill = PatternFill("solid", fgColor=fill)
                cell.font = Font(name="Calibri", size=10, bold=True, color=colour)
                if column == balance_column and value is not None:
                    cell.comment = Comment(
                        f"{mark.capitalize()} balance exactly as printed"
                        + (f" ({source})." if source else "."),
                        "Firm PDF Workspace",
                    )
            else:
                cell.font = Font(
                    name="Calibri", size=10, bold=bool(row.get("is_total")), color=INK_SOFT
                )
            cell.border = BORDER
            widths[column] = max(widths[column], len(str(raw)))

        source_cell = ws.cell(row=row_index, column=len(columns) + 1, value=source)
        source_cell.alignment = Alignment(horizontal="left", vertical="top")
        source_cell.border = BORDER
        if mark:
            fill, colour = BALANCE_STYLE[mark]
            source_cell.fill = PatternFill("solid", fgColor=fill)
            source_cell.font = Font(name="Calibri", size=9, bold=True, color=colour)
        else:
            source_cell.font = Font(name="Calibri", size=9, color=INK_SOFT)
        widths[SOURCE_HEADER] = max(widths[SOURCE_HEADER], len(source))

    ws.freeze_panes = "A2"
    if rows:
        last_column = get_column_letter(len(headers))
        ws.auto_filter.ref = f"A1:{last_column}{len(rows) + 1}"

    for col, name in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(col)].width = float(min(max(10, widths[name] + 2), 52))


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
def _balance_marks(result: ExtractResult) -> dict[int, str]:
    """Which record is the opening balance and which is the closing balance.

    A statement usually prints both lines, and they are found by their wording.
    When it does not, the first and last row carrying a balance are used, so the
    highlight still lands on something an accountant recognises.
    """
    if not result.records:
        return {}
    opening: int | None = None
    closing: int | None = None
    for index, record in enumerate(result.records):
        text = " ".join(str(value) for value in record.values())
        if opening is None and OPENING_PATTERN.search(text):
            opening = index
        if CLOSING_PATTERN.search(text):
            closing = index

    balance_column = _role_column(result, "balance")
    readable = []
    if balance_column:
        readable = [
            index
            for index, record in enumerate(result.records)
            if parse_number(record.get(balance_column)) is not None
        ]
    if opening is None:
        opening = readable[0] if readable else 0
    if closing is None:
        closing = readable[-1] if readable else len(result.records) - 1

    marks: dict[int, str] = {}
    if opening != closing:
        marks[opening] = "opening"
    marks[closing] = "closing"
    return marks


def _source_label(result: ExtractResult, index: int, prefix: str = "") -> str:
    pages = result.record_pages
    page = pages[index] if index < len(pages) else None
    label = f"Page {page}" if page else ""
    if prefix and label:
        return f"{prefix} · {label}"
    return prefix or label


def _role_column(result: ExtractResult, role: str) -> str | None:
    index = result.roles.get(role)
    if index is None or index >= len(result.columns):
        return None
    return result.columns[index]


def _numeric_columns(result: ExtractResult) -> set[str]:
    return {
        column
        for role in ("amount", "debit", "credit", "balance")
        for column in [_role_column(result, role)]
        if column
    }


def _date_column(result: ExtractResult) -> str | None:
    return _role_column(result, "date")


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
