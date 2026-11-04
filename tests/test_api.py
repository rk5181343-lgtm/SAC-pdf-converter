from __future__ import annotations

import io


def _upload(client, pdf: bytes, name: str = "statement.pdf"):
    return client.post(
        "/api/uploads",
        data={"files": (io.BytesIO(pdf), name)},
        content_type="multipart/form-data",
    )


def _run_until_done(client, job_id: str, attempts: int = 30):
    job = None
    for _ in range(attempts):
        payload = client.post(f"/api/jobs/{job_id}/advance").get_json()["job"]
        job = payload
        if payload["status"] in {"done", "failed"}:
            return payload
    assert job is not None
    return job


def test_health_and_tools(client):
    health = client.get("/api/health").get_json()
    assert health["status"] == "ok"
    assert health["jobs"] == "poll"

    tools = client.get("/api/tools").get_json()
    assert "Convert" in tools["groups"]
    ids = {tool["id"] for tool in tools["tools"]}
    assert {"pdf-to-excel", "merge-pdf", "protect-pdf", "watermark"} <= ids


def test_upload_requires_files(client):
    response = client.post("/api/uploads", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_upload_rejects_unknown_extension(client):
    response = client.post(
        "/api/uploads",
        data={"files": (io.BytesIO(b"hello"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_excel_job_end_to_end(client, statement_pdf):
    upload = _upload(client, statement_pdf)
    assert upload.status_code == 201
    batch = upload.get_json()["batch"]
    assert len(batch["documents"]) == 1
    assert batch["documents"][0]["meta"]["pages"] == 1

    started = client.post(
        f"/api/batches/{batch['id']}/run", json={"tool": "pdf-to-excel", "options": {}}
    )
    assert started.status_code == 202
    job = started.get_json()["job"]
    assert job["mode"] == "poll"

    finished = _run_until_done(client, job["id"])
    assert finished["status"] == "done", finished
    assert finished["progress"] == 100

    detail = client.get(f"/api/batches/{batch['id']}").get_json()
    outputs = [doc for doc in detail["batch"]["documents"] if doc["kind"] == "output"]
    assert outputs and outputs[0]["filename"].endswith(".xlsx")

    download = client.get(outputs[0]["download_url"])
    assert download.status_code == 200
    assert download.data[:2] == b"PK"  # xlsx is a zip


def test_merge_tool_one_shot(client, statement_pdf):
    response = client.post(
        "/api/tools/merge-pdf/run",
        data={
            "files": [(io.BytesIO(statement_pdf), "a.pdf"), (io.BytesIO(statement_pdf), "b.pdf")],
            "options": "{}",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job = response.get_json()["job"]
    finished = _run_until_done(client, job["id"])
    assert finished["status"] == "done"

    batch_id = finished["batch_id"]
    detail = client.get(f"/api/batches/{batch_id}").get_json()
    outputs = [doc for doc in detail["batch"]["documents"] if doc["kind"] == "output"]
    assert outputs[0]["filename"] == "merged.pdf"


def test_unknown_tool_is_404(client):
    assert client.post("/api/tools/moon-landing/run", data={}).status_code == 404


def test_library_listing_and_delete(client, statement_pdf):
    upload = _upload(client, statement_pdf).get_json()["batch"]
    listing = client.get("/api/library").get_json()
    assert any(batch["id"] == upload["id"] for batch in listing["batches"])
    assert listing["stats"]["documents"] >= 1

    assert client.delete(f"/api/batches/{upload['id']}").status_code == 200
    assert client.get(f"/api/batches/{upload['id']}").status_code == 404


def test_upload_limit_follows_the_host(monkeypatch):
    """A serverless host drops an oversized body before the app runs, which the
    browser reports as a dead connection, so the app must not advertise a limit
    the platform will refuse."""
    from app import Settings

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    assert Settings().max_upload_mb == 4

    monkeypatch.setenv("MAX_UPLOAD_MB", "25")
    assert Settings().max_upload_mb == 25

    monkeypatch.delenv("VERCEL")
    monkeypatch.delenv("MAX_UPLOAD_MB")
    assert Settings().max_upload_mb == 40
