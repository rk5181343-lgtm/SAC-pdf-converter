from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from app.converter.pipeline import convert_batch, convert_pdf


def _workbook(data: bytes):
    return load_workbook(io.BytesIO(data))


def test_convert_pdf_writes_three_sheets(statement_pdf):
    output = convert_pdf("statement.pdf", statement_pdf)
    assert output["filename"] == "statement.xlsx"
    assert output["mimetype"].endswith("spreadsheetml.sheet")
    assert output["meta"]["doc_type"] == "bank_statement"
    assert output["meta"]["rows"] >= 4

    workbook = _workbook(output["data"])
    assert workbook.sheetnames == ["Original Layout", "Clean Data", "Validation"]

    data_sheet = workbook["Clean Data"]
    headers = [cell.value for cell in data_sheet[1]]
    assert headers[0] == "Date"
    assert "Description" in headers

    balance_column = headers.index("Balance") + 1
    balances = [
        data_sheet.cell(row=row, column=balance_column).value
        for row in range(2, data_sheet.max_row + 1)
    ]
    assert 1000.0 in [round(value, 2) for value in balances if isinstance(value, (int, float))]

    validation_names = [workbook["Validation"].cell(row=row, column=1).value for row in range(2, 12)]
    assert "Rows extracted" in validation_names
    assert "Page coverage" in validation_names

    layout = workbook["Original Layout"]
    assert layout.max_row >= 1
    assert layout["A1"].value is not None


def test_convert_pdf_without_layout_sheet(statement_pdf):
    output = convert_pdf(
        "statement.pdf",
        statement_pdf,
        {"sheets": {"layout": False, "data": True, "validation": True}, "images": False},
    )
    assert _workbook(output["data"]).sheetnames == ["Clean Data", "Validation"]


def test_convert_batch_adds_combined_workbook(statement_pdf):
    files = [
        {"filename": "jan.pdf", "data": statement_pdf},
        {"filename": "feb.pdf", "data": statement_pdf},
    ]
    result = convert_batch(files, {"combine": True, "images": False})
    names = [output["filename"] for output in result["outputs"]]
    assert names == ["jan.xlsx", "feb.xlsx", "Combined workbook.xlsx"]
    assert result["errors"] == []

    combined = _workbook(result["outputs"][2]["data"])
    assert "Summary" in combined.sheetnames
    assert "Validation" in combined.sheetnames
    assert any(name.startswith("Layout 1 ") for name in combined.sheetnames)
    assert combined.sheetnames.count("Clean Data") == 1


def test_convert_batch_reports_bad_file(statement_pdf):
    files = [
        {"filename": "good.pdf", "data": statement_pdf},
        {"filename": "broken.pdf", "data": b"not a pdf"},
    ]
    result = convert_batch(files, {"images": False})
    assert [output["filename"] for output in result["outputs"]] == ["good.xlsx"]
    assert result["errors"][0]["filename"] == "broken.pdf"


def test_convert_batch_with_nothing_readable_raises():
    with pytest.raises(ValueError):
        convert_batch([{"filename": "broken.pdf", "data": b"not a pdf"}], {})


def test_a_multi_page_pdf_keeps_one_layout_sheet(two_page_statement_pdf):
    output = convert_pdf("statement.pdf", two_page_statement_pdf, {"images": False})
    workbook = _workbook(output["data"])
    assert workbook.sheetnames == ["Original Layout", "Clean Data", "Validation"]
    layout = workbook["Original Layout"]
    values = [layout.cell(row=row, column=1).value for row in range(1, layout.max_row + 1)]
    assert "Page 2" in values, "the second page starts below a marker on the same sheet"


def test_clean_data_ends_with_the_source_page(two_page_statement_pdf):
    output = convert_pdf("statement.pdf", two_page_statement_pdf, {"images": False})
    sheet = _workbook(output["data"])["Clean Data"]
    headers = [cell.value for cell in sheet[1]]
    assert headers[-1] == "Source"
    sources = [sheet.cell(row=row, column=len(headers)).value for row in range(2, sheet.max_row + 1)]
    assert sources[0] == "Page 1"
    assert "Page 2" in sources
    assert sheet.max_row - 1 == 8, "both pages' rows are in the one sheet"


def test_opening_and_closing_balances_are_highlighted(statement_pdf):
    output = convert_pdf("statement.pdf", statement_pdf, {"images": False})
    sheet = _workbook(output["data"])["Clean Data"]
    headers = [cell.value for cell in sheet[1]]
    balance = headers.index("Balance") + 1
    fills = [
        str(sheet.cell(row=row, column=balance).fill.fgColor.rgb or "")
        for row in range(2, sheet.max_row + 1)
    ]
    assert any(fill.endswith("FFF1D6") for fill in fills), "opening balance row is tinted"
    assert any(fill.endswith("DDF0E8") for fill in fills), "closing balance row is tinted"


def test_validation_no_longer_reports_the_balance(statement_pdf):
    output = convert_pdf("statement.pdf", statement_pdf, {"images": False})
    names = [cell.value for cell in _workbook(output["data"])["Validation"]["A"]]
    assert "Opening / closing balance" not in names
    assert "Rows extracted" in names
