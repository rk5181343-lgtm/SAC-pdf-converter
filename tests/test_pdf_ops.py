from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfReader

from app.tools import pdf_ops


def _files(pdf: bytes, name: str = "statement.pdf"):
    return [{"filename": name, "data": pdf, "mimetype": "application/pdf"}]


def test_merge_pdf(statement_pdf):
    outputs = pdf_ops.merge_pdf(_files(statement_pdf) * 2, {})
    assert len(outputs) == 1
    assert len(PdfReader(io.BytesIO(outputs[0]["data"])).pages) == 2


def test_split_pdf_ranges(statement_pdf):
    outputs = pdf_ops.split_pdf(_files(statement_pdf), {"mode": "ranges", "pages": "1"})
    assert len(outputs) == 1
    assert len(PdfReader(io.BytesIO(outputs[0]["data"])).pages) == 1


def test_split_pdf_each_page_returns_zip(statement_pdf):
    outputs = pdf_ops.split_pdf(_files(statement_pdf), {"mode": "each"})
    assert outputs[0]["filename"].endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(outputs[0]["data"])) as archive:
        assert len(archive.namelist()) == 1


def test_rotate_pdf(statement_pdf):
    outputs = pdf_ops.rotate_pages(_files(statement_pdf), {"angle": "90", "pages": ""})
    page = PdfReader(io.BytesIO(outputs[0]["data"])).pages[0]
    assert page.rotation == 90


def test_watermark_keeps_page_count(statement_pdf):
    outputs = pdf_ops.watermark(_files(statement_pdf), {"text": "DRAFT", "opacity": 10})
    assert len(PdfReader(io.BytesIO(outputs[0]["data"])).pages) == 1


def test_crop_margins_shrinks_cropbox(statement_pdf):
    original = PdfReader(io.BytesIO(statement_pdf)).pages[0]
    outputs = pdf_ops.crop_margins(
        _files(statement_pdf), {"top": 10, "bottom": 10, "left": 5, "right": 5}
    )
    cropped = PdfReader(io.BytesIO(outputs[0]["data"])).pages[0]
    assert float(cropped.cropbox.width) < float(original.cropbox.width)
    assert float(cropped.cropbox.height) < float(original.cropbox.height)


def test_protect_and_unlock_roundtrip(statement_pdf):
    protected = pdf_ops.protect_pdf(
        _files(statement_pdf), {"password": "firm-secret", "allow_printing": True}
    )
    assert PdfReader(io.BytesIO(protected[0]["data"])).is_encrypted

    unlocked = pdf_ops.unlock_pdf(_files(protected[0]["data"]), {"password": "firm-secret"})
    assert not PdfReader(io.BytesIO(unlocked[0]["data"])).is_encrypted


def test_unlock_rejects_wrong_password(statement_pdf):
    protected = pdf_ops.protect_pdf(_files(statement_pdf), {"password": "firm-secret"})
    with pytest.raises(ValueError):
        pdf_ops.unlock_pdf(_files(protected[0]["data"]), {"password": "wrong"})


def test_pdf_to_images_and_back(statement_pdf):
    images = pdf_ops.pdf_to_images(_files(statement_pdf), {"dpi": "96", "format": "png"})
    assert images[0]["filename"].endswith(".png")
    rebuilt = pdf_ops.images_to_pdf(
        [{"filename": "page.png", "data": images[0]["data"], "mimetype": "image/png"}],
        {"size": "A4"},
    )
    assert len(PdfReader(io.BytesIO(rebuilt[0]["data"])).pages) == 1


def test_compress_and_nup(statement_pdf):
    compressed = pdf_ops.compress_pdf(_files(statement_pdf), {"dpi": "96", "quality": 60})
    assert compressed[0]["filename"].endswith("-compressed.pdf")
    nup = pdf_ops.pages_per_sheet(_files(statement_pdf), {"per_sheet": "2", "size": "A4"})
    assert len(PdfReader(io.BytesIO(nup[0]["data"])).pages) == 1


def test_reorder_and_header_footer(statement_pdf):
    reordered = pdf_ops.reorder_pages(_files(statement_pdf), {"order": "1,0,1"})
    assert len(PdfReader(io.BytesIO(reordered[0]["data"])).pages) == 3
    stamped = pdf_ops.header_footer(
        _files(statement_pdf), {"header": "Northwind (file)", "footer": "", "page_numbers": True}
    )
    assert len(PdfReader(io.BytesIO(stamped[0]["data"])).pages) == 1
