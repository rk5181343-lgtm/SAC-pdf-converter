"""Layouts taken from real statements that used to break the reader.

The three shapes below are the ones that failed on live Axis, SBI and ICICI
statements: a header printed outside the ruled box, a header that appears only
on page one, a Debit/Credit direction column, and a date printed with a time.
"""
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import load_workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.converter.extract import parse_date
from app.converter.pipeline import convert_pdf

WIDTH, HEIGHT = A4


def _table(page, xs, header, rows, top, row_height=16, header_line=True, rule=True):
    page.setFont("Helvetica-Bold", 6.0)
    if header:
        for x, name in zip(xs, header):
            page.drawString(x, top, name)
    if rule:
        page.setStrokeColorRGB(0.6, 0.6, 0.6)
        page.line(20, top + 8, 575, top + 8)
    page.setFont("Helvetica", 7)
    y = top - 4
    for row in rows:
        y -= row_height
        for x, cell in zip(xs, row):
            if cell:
                page.drawString(x, y + 4, str(cell))
        if rule:
            page.line(20, y - 2, 575, y - 2)
    return y


def _sbi_like(pages: int = 2) -> bytes:
    """Header above the box on page one, and no header at all after that."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    header = ["Txn Date", "Value Date", "Description", "Ref No./Cheque No.", "Branch Code", "Debit", "Credit", "Balance"]
    xs = [20, 70, 120, 290, 356, 410, 452, 490]
    for number in range(pages):
        if number == 0:
            page.setFont("Helvetica-Bold", 9)
            page.drawString(28, HEIGHT - 32, "Account Name : THE WATERBASE LIMITED")
            page.setFont("Helvetica", 7)
            page.drawString(28, HEIGHT - 46, "Opening Balance as on 1 Sep 2026 : -7,91,792.71")
            top = HEIGHT - 90
        else:
            top = HEIGHT - 50
        rows = []
        for index in range(4):
            row = number * 4 + index
            rows.append([
                "01/09/2026", "01/09/2026", f"BY TRANSFER- INB Payment towards Invoice {row}",
                f"CT0AIXR{row}", "20553", f"{1000 + row * 10}.00" if row % 2 else "",
                f"{10000 + row * 20}.00" if row % 2 == 0 else "", f"-6,{row:02d},98,196.13",
            ])
        _table(page, xs, header if number == 0 else None, rows, top)
        page.showPage()
    page.save()
    return buffer.getvalue()


def _axis_like() -> bytes:
    """Amount plus a Debit/Credit direction column, header inside the box."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    header = ["S.NO", "Transaction Date", "Value Date", "Particulars", "Amount(INR)",
              "Debit/Credit", "Balance(INR)", "Branch Name(SOL)"]
    xs = [22, 44, 104, 150, 300, 366, 414, 486]
    rows = []
    for index in range(5):
        rows.append([
            str(index + 1), "05/09/2026", "05/09/2026", f"Offset for [D14642{index:03d}]",
            f"{500000 + index}.00", "DR" if index % 2 else "CR",
            f"-{index + 1},02,32,826.54", "CBB KOLKATA (1164)",
        ])
    rows.append(["6", "", "", "TRANSACTION TOTAL DR/CR", "24,80,42,813.50", "DR", "24,71,00,000.00", ""])
    bottom = _table(page, xs, header, rows, HEIGHT - 120)
    page.setFont("Helvetica", 6)
    page.drawString(22, bottom - 18, "Balance: the constituent of any discrepancy OFFICE, TRISHUL, Opp. Samartheswar, 380006.")
    page.drawString(22, bottom - 30, "This is a computer generated statement and does not require a signature.")
    page.save()
    return buffer.getvalue()


def _icici_like() -> bytes:
    """Posted date printed with its time in the same cell."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    header = ["No.", "Transaction ID", "Value Date", "Txn Posted Date", "ChequeNo.",
              "Description", "Cr/Dr", "Transaction Amount(INR)", "Available Balance(INR)"]
    xs = [20, 40, 118, 158, 240, 280, 386, 420, 500]
    rows = [
        [str(index + 1), f"S6020{index}607", "01/09/2026", "01/09/2026 07:55:29 AM", "",
         f"UPI/182915408688/Payment {index}", "CR" if index % 2 else "DR",
         f"{200 + index}.00", f"{3508198 + index * 100}.49"]
        for index in range(4)
    ]
    _table(page, xs, header, rows, HEIGHT - 80, row_height=14)
    page.save()
    return buffer.getvalue()


def test_every_page_of_a_headerless_continuation_is_kept():
    output = convert_pdf("sbi.pdf", _sbi_like(2), {"images": False})
    sheet = load_workbook(io.BytesIO(output["data"]))["Clean Data"]
    headers = [cell.value for cell in sheet[1]]
    sources = [sheet.cell(row=row, column=len(headers)).value for row in range(2, sheet.max_row + 1)]
    assert sheet.max_row - 1 == 8, "no row is consumed as a header on page two"
    assert "Page 2" in sources, "page two is not dropped for having no header"
    assert headers[0] == "Txn Date"
    assert headers[1:-1] == [
        "Value Date",
        "Description",
        "Ref No./Cheque No.",
        "Branch Code",
        "Debit",
        "Credit",
        "Balance",
    ], "Description, Ref, Branch, Debit and Credit are separate columns"


def test_amount_and_direction_columns_are_understood():
    output = convert_pdf("axis.pdf", _axis_like(), {"images": False})
    workbook = load_workbook(io.BytesIO(output["data"]))
    sheet = workbook["Clean Data"]
    headers = [cell.value for cell in sheet[1]]
    assert "Debit/Credit" in headers
    amount = next(index for index, name in enumerate(headers) if "Amount" in str(name)) + 1
    balance = next(index for index, name in enumerate(headers) if "Balance" in str(name)) + 1
    first = sheet.cell(row=2, column=amount).value
    assert float(first) == 500000, f"the amount is written as a number, not text: {first!r}"
    assert isinstance(sheet.cell(row=2, column=balance).value, (int, float))
    assert str(sheet.cell(row=2, column=1).value) == "1", "the first data row is not consumed as the header"

    # The TOTAL row is its own row, with its amounts in the amount and balance
    # columns, and the footer under it is not glued onto the last transaction.
    total_row = sheet.max_row
    description = next(index for index, name in enumerate(headers) if "Particular" in str(name)) + 1
    assert str(sheet.cell(row=total_row, column=description).value).startswith("TRANSACTION TOTAL")
    assert float(sheet.cell(row=total_row, column=amount).value) == 248042813.50
    assert float(sheet.cell(row=total_row, column=balance).value) == 247100000.00
    last_transaction = sheet.cell(row=total_row - 1, column=description).value
    assert "discrepancy" not in last_transaction and "signature" not in last_transaction
    body = " ".join(
        str(cell.value or "")
        for row in sheet.iter_rows(min_row=2)
        for cell in row
    )
    assert "does not require a signature" not in body


def test_a_date_printed_with_a_time_is_still_a_date():
    assert parse_date("01/09/2026 07:55:29 AM") is not None
    output = convert_pdf("icici.pdf", _icici_like(), {"images": False})
    sheet = load_workbook(io.BytesIO(output["data"]))["Clean Data"]
    headers = [cell.value for cell in sheet[1]]
    dates = [
        cell.value
        for row in sheet.iter_rows(min_row=2, max_row=5)
        for cell in row
        if isinstance(cell.value, datetime)
    ]
    assert dates and dates[0].date().isoformat() == "2026-09-01"
    balance = next(index for index, name in enumerate(headers) if "Balance" in str(name)) + 1
    assert sheet.cell(row=2, column=balance).value == 3508198.49
    assert "Cr/Dr" in headers and "Description" in headers
    assert headers.index("Cr/Dr") > headers.index("Description"), "Cr/Dr is not merged into Description"
