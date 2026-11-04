"""Application package.

Importing :mod:`app` is the first thing every entry point does, so this is where
``.env`` is read: modules that need configuration do ``from app import
settings`` and can rely on the file having been loaded already.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load ``.env`` from the project root, if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:  # pragma: no cover - dotenv is optional at runtime
        return
    load_dotenv(BASE_DIR / ".env")


_load_dotenv()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = (os.environ.get(name) or "").strip()
    return Path(raw).expanduser() if raw else default


class Settings:
    """Process-wide configuration, resolved once from the environment.

    Nothing here touches the network or the filesystem, so importing the
    package stays cheap on a cold start.
    """

    def __init__(self) -> None:
        self.base_dir = BASE_DIR

        # Storage
        self.database_url = (os.environ.get("DATABASE_URL") or "").strip()
        self.serverless = env_flag("VERCEL") or env_flag("SERVERLESS")
        # A serverless bundle is read-only, so SQLite has to live in the one
        # writable place: /tmp. It is ephemeral, which is why production
        # deployments should set DATABASE_URL to Postgres instead.
        default_sqlite = (
            Path("/tmp/firm-workspace/workspace.db")
            if self.serverless
            else BASE_DIR / "data" / "workspace.db"
        )
        self.sqlite_path = _env_path("SQLITE_PATH", default_sqlite)
        self.storage_dir = _env_path("STORAGE_DIR", BASE_DIR / "data" / "files")
        # "fs" keeps uploads on disk, "db" keeps them in the database. Serverless
        # runtimes have a read-only bundle and an ephemeral /tmp, so default to
        # the database there.
        self.storage_mode = (os.environ.get("STORAGE_MODE") or ("db" if self.serverless else "fs")).strip().lower()

        # Jobs: a worker thread is fine on a long-lived process; behind a
        # serverless function the browser drives the job forward instead.
        self.background_threads = env_flag("BACKGROUND_THREADS", default=not self.serverless)

        # Limits. A serverless host rejects a large request body before the app
        # runs, which the browser reports as a dropped connection rather than an
        # error message — so stay under the platform body limit there and let
        # the app answer with a real 413. MAX_UPLOAD_MB always wins.
        configured_mb = int(os.environ.get("MAX_UPLOAD_MB") or 40)
        explicit_mb = "MAX_UPLOAD_MB" in os.environ
        self.max_upload_mb = configured_mb if explicit_mb or not self.serverless else min(configured_mb, 4)
        self.max_pages = int(os.environ.get("MAX_PAGES") or 150)
        self.max_files_per_run = int(os.environ.get("MAX_FILES_PER_RUN") or 25)

        # Optional services
        self.admin_token = (os.environ.get("ADMIN_TOKEN") or "").strip()
        self.exa_api_key = (os.environ.get("EXA_API_KEY") or "").strip()
        self.ocr_dpi = int(os.environ.get("OCR_DPI") or 200)

        # Rendering
        self.render_dpi = int(os.environ.get("RENDER_DPI") or 150)
        self.debug = env_flag("FLASK_DEBUG", default=not self.serverless)

    @property
    def database_label(self) -> str:
        return "postgres" if self.database_url.startswith(("postgres://", "postgresql://")) else "sqlite"

    @property
    def mode_label(self) -> str:
        return "poll" if not self.background_threads else "thread"


settings = Settings()
