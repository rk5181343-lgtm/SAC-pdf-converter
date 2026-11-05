"""Per-file page selection and the batch ZIP download.

The comparison target (Online2PDF) lets each selected file pick its own pages and
hands back one download for everything, so both behaviours are covered here.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def _multipage_pdf(pages: int = 3) -> bytes:
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    for number in range(1, pages + 1):
        page.setFont("Helvetica", 12)
        page.drawString(60, 780, f"Page {number} of the sample")
        page.showPage()
    page.save()
    return buffer.getvalue()


@pytest.fixture()
def three_page_pdf() -> bytes:
    return _multipage_pdf(3)


def _upload(client, pdf: bytes, name: str) -> dict:
    response = client.post(
        "/api/uploads",
        data={"files": (io.BytesIO(pdf), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    return response.get_json()["batch"]


def _finish(client, job_id: str) -> dict:
    job = None
    for _ in range(30):
        job = client.post(f"/api/jobs/{job_id}/advance").get_json()["job"]
        if job["status"] in {"done", "failed"}:
            return job
    assert job is not None
    return job


def _outputs(client, batch_id: str) -> list[dict]:
    documents = client.get(f"/api/batches/{batch_id}").get_json()["batch"]["documents"]
    return [doc for doc in documents if doc["kind"] == "output"]


def test_pages_by_doc_trims_before_running(client, three_page_pdf):
    batch = _upload(client, three_page_pdf, "three pages.pdf")
    doc_id = batch["documents"][0]["id"]

    started = client.post(
        f"/api/batches/{batch['id']}/run",
        json={
            "tool": "pdf-to-images",
            "options": {"format": "png", "dpi": "96", "pages_by_doc": {doc_id: "2-3"}},
        },
    )
    assert started.status_code == 202
    finished = _finish(client, started.get_json()["job"]["id"])
    assert finished["status"] == "done", finished

    outputs = _outputs(client, batch["id"])
    assert len(outputs) == 1
    archive = zipfile.ZipFile(io.BytesIO(client.get(outputs[0]["download_url"]).data))
    assert len(archive.namelist()) == 2  # pages 2 and 3 were converted


def test_pages_by_doc_also_trims_a_pdf_to_excel_run(client, two_page_statement_pdf):
    """The pages drill-down is not the privilege of the PDF toolbox tools."""
    from openpyxl import load_workbook

    batch = _upload(client, two_page_statement_pdf, "two pages.pdf")
    doc_id = batch["documents"][0]["id"]

    started = client.post(
        f"/api/batches/{batch['id']}/run",
        json={"tool": "pdf-to-excel", "options": {"pages_by_doc": {doc_id: "2"}}},
    )
    assert started.status_code == 202
    finished = _finish(client, started.get_json()["job"]["id"])
    assert finished["status"] == "done", finished

    # Balances are only highlighted from printed wording, so no fills are awaited
    # here: what matters is that page 1 was dropped before conversion.
    outputs = _outputs(client, batch["id"])
    workbook = load_workbook(io.BytesIO(client.get(outputs[0]["download_url"]).data))
    rows = list(workbook["Clean Data"].iter_rows(min_row=2, values_only=True))
    descriptions = [str(row[1]) for row in rows if row[1]]
    assert descriptions, rows
    assert all("p2" in text for text in descriptions), descriptions


def test_pages_by_doc_can_reorder_the_whole_statement(client, two_page_statement_pdf):
    """Holding a page and dropping it elsewhere changes the order of the rows."""
    from openpyxl import load_workbook

    batch = _upload(client, two_page_statement_pdf, "two pages.pdf")
    doc_id = batch["documents"][0]["id"]

    started = client.post(
        f"/api/batches/{batch['id']}/run",
        json={"tool": "pdf-to-excel", "options": {"pages_by_doc": {doc_id: "2,1"}}},
    )
    assert started.status_code == 202
    finished = _finish(client, started.get_json()["job"]["id"])
    assert finished["status"] == "done", finished

    outputs = _outputs(client, batch["id"])
    workbook = load_workbook(io.BytesIO(client.get(outputs[0]["download_url"]).data))
    rows = list(workbook["Clean Data"].iter_rows(min_row=2, values_only=True))
    descriptions = [str(row[1]) for row in rows if row[1]]
    assert any("p2" in text for text in descriptions), descriptions
    assert "p2" in descriptions[0], descriptions  # page 2 now comes first
    assert "p2" not in descriptions[-1], descriptions


def test_pages_by_doc_is_ignored_when_absent(client, three_page_pdf):
    batch = _upload(client, three_page_pdf, "all pages.pdf")
    started = client.post(
        f"/api/batches/{batch['id']}/run",
        json={"tool": "pdf-to-images", "options": {"format": "png", "dpi": "96"}},
    )
    finished = _finish(client, started.get_json()["job"]["id"])
    assert finished["status"] == "done", finished
    archive = zipfile.ZipFile(io.BytesIO(client.get(_outputs(client, batch["id"])[0]["download_url"]).data))
    assert len(archive.namelist()) == 3


def test_batch_zip_gathers_every_output(client, three_page_pdf):
    batch = _upload(client, three_page_pdf, "first.pdf")

    assert client.get(f"/api/batches/{batch['id']}/download.zip").status_code == 404

    for _ in range(2):  # two runs, two outputs with the same name
        started = client.post(
            f"/api/batches/{batch['id']}/run",
            json={"tool": "pdf-to-images", "options": {"format": "png", "dpi": "96"}},
        )
        assert started.status_code == 202
        assert _finish(client, started.get_json()["job"]["id"])["status"] == "done"

    outputs = _outputs(client, batch["id"])
    assert len(outputs) == 2
    response = client.get(f"/api/batches/{batch['id']}/download.zip")
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
    assert len(names) == len(outputs)
    assert len(set(names)) == len(names)  # duplicate names are kept apart
    assert all(name.endswith(".zip") for name in names)
