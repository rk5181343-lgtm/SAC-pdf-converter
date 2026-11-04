"""Conversion jobs.

Two execution modes share one job record:

* **thread mode** (default on a long-lived host): :func:`submit` starts a worker
  thread that walks the job's steps to completion.
* **poll mode** (default on serverless hosts, e.g. Vercel): the job is stored as
  ``queued`` and the browser POSTs ``/api/jobs/<id>/advance``; each call does as
  much work as fits in its budget and returns the new state, so no request has
  to hold a whole conversion.

A job is a list of *steps*. PDF → Excel runs one step per file (unless the
caller asks for the combined workbook, which needs the whole batch at once);
every other tool runs the whole input set as a single step, because tools such
as merge and split care about the order of all the files together.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from app import library, settings
from app.db import get_db
from app.tools import get_tool, run_tool

TERMINAL = {"done", "failed"}
ACTIVE = {"queued", "running"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------- submit
def submit(batch_id: str, tool_id: str, document_ids: list[str], options: dict | None = None) -> dict:
    get_tool(tool_id)  # raises for an unknown tool
    document_ids = [doc_id for doc_id in document_ids if doc_id]
    if not document_ids:
        raise ValueError("A run needs at least one document.")

    payload: dict[str, Any] = {
        "steps": _plan(tool_id, document_ids, options or {}),
        "cursor": 0,
        "options": options or {},
        "errors": [],
        "outputs": [],
    }
    job_id = uuid.uuid4().hex
    stamp = library.now_iso()
    get_db().execute(
        "INSERT INTO jobs (id, batch_id, tool, status, progress, message, error, payload, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, batch_id, tool_id, "queued", 0, "Queued", "", json.dumps(payload), stamp, stamp),
    )
    library.touch_batch(batch_id, "queued")

    if settings.background_threads:
        worker = threading.Thread(target=_run, args=(job_id,), name=f"job-{job_id[:8]}", daemon=True)
        worker.start()
    return public_job(job_id)


def _plan(tool_id: str, document_ids: list[str], options: dict) -> list[list[str]]:
    if tool_id == "pdf-to-excel" and not options.get("combine"):
        return [[doc_id] for doc_id in document_ids]
    return [list(document_ids)]


# ----------------------------------------------------------------------- reads
def get_job(job_id: str) -> dict | None:
    row = get_db().query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None
    payload = row.get("payload") or "{}"
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    row["payload"] = payload
    return row


def public_job(job_id: str) -> dict | None:
    row = get_job(job_id)
    return _public(row) if row else None


def list_jobs(batch_id: str) -> list[dict]:
    rows = get_db().query(
        "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at DESC", (batch_id,)
    )
    jobs = []
    for row in rows:
        payload = row.get("payload") or "{}"
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        row["payload"] = payload
        jobs.append(_public(row))
    return jobs


def _public(row: dict) -> dict:
    payload = row.get("payload") or {}
    steps = payload.get("steps") or []
    cursor = int(payload.get("cursor") or 0)
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "tool": row["tool"],
        "status": row["status"],
        "progress": int(row.get("progress") or 0),
        "message": row.get("message") or "",
        "error": row.get("error") or "",
        "mode": settings.mode_label,
        "steps_total": len(steps),
        "steps_done": min(cursor, len(steps)),
        "errors": payload.get("errors") or [],
        "outputs": payload.get("outputs") or [],
        "updated_at": row.get("updated_at"),
    }


# --------------------------------------------------------------------- running
def advance(job_id: str, budget_seconds: float = 20.0) -> dict | None:
    """Do as much of a poll-driven job as fits in the budget."""
    job = get_job(job_id)
    if not job:
        return None
    if job["status"] in TERMINAL:
        return _public(job)

    lock = _lock_for(job_id)
    if not lock.acquire(blocking=False):
        return _public(job)  # another request is already working on it
    try:
        started = time.monotonic()
        while True:
            job = get_job(job_id)
            if not job or job["status"] in TERMINAL:
                return _public(job) if job else None
            if not _has_work(job):
                _finish(job)
                return public_job(job_id)
            _run_step(job)
            if time.monotonic() - started >= budget_seconds:
                return public_job(job_id)
    finally:
        lock.release()


def _run(job_id: str) -> None:
    try:
        while True:
            job = get_job(job_id)
            if not job or job["status"] in TERMINAL:
                return
            if not _has_work(job):
                _finish(job)
                return
            _run_step(job)
    except Exception as exc:  # pragma: no cover - defensive
        _fail(job_id, f"{type(exc).__name__}: {exc}")


def _has_work(job: dict) -> bool:
    payload = job["payload"]
    return int(payload.get("cursor") or 0) < len(payload.get("steps") or [])


def _run_step(job: dict) -> None:
    payload = job["payload"]
    cursor = int(payload.get("cursor") or 0)
    steps = payload.get("steps") or []
    step = steps[cursor]
    tool_id = job["tool"]
    options = payload.get("options") or {}

    _update(job["id"], status="running", message=_step_message(tool_id, step, library.get_batch(job["batch_id"])), progress=_progress(cursor, len(steps)))

    try:
        outputs = _process(tool_id, step, options)
    except Exception as exc:
        payload.setdefault("errors", []).append({"step": cursor + 1, "message": _short_error(exc)})
        payload["cursor"] = cursor + 1
        _save(job["id"], payload, status="running", message="Step failed", error=_short_error(exc),
              progress=_progress(cursor + 1, len(steps)))
        return

    stored = []
    for output in outputs:
        document = library.add_document(
            job["batch_id"],
            library.KIND_OUTPUT,
            output["filename"],
            output["data"],
            output.get("mimetype") or "application/octet-stream",
            output.get("meta") or {},
        )
        stored.append({"document_id": document["id"], "filename": document["filename"]})
    payload.setdefault("outputs", []).extend(stored)
    payload["cursor"] = cursor + 1
    _save(
        job["id"],
        payload,
        status="running",
        message=f"Finished {len(stored)} file(s) — {cursor + 1} of {len(steps)}",
        progress=_progress(cursor + 1, len(steps)),
    )


def _process(tool_id: str, document_ids: list[str], options: dict) -> list[dict]:
    options = dict(options or {})
    # Per-file page selection: the UI lets each file pick its own pages, and the
    # chosen pages are trimmed off here so every tool inherits the behaviour
    # instead of each handler re-implementing it.
    pages_by_doc = options.pop("pages_by_doc", None) or {}
    if not isinstance(pages_by_doc, dict):
        pages_by_doc = {}

    files = []
    for doc_id in document_ids:
        document, data = library.read_document(doc_id)
        if not document:
            raise FileNotFoundError(f"Document {doc_id} is no longer in the library.")
        spec = str(pages_by_doc.get(doc_id) or "").strip()
        if spec and data and data[:5].startswith(b"%PDF"):
            try:
                data = _subset_pdf(data, spec)
            except Exception as exc:
                raise ValueError(f"{document['filename']}: could not select pages ({exc})") from exc
        files.append({"filename": document["filename"], "data": data, "mimetype": document["mimetype"]})

    if tool_id == "pdf-to-excel":
        from app.converter.pipeline import convert_batch

        result = convert_batch(files, options)
        return result["outputs"]

    return run_tool(tool_id, files, options)


def _subset_pdf(data: bytes, spec: str) -> bytes:
    """Keep only the pages named by ``spec`` (``1-3,7``), in the given order."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    from app.tools.pdf_ops import _ranges

    reader = PdfReader(BytesIO(data))
    total = len(reader.pages)
    wanted: list[int] = []
    for group in _ranges(spec, total):
        for index in group:
            if index not in wanted:
                wanted.append(index)
    if not wanted or len(wanted) >= total:
        return data
    writer = PdfWriter()
    for index in wanted:
        writer.add_page(reader.pages[index])
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _finish(job: dict) -> None:
    payload = job["payload"]
    errors = payload.get("errors") or []
    outputs = payload.get("outputs") or []
    if outputs:
        status = "done"
        message = f"Done — {len(outputs)} file(s) ready" + (f", {len(errors)} step(s) failed" if errors else "")
    else:
        status = "failed"
        message = "Nothing was produced"
    _save(
        job["id"],
        payload,
        status=status,
        message=message,
        error="; ".join(item.get("message", "") for item in errors[:3]),
        progress=100,
    )
    library.touch_batch(job["batch_id"], status)


def _fail(job_id: str, message: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    _save(job_id, job["payload"], status="failed", message="Job failed", error=message, progress=100)
    library.touch_batch(job["batch_id"], "failed")


# ---------------------------------------------------------------- persistence
def _save(job_id: str, payload: dict, *, status: str, message: str, progress: int, error: str = "") -> None:
    get_db().execute(
        "UPDATE jobs SET status = ?, progress = ?, message = ?, error = ?, payload = ?, updated_at = ?"
        " WHERE id = ?",
        (status, max(0, min(int(progress), 100)), message, error or "", json.dumps(payload), library.now_iso(), job_id),
    )


def _update(job_id: str, *, status: str, message: str, progress: int) -> None:
    get_db().execute(
        "UPDATE jobs SET status = ?, progress = ?, message = ?, updated_at = ? WHERE id = ?",
        (status, max(0, min(int(progress), 100)), message, library.now_iso(), job_id),
    )


# ------------------------------------------------------------------- helpers
def _progress(done: int, total: int) -> int:
    if total <= 0:
        return 100
    return int(round(100 * done / total))


def _step_message(tool_id: str, step: list[str], batch: dict | None) -> str:
    count = len(step)
    label = tool_id.replace("-", " ").capitalize()
    if tool_id == "pdf-to-excel" and count > 1:
        return f"{label}: reading {count} files"
    if count == 1:
        document = library.get_document(step[0]) or {}
        return f"{label}: {document.get('filename', 'file')}"
    return f"{label}: {count} files"


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:400]


def _lock_for(job_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[job_id] = lock
        return lock
