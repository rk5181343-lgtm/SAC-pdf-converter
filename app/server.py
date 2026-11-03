"""Flask app: every HTTP route the workspace needs.

The workspace has no sign-in: one shared library, one tool list, one job queue.
Routes come in five groups — pages, tools, uploads/jobs, library and documents —
and all of them answer JSON except downloads and thumbnails.

Uploads are deliberately separate from runs: a file is uploaded once, the UI can
then show page thumbnails, and the run is started against the stored documents.
That keeps "the grid shows exactly what the run will do" honest.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from app import exa_search, excel_runner, library, settings
from app.db import init_db
from app.tools import all_tools, groups, tool_or_none

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/assets")
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_mb * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False

    # On a serverless host the bundle is read-only and a cold start can happen
    # before Postgres is reachable, so a missing database must not crash the
    # import: it is retried per request and reported as 503 for API calls.
    db_state = {"ready": False}

    def ensure_database() -> None:
        if not db_state["ready"]:
            init_db()
            db_state["ready"] = True

    try:
        ensure_database()
    except Exception as exc:  # pragma: no cover - read-only bundle / cold Postgres
        app.logger.warning("Workspace database is not ready yet: %s", exc)

    @app.before_request
    def _ensure_database():
        if db_state["ready"] or not request.path.startswith("/api/"):
            return None
        try:
            ensure_database()
        except Exception as exc:  # pragma: no cover - depends on the host
            app.logger.exception("Workspace database unavailable: %s", exc)
            return _error(
                "The workspace database is not available. Point DATABASE_URL at a Postgres "
                "database for this deployment, or make the storage directory writable.",
                503,
            )
        return None

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "logo-mark.svg", mimetype="image/svg+xml")

    @app.get("/api/health")
    def health():
        from app.converter.pdf_source import ocr_available

        return jsonify(
            {
                "status": "ok",
                "database": settings.database_label,
                "storage": settings.storage_mode,
                "jobs": settings.mode_label,
                "database_ready": db_state["ready"],
                "ocr": ocr_available(),
                "exa": exa_search.enabled(),
                "limits": _limits(),
            }
        )

    # ------------------------------------------------------------------ tools
    @app.get("/api/tools")
    def tools():
        return jsonify({"groups": groups(), "tools": all_tools(), "limits": _limits()})

    # ---------------------------------------------------------------- uploads
    @app.post("/api/uploads")
    def uploads():
        files = [item for item in request.files.getlist("files") if item and item.filename]
        problem = _upload_problem(files)
        if problem:
            return _error(problem, 400)

        batch = library.create_batch("upload", title=_batch_title(files))
        stored = []
        for upload in files:
            data = upload.read()
            if not data:
                continue
            meta = {"pages": _pdf_pages(data)} if _is_pdf(upload.filename, data) else {}
            stored.append(
                library.add_document(
                    batch["id"], library.KIND_INPUT, upload.filename, data, upload.mimetype or "", meta
                )
            )
        if not stored:
            library.delete_batch(batch["id"])
            return _error("The attached files were empty.", 400)
        return jsonify({"batch": library.get_batch(batch["id"])}), 201

    @app.post("/api/batches/<batch_id>/run")
    def run_batch(batch_id: str):
        batch = library.get_batch(batch_id)
        if not batch:
            return _error("That batch does not exist.", 404)
        payload = request.get_json(silent=True) or {}
        tool_id = str(payload.get("tool") or "").strip()
        tool = tool_or_none(tool_id)
        if not tool:
            return _error("That tool does not exist.", 404)

        options = payload.get("options") or {}
        if not isinstance(options, dict):
            return _error("Options must be an object.", 400)
        if tool_id == "pdf-to-excel":
            options = _excel_options(options)
        if tool.get("page_ranges"):
            options = {**options, "pages": str(options.get("pages") or "").strip()}

        inputs = library.list_documents(batch_id, library.KIND_INPUT)
        if not inputs:
            return _error("Upload at least one file first.", 400)
        if not tool.get("multiple") and len(inputs) > 1:
            return _error(f"{tool['label']} works on one file at a time.", 400)

        try:
            job = excel_runner.submit(batch_id, tool_id, [doc["id"] for doc in inputs], options)
        except Exception as exc:
            library.touch_batch(batch_id, "failed")
            return _error(str(exc), 400)

        from app.db import get_db

        get_db().execute(
            "UPDATE batches SET title = ?, tool = ? WHERE id = ?",
            (f"{tool['label']} · {batch['title'][:140]}", tool_id, batch_id),
        )
        return jsonify({"batch": library.get_batch(batch_id), "job": job}), 202

    @app.post("/api/tools/<tool_id>/run")
    def run_tool(tool_id: str):
        """One-shot run: upload and start in the same request."""
        tool = tool_or_none(tool_id)
        if not tool:
            return _error("That tool does not exist.", 404)
        files = [item for item in request.files.getlist("files") if item and item.filename]
        problem = _upload_problem(files)
        if problem:
            return _error(problem, 400)

        options = _parse_options()
        if options is None:
            return _error("The options payload is not valid JSON.", 400)
        if tool_id == "pdf-to-excel":
            options = _excel_options(options)

        batch = library.create_batch(tool_id, title=_batch_title(files))
        stored = []
        for upload in files:
            data = upload.read()
            if not data:
                continue
            accepted = _accepts(tool, upload.filename, data)
            if accepted:
                library.delete_batch(batch["id"])
                return _error(accepted, 400)
            stored.append(
                library.add_document(
                    batch["id"], library.KIND_INPUT, upload.filename, data, upload.mimetype or ""
                )
            )
        if not stored:
            library.delete_batch(batch["id"])
            return _error("The attached files were empty.", 400)

        try:
            job = excel_runner.submit(batch["id"], tool_id, [doc["id"] for doc in stored], options)
        except Exception as exc:
            library.touch_batch(batch["id"], "failed")
            return _error(str(exc), 400)
        return jsonify({"batch": library.get_batch(batch["id"]), "job": job}), 202

    # ------------------------------------------------------------------- jobs
    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = excel_runner.public_job(job_id)
        if not job:
            return _error("That job does not exist.", 404)
        return jsonify({"job": job})

    @app.post("/api/jobs/<job_id>/advance")
    def advance_job(job_id: str):
        job = excel_runner.advance(job_id)
        if not job:
            return _error("That job does not exist.", 404)
        return jsonify({"job": job, "batch": library.get_batch(job["batch_id"])})

    # ---------------------------------------------------------------- library
    @app.get("/api/library")
    @app.get("/api/batches")
    def list_batches():
        return jsonify(
            {
                "batches": library.list_batches(
                    limit=_int(request.args.get("limit"), 50), offset=_int(request.args.get("offset"), 0)
                ),
                "stats": library.workspace_stats(),
            }
        )

    @app.post("/api/batches")
    def create_batch():
        payload = request.get_json(silent=True) or {}
        batch = library.create_batch(
            str(payload.get("tool") or "manual"), title=str(payload.get("title") or "")
        )
        return jsonify({"batch": batch}), 201

    @app.get("/api/batches/<batch_id>")
    def get_batch(batch_id: str):
        batch = library.get_batch(batch_id)
        if not batch:
            return _error("That batch does not exist.", 404)
        return jsonify({"batch": batch, "jobs": excel_runner.list_jobs(batch_id)})

    @app.delete("/api/batches/<batch_id>")
    def delete_batch(batch_id: str):
        if not library.delete_batch(batch_id):
            return _error("That batch does not exist.", 404)
        return jsonify({"deleted": batch_id})

    @app.get("/api/library/stats")
    def stats():
        return jsonify(library.workspace_stats())

    @app.post("/api/library/clear")
    def clear_library():
        if settings.admin_token:
            if request.headers.get("X-Admin-Token", "") != settings.admin_token:
                return _error("An admin token is required to clear the library.", 403)
        removed = 0
        for batch in library.list_batches(limit=200):
            if library.delete_batch(batch["id"]):
                removed += 1
        return jsonify({"deleted_batches": removed})

    # -------------------------------------------------------------- documents
    @app.get("/api/documents/<doc_id>/download")
    def download(doc_id: str):
        document, data = library.read_document(doc_id)
        if not document or data is None:
            return _error("That file is no longer in the library.", 404)
        inline = request.args.get("inline") == "1"
        return send_file(
            io.BytesIO(data),
            mimetype=document["mimetype"],
            as_attachment=not inline,
            download_name=document["filename"],
        )

    @app.get("/api/documents/<doc_id>/pages")
    def document_pages(doc_id: str):
        document, data = library.read_document(doc_id)
        if not document or not data:
            return _error("That file is no longer in the library.", 404)
        from app.converter.pdf_source import PdfError, PdfSource

        try:
            with PdfSource(data, ocr_mode="never") as source:
                return jsonify({"pages": source.page_count, "filename": document["filename"]})
        except PdfError as exc:
            return _error(str(exc), 400)

    @app.get("/api/documents/<doc_id>/thumb/<int:page>")
    def thumbnail(doc_id: str, page: int):
        document, data = library.read_document(doc_id)
        if not document or not data:
            return _error("That file is no longer in the library.", 404)
        from app.converter.pdf_source import PdfError, PdfSource

        try:
            with PdfSource(data, ocr_mode="never") as source:
                if page < 1 or page > source.page_count:
                    return _error("That page does not exist.", 404)
                image = source.thumbnail(page - 1)
        except PdfError as exc:
            return _error(str(exc), 400)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        response = send_file(buffer, mimetype="image/png", max_age=300)
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.delete("/api/documents/<doc_id>")
    def delete_document(doc_id: str):
        if not library.delete_document(doc_id):
            return _error("That file is no longer in the library.", 404)
        return jsonify({"deleted": doc_id})

    # ------------------------------------------------------------------ extras
    @app.get("/api/search")
    def search():
        return jsonify(
            exa_search.search(str(request.args.get("q") or ""), num_results=_int(request.args.get("n"), 4))
        )

    # ------------------------------------------------------------------ errors
    @app.errorhandler(413)
    def too_large(_error_info):
        return _error(f"That upload is over the {settings.max_upload_mb} MB limit.", 413)

    @app.errorhandler(404)
    def missing(_error_info):
        if request.path.startswith("/api/"):
            return _error("Not found.", 404)
        return send_from_directory(app.static_folder, "index.html")

    @app.errorhandler(500)
    def server_error(error_info):  # pragma: no cover - safety net
        app.logger.exception("Unhandled error: %s", error_info)
        return _error("Something went wrong while handling that request.", 500)

    return app


# ---------------------------------------------------------------------- helpers
def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _limits() -> dict:
    return {
        "upload_mb": settings.max_upload_mb,
        "pages": settings.max_pages,
        "files": settings.max_files_per_run,
    }


def _parse_options() -> dict | None:
    raw = request.form.get("options")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _excel_options(options: dict) -> dict:
    """Flatten the UI's Excel switches into the pipeline's option shape."""
    return {
        "combine": bool(options.get("combine", True)),
        "ocr": options.get("ocr", "auto"),
        "images": bool(options.get("images", True)),
        "fills": bool(options.get("fills", True)),
        "sheets": {
            "layout": bool(options.get("layout", True)),
            "data": True,
            "validation": True,
        },
    }


def _batch_title(files) -> str:
    names = [Path(item.filename).name for item in files[:2]]
    suffix = f" +{len(files) - 2}" if len(files) > 2 else ""
    return (", ".join(names) + suffix)[:160] or "Upload"


def _upload_problem(files) -> str | None:
    if not files:
        return "Attach at least one file."
    if len(files) > settings.max_files_per_run:
        return f"At most {settings.max_files_per_run} files per run."
    for item in files:
        suffix = Path(item.filename or "").suffix.lower()
        if suffix not in PDF_EXTENSIONS | IMAGE_EXTENSIONS:
            return f"{item.filename}: upload PDFs or images (PNG, JPG, WebP)."
    return None


def _accepts(tool: dict, filename: str, data: bytes) -> str | None:
    suffix = Path(filename or "").suffix.lower()
    if tool.get("accepts") == "images":
        if suffix not in IMAGE_EXTENSIONS:
            return f"{tool['label']} needs images (PNG, JPG, WebP), not {suffix or 'that file'}."
        return None
    if suffix not in PDF_EXTENSIONS and not data[:5].startswith(b"%PDF"):
        return f"{tool['label']} needs PDF files, not {suffix or 'that file'}."
    return None


def _is_pdf(filename: str, data: bytes) -> bool:
    return Path(filename or "").suffix.lower() in PDF_EXTENSIONS or data[:5].startswith(b"%PDF")


def _pdf_pages(data: bytes) -> int | None:
    try:
        from app.converter.pdf_source import PdfSource

        with PdfSource(data, ocr_mode="never") as source:
            return source.page_count
    except Exception:
        return None


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or 5000), debug=settings.debug)
