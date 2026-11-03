from __future__ import annotations

from datetime import date

import pytest

from app.converter.extract import classify_columns, parse_date, parse_number


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.50", 1234.50),
        ("(500.00)", -500.00),
        ("1.234,56", 1234.56),
        ("-12.5", -12.5),
        ("12.5-", -12.5),
        ("$3,000", 3000.0),
        ("1.500", 1500.0),
        ("0.00", 0.0),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "n/a", "—", "abc"])
def test_parse_number_rejects_noise(raw):
    assert parse_number(raw) is None


def test_parse_date_orders():
    assert parse_date("01/02/2026", "dmy") == date(2026, 2, 1)
    assert parse_date("01/02/2026", "mdy") == date(2026, 1, 2)
    assert parse_date("2026-02-01") == date(2026, 2, 1)
    assert parse_date("1 Feb 2026") == date(2026, 2, 1)
    assert parse_date("Feb 1, 2026") == date(2026, 2, 1)
    assert parse_date("20260201") == date(2026, 2, 1)
    assert parse_date("not a date") is None


def test_classify_columns():
    roles = classify_columns(["Date", "Description", "Debit", "Credit", "Balance"])
    assert roles["date"] == 0
    assert roles["description"] == 1
    assert roles["debit"] == 2
    assert roles["credit"] == 3
    assert roles["balance"] == 4
    assert roles["amount"] is None


def test_extract_finds_statement_records(statement_pdf):
    from app.converter.extract import extract
    from app.converter.pdf_source import PdfSource

    with PdfSource(statement_pdf, ocr_mode="never") as source:
        result = extract(source, "statement.pdf")

    assert result.doc_type == "bank_statement"
    assert result.columns[:2] == ["Date", "Description"]
    assert result.row_count >= 4
    assert result.roles["date"] == 0
    assert result.roles["balance"] is not None
    amounts = result.numeric_values("balance")
    assert amounts[0] == pytest.approx(1000.00)
    assert amounts[-1] == pytest.approx(1134.50)
