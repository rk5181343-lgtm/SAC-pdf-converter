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
    assert len(tools["tools"]) >= 20
    assert tools["groups"]


def test_upload_rejects_unknown_suffix(client, statement_pdf):
    response = client.post(
        "/api/uploads",
        data={"files": (io.BytesIO(b"hello"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "PDF" in response.get_json()["error"]


def test_upload_listing_and_pages(client, statement_pdf):
    batch = _upload(client, statement_pdf).get_json()["batch"]
    assert batch["documents"][0]["meta"]["pages"] >= 1
    pages = client.get(f"/api/documents/{batch['documents'][0]['id']}/pages").get_json()
    assert pages["pages"] == batch["documents"][0]["meta"]["pages"]


def test_run_rejects_unknown_tool(client, statement_pdf):
    batch = _upload(client, statement_pdf).get_json()["batch"]
    response = client.post(f"/api/batches/{batch['id']}/run", json={"tool": "nope"})
    assert response.status_code == 404


def test_single_file_tool_refuses_many(client, statement_pdf):
    batch = _upload(client, statement_pdf).get_json()["batch"]
    second = _upload(client, statement_pdf, "other.pdf").get_json()["batch"]
    ids = [batch["id"], second["id"]]
    assert len(ids) == 2
    merged = client.post(
        "/api/uploads",
        data={
            "files": [
                (io.BytesIO(statement_pdf), "a.pdf"),
                (io.BytesIO(statement_pdf), "b.pdf"),
            ]
        },
        content_type="multipart/form-data",
    ).get_json()["batch"]
    response = client.post(f"/api/batches/{merged['id']}/run", json={"tool": "pdf-to-word"})
    assert response.status_code == 400
    assert "one file" in response.get_json()["error"]


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
