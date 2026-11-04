from __future__ import annotations

import io
import zipfile


def _pages(count: int = 3) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=A4)
    for number in range(count):
        sheet.drawString(72, 720, f"page {number + 1}")
        sheet.showPage()
    sheet.save()
    return buffer.getvalue()


def _rotations(pdf: bytes) -> list[int]:
    from pypdf import PdfReader

    return [int(page.rotation or 0) for page in PdfReader(io.BytesIO(pdf)).pages]


def test_rotate_pdf_turns_only_the_named_pages():
    from app.excel_runner import _rotate_pdf

    out = _rotate_pdf(_pages(3), {"2": 90, "3": -90, "9": 90, "x": "sideways"})
    assert _rotations(out) == [0, 90, 270]


def test_rotate_pdf_leaves_a_file_without_turns_untouched():
    from app.excel_runner import _rotate_pdf

    original = _pages(2)
    assert _rotate_pdf(original, {}) == original
    assert _rotate_pdf(original, {"1": 0, "2": 360}) == original


def test_invalid_rotations_are_dropped():
    from app.server import _rotate_map

    cleaned = _rotate_map({"doc": {"1": 45, "2": "90", "3": 270}, "bad": "nope", "empty": {"5": 0}})
    assert cleaned == {"doc": {"2": 90, "3": 270}}


def test_rotations_survive_the_excel_option_flattening():
    from app.server import _excel_options

    options = _excel_options({"rotate_by_doc": {"doc": {"2": 90}}})
    assert options["rotate_by_doc"] == {"doc": {"2": 90}}


def _run(client, batch_id: str, tool: str, options: dict) -> dict:
    started = client.post(f"/api/batches/{batch_id}/run", json={"tool": tool, "options": options})
    assert started.status_code == 202, started.get_json()
    job = started.get_json()["job"]
    for _ in range(40):
        job = client.post(f"/api/jobs/{job['id']}/advance").get_json()["job"]
        if job["status"] in {"done", "failed"}:
            return job
    raise AssertionError(f"job did not finish: {job}")


def test_a_rotated_page_reaches_the_output(client):
    from PIL import Image

    batch = client.post(
        "/api/uploads",
        data={"files": (io.BytesIO(_pages(2)), "scan.pdf")},
        content_type="multipart/form-data",
    ).get_json()["batch"]
    document = batch["documents"][0]["id"]

    job = _run(
        client,
        batch["id"],
        "pdf-to-images",
        {
            "format": "png",
            "dpi": "96",
            "pages_by_doc": {document: "1"},
            "rotate_by_doc": {document: {"1": 90}},
        },
    )
    assert job["status"] == "done", job

    body = client.get(f"/api/documents/{job['outputs'][0]['document_id']}/download").data
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            body = archive.read(archive.namelist()[0])
    width, height = Image.open(io.BytesIO(body)).size
    assert width > height, f"page was not turned: {width}x{height}"
