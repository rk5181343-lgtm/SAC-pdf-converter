"""Batches and documents — the shared Library.

A *batch* is one run of one tool. It owns its inputs (the files a user dropped
in) and its outputs (everything the run produced). Because the workspace has no
sign-in, every batch belongs to the workspace itself and any visitor can list
and download from it.

File bytes live either on disk (``STORAGE_MODE=fs``) or in the database
(``STORAGE_MODE=db``, the default on serverless hosts) — see
:func:`app.settings.Settings.storage_mode`. Callers only ever see
:func:`add_document` / :func:`read_document`.
"""
from __future__ import annotations

import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import settings
from app.db import get_db

KIND_INPUT = "input"
KIND_OUTPUT = "output"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_filename(name: str, default: str = "file") -> str:
    """Bare, filesystem-safe file name (no directories, no control characters)."""
    base = Path((name or "").replace("\\", "/")).name.strip()
    base = _SAFE_NAME.sub("_", base).strip(" .")
    return base[:120] or default


def _db_storage() -> bool:
    return settings.storage_mode == "db"


# --------------------------------------------------------------------- batches
def create_batch(tool: str, title: str = "", note: str = "") -> dict:
    batch_id = uuid.uuid4().hex
    stamp = now_iso()
    label = (title or "").strip() or f"{tool} · {stamp[:16].replace('T', ' ')}"
    get_db().execute(
        "INSERT INTO batches (id, title, tool, note, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, label[:180], tool, note or "", "open", stamp, stamp),
    )
    return get_batch(batch_id) or {}


def get_batch(batch_id: str) -> dict | None:
    row = get_db().query_one(
        "SELECT id, title, tool, note, status, created_at, updated_at FROM batches WHERE id = ?",
        (batch_id,),
    )
    if not row:
        return None
    row["documents"] = [_public_document(doc) for doc in list_documents(batch_id)]
    return row


def list_batches(limit: int = 50, offset: int = 0) -> list[dict]:
    db = get_db()
    rows = db.query(
        "SELECT id, title, tool, note, status, created_at, updated_at FROM batches"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (max(1, min(limit, 200)), max(0, offset)),
    )
    tally: dict[str, dict[str, int]] = {}
    for item in db.query(
        "SELECT batch_id, kind, COUNT(*) AS total FROM documents GROUP BY batch_id, kind"
    ):
        tally.setdefault(item["batch_id"], {})[item["kind"]] = int(item["total"] or 0)
    for row in rows:
        row["counts"] = tally.get(row["id"], {})
    return rows


def touch_batch(batch_id: str, status: str | None = None) -> None:
    stamp = now_iso()
    if status:
        get_db().execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE id = ?", (status, stamp, batch_id)
        )
    else:
        get_db().execute("UPDATE batches SET updated_at = ? WHERE id = ?", (stamp, batch_id))


# ------------------------------------------------------------------- documents
def add_document(
    batch_id: str,
    kind: str,
    filename: str,
    data: bytes,
    mimetype: str = "",
    meta: dict[str, Any] | None = None,
) -> dict:
    doc_id = uuid.uuid4().hex
    name = safe_filename(filename, default="document")
    mime = mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    stored_path: str | None = None
    blob: bytes | None = None

    if _db_storage():
        blob = data
    else:
        directory = settings.storage_dir / batch_id[:2] / batch_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{doc_id}__{name}"
        target.write_bytes(data)
        try:
            stored_path = str(target.relative_to(settings.storage_dir))
        except ValueError:  # pragma: no cover - storage dir outside base
            stored_path = str(target)

    get_db().execute(
        "INSERT INTO documents (id, batch_id, kind, filename, mimetype, size, path, data, meta, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, batch_id, kind, name, mime, len(data), stored_path, blob, json.dumps(meta or {}), now_iso()),
    )
    touch_batch(batch_id)
    return get_document(doc_id) or {}


def list_documents(batch_id: str, kind: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, batch_id, kind, filename, mimetype, size, path, meta, created_at"
        " FROM documents WHERE batch_id = ?"
    )
    params: list[Any] = [batch_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at ASC"
    return [_public_document(row) for row in get_db().query(sql, tuple(params))]


def get_document(doc_id: str) -> dict | None:
    row = get_db().query_one(
        "SELECT id, batch_id, kind, filename, mimetype, size, path, meta, created_at"
        " FROM documents WHERE id = ?",
        (doc_id,),
    )
    return _public_document(row) if row else None


def read_document(doc_id: str) -> tuple[dict, bytes] | tuple[None, None]:
    row = get_db().query_one(
        "SELECT id, batch_id, kind, filename, mimetype, size, path, data, meta, created_at"
        " FROM documents WHERE id = ?",
        (doc_id,),
    )
    if not row:
        return None, None
    payload = row.get("data")
    meta = row
    if payload is None and row.get("path"):
        candidate = Path(row["path"])
        if not candidate.is_absolute():
            candidate = settings.storage_dir / candidate
        try:
            payload = candidate.read_bytes()
        except OSError:
            payload = None
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    return _public_document(meta), payload or b""


def delete_document(doc_id: str) -> bool:
    doc = get_document(doc_id)
    if not doc:
        return False
    if not _db_storage() and doc.get("path"):
        candidate = Path(doc["path"])
        if not candidate.is_absolute():
            candidate = settings.storage_dir / candidate
        try:
            candidate.unlink()
        except OSError:
            pass
    get_db().execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return True


def delete_batch(batch_id: str) -> bool:
    if not get_batch(batch_id):
        return False
    for doc in list_documents(batch_id):
        delete_document(doc["id"])
    get_db().execute("DELETE FROM jobs WHERE batch_id = ?", (batch_id,))
    get_db().execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    return True


def workspace_stats() -> dict:
    db = get_db()
    batches = db.query_one("SELECT COUNT(*) AS total FROM batches") or {"total": 0}
    docs = db.query_one("SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS bytes FROM documents") or {}
    outputs = db.query_one("SELECT COUNT(*) AS total FROM documents WHERE kind = 'output'") or {"total": 0}
    return {
        "batches": int(batches.get("total") or 0),
        "documents": int(docs.get("total") or 0),
        "outputs": int(outputs.get("total") or 0),
        "bytes": int(docs.get("bytes") or 0),
        "database": settings.database_label,
        "storage": settings.storage_mode,
        "jobs": settings.mode_label,
    }


def _public_document(row: dict) -> dict:
    meta = row.get("meta") or "{}"
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return {
        "id": row.get("id"),
        "batch_id": row.get("batch_id"),
        "kind": row.get("kind"),
        "filename": row.get("filename"),
        "mimetype": row.get("mimetype") or "application/octet-stream",
        "size": int(row.get("size") or 0),
        "meta": meta,
        "created_at": row.get("created_at"),
        "download_url": f"/api/documents/{row.get('id')}/download",
    }
