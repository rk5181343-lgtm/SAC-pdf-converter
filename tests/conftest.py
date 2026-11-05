"""Shared fixtures: a temporary workspace and a small bank statement PDF."""
from __future__ import annotations

import io
import os
import tempfile

# The app reads settings at import time, so configure the workspace first.
_TMP = tempfile.mkdtemp(prefix="firm-pdf-workspace-")
os.environ.setdefault("STORAGE_MODE", "fs")
os.environ.setdefault("BACKGROUND_THREADS", "0")
os.environ.setdefault("SQLITE_PATH", os.path.join(_TMP, "workspace.db"))
os.environ.setdefault("STORAGE_DIR", os.path.join(_TMP, "files"))
os.environ.setdefault("MAX_PAGES", "50")
os.environ.setdefault("MAX_UPLOAD_MB", "20")

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROWS = [
    ("Date", "Description", "Amount", "Balance"),
    ("01/02/2026", "Brought forward", "", "1,000.00"),
    ("03/02/2026", "Invoice 1042", "250.00", "1,250.00"),
    ("10/02/2026", "Invoice 1043", "-100.00", "1,150.00"),
    ("28/02/2026", "Bank charges", "-15.50", "1,134.50"),
]


def _draw_page(page, rows: list[tuple[str, ...]], height: float) -> None:
    page.setFont("Helvetica-Bold", 14)
    page.drawString(60, height - 50, "Northwind Supplies Ltd")
    page.setFont("Helvetica", 10)
    page.drawString(60, height - 68, "Bank statement - account 12345678")
    page.drawString(60, height - 84, "Statement period 01/02/2026 to 28/02/2026")

    widths = [70, 220, 80, 90]
    x0 = 60
    row_height = 24
    top = height - 120

    page.setFont("Helvetica", 9)
    for index, row in enumerate(rows):
        x = x0
        baseline = top - index * row_height - row_height + 8
        for column, cell in enumerate(row):
            page.drawString(x + 4, baseline, cell)
            x += widths[column]

    total_width = sum(widths)
    table_bottom = top - len(rows) * row_height
    for index in range(len(rows) + 1):
        y = top - index * row_height
        page.line(x0, y, x0 + total_width, y)
    x = x0
    for w in widths:
        page.line(x, top, x, table_bottom)
        x += w
    page.line(x0 + total_width, top, x0 + total_width, table_bottom)

    page.showPage()


def build_statement_pdf(pages: int = 1) -> bytes:
    """A statement with a ruled table; later pages repeat the same columns."""
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    for number in range(pages):
        if number == 0:
            rows = list(ROWS)
        else:
            rows = [ROWS[0]] + [
                (row[0], f"{row[1]} p{number + 1}", row[2], row[3]) for row in ROWS[1:]
            ]
        _draw_page(doc, rows, height)
    doc.save()
    return buffer.getvalue()


# Statements that spell out their balances, and ones that never do.
BALANCED_ROWS = [
    ("Date", "Description", "Amount", "Balance"),
    ("01/02/2026", "Opening balance", "", "1,000.00"),
    ("03/02/2026", "Invoice 1042", "250.00", "1,250.00"),
    ("10/02/2026", "Invoice 1043", "-100.00", "1,150.00"),
    ("28/02/2026", "Closing balance", "", "1,150.00"),
]
UNLABELLED_ROWS = [
    ("Date", "Description", "Amount", "Balance"),
    ("01/02/2026", "Salary credit", "1,000.00", "1,000.00"),
    ("03/02/2026", "Invoice 1042", "250.00", "1,250.00"),
    ("10/02/2026", "Invoice 1043", "-100.00", "1,150.00"),
    ("28/02/2026", "Bank charges", "-15.50", "1,134.50"),
]


def build_rows_pdf(rows: list[tuple[str, ...]]) -> bytes:
    """One page holding exactly these rows."""
    buffer = io.BytesIO()
    doc = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    _draw_page(doc, list(rows), height)
    doc.save()
    return buffer.getvalue()


@pytest.fixture(scope="session")
def statement_pdf() -> bytes:
    return build_statement_pdf()


@pytest.fixture(scope="session")
def balanced_statement_pdf() -> bytes:
    return build_rows_pdf(BALANCED_ROWS)


@pytest.fixture(scope="session")
def unlabelled_statement_pdf() -> bytes:
    return build_rows_pdf(UNLABELLED_ROWS)


@pytest.fixture(scope="session")
def two_page_statement_pdf() -> bytes:
    return build_statement_pdf(pages=2)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import settings
    from app.db import init_db, reset_db

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "files")
    monkeypatch.setattr(settings, "background_threads", False)
    monkeypatch.setattr(settings, "max_pages", 50)
    reset_db("", tmp_path / "workspace.db")
    init_db()

    from app.server import create_app

    application = create_app()
    application.config.update(TESTING=True)
    with application.test_client() as test_client:
        yield test_client
