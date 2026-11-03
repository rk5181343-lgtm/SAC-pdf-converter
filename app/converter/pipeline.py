"""Orchestrate a PDF from bytes to a finished workbook."""
from __future__ import annotations

from app import settings
from app.converter import workbook as workbook_builder
from app.converter.extract import extract
from app.converter.layout import build_layout
from app.converter.pdf_source import PdfError, PdfSource
from app.converter.validation import summarise, validate

DEFAULT_SHEETS = {"layout": True, "data": True, "validation": True}
OCR_MODES = {"auto", "always", "never"}


def normalise_options(options: dict | None) -> dict:
    options = dict(options or {})
    sheets = dict(DEFAULT_SHEETS)
    sheets.update({key: value for key, value in (options.get("sheets") or {}).items() if key in DEFAULT_SHEETS})
    ocr = str(options.get("ocr") or "auto").strip().lower()
    return {
        "sheets": {key: bool(value) for key, value in sheets.items()},
        "ocr": ocr if ocr in OCR_MODES else "auto",
        "combine": bool(options.get("combine", True)),
        "images": bool(options.get("images", True)),
        "fills": bool(options.get("fills", True)),
        "max_pages": int(options.get("max_pages") or settings.max_pages),
    }


def analyse(filename: str, data: bytes, options: dict | None = None) -> dict:
    """Read one PDF and produce everything the workbook needs (no writing yet)."""
    options = normalise_options(options)
    try:
        source = PdfSource(data, ocr_mode=options["ocr"])
    except PdfError as exc:
        raise ValueError(str(exc)) from exc

    try:
        if source.page_count == 0:
            raise ValueError(f"{filename}: the PDF has no pages.")
        if source.page_count > options["max_pages"]:
            raise ValueError(
                f"{filename}: {source.page_count} pages is over the {options['max_pages']}-page limit. "
                "Split the file first."
            )

        result = extract(source, filename)
        checks = validate(result)

        layouts = []
        if options["sheets"]["layout"]:
            for page in source.pages():
                layouts.append(
                    build_layout(
                        source,
                        page,
                        "Original Layout",
                        include_images=options["images"],
                        include_fills=options["fills"],
                    )
                )
    finally:
        source.close()

    return {
        "filename": filename,
        "extract": result,
        "checks": checks,
        "layouts": layouts,
        "page_count": source.page_count,
    }


def convert_pdf(filename: str, data: bytes, options: dict | None = None) -> dict:
    """One PDF in, one .xlsx output record out."""
    options = normalise_options(options)
    analysis = analyse(filename, data, options)
    blob = workbook_builder.build(analysis["extract"], analysis["layouts"], analysis["checks"], options)
    return {
        "filename": workbook_builder.output_name(filename),
        "data": blob,
        "mimetype": workbook_builder.XLSX_MIME,
        "meta": describe(analysis),
    }


def convert_batch(files: list[dict], options: dict | None = None) -> dict:
    """Several PDFs in, per-file workbooks plus one combined workbook out.

    A file that cannot be read is reported in ``errors`` and does not stop the
    others; only a run where nothing at all worked raises.
    """
    options = normalise_options(options)
    analyses: list[dict] = []
    errors: list[dict] = []

    for file in files:
        try:
            analyses.append(analyse(file["filename"], file["data"], options))
        except Exception as exc:
            errors.append({"filename": file.get("filename", "document"), "message": str(exc)})

    if not analyses:
        message = "; ".join(f"{item['filename']}: {item['message']}" for item in errors) or "Nothing to convert."
        raise ValueError(message)

    outputs: list[dict] = []
    for analysis in analyses:
        blob = workbook_builder.build(analysis["extract"], analysis["layouts"], analysis["checks"], options)
        outputs.append(
            {
                "filename": workbook_builder.output_name(analysis["filename"]),
                "data": blob,
                "mimetype": workbook_builder.XLSX_MIME,
                "meta": {**describe(analysis), "kind": "workbook"},
            }
        )

    if len(analyses) > 1 and options["combine"]:
        combined = workbook_builder.build_combined(analyses, options)
        outputs.append(
            {
                "filename": "Combined workbook.xlsx",
                "data": combined,
                "mimetype": workbook_builder.XLSX_MIME,
                "meta": {
                    "kind": "combined",
                    "files": [analysis["filename"] for analysis in analyses],
                    "rows": sum(analysis["extract"].row_count for analysis in analyses),
                    "checks": _combine_counts(analyses),
                },
            }
        )

    return {"outputs": outputs, "errors": errors, "summaries": [describe(analysis) for analysis in analyses]}


def describe(analysis: dict) -> dict:
    result = analysis["extract"]
    counts = summarise(analysis["checks"])
    return {
        "doc_type": result.doc_type,
        "doc_label": result.doc_label,
        "confidence": result.confidence,
        "rows": result.row_count,
        "columns": len(result.columns),
        "column_names": result.columns,
        "pages": result.pages_total,
        "ocr_pages": result.ocr_pages,
        "checks": counts,
        "notes": result.notes,
        "source": analysis["filename"],
        "kind": "workbook",
    }


def _combine_counts(analyses: list[dict]) -> dict[str, int]:
    totals = {"Passed": 0, "Warning": 0, "Error": 0, "Not Applicable": 0}
    for analysis in analyses:
        for status, count in summarise(analysis["checks"]).items():
            totals[status] = totals.get(status, 0) + count
    return totals
