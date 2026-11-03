"""The conversion engine.

Pipeline per document::

    pdf_source  →  extract  →  validation  →  layout  →  workbook
    (open/read)    (records)   (financial)    (visual)   (.xlsx + stats)

``pipeline.convert_batch`` is the entry point used by the job runner.
"""
from app.converter.pipeline import convert_batch, convert_pdf  # noqa: F401

__all__ = ["convert_batch", "convert_pdf"]
