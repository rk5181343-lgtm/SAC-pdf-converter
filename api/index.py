"""Vercel serverless entry point.

Vercel's Python runtime looks for a module-level WSGI ``app``; everything else
(the Flask routes, static files, job queue) lives in :mod:`app.server`.
"""
from app.server import app  # noqa: F401

__all__ = ["app"]
