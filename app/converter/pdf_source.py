"""Open a PDF once and expose the page data the converter needs.

Everything downstream reads :class:`PageData`: text, positioned words, tables
and image boxes, all in PDF points. Scanned pages carry no text layer, so this
module also owns the OCR fallback — the page is rendered and passed through
RapidOCR, then the recognised boxes are converted back into PDF coordinates so
they line up with the rest of the pipeline.

Nothing here writes files: sources are always opened from bytes so the same
code works for uploads, library downloads and tests.
"""
from __future__ import annotations

import importlib.util
import io
import statistics
from dataclasses import dataclass, field

import pdfplumber
from PIL import Image

from app import settings

OCR_CHAR_THRESHOLD = 32
_OCR_ENGINE = None


class PdfError(RuntimeError):
    """Raised when a file cannot be opened as a PDF."""


@dataclass(slots=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float = 0.0
    bold: bool = False

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)

    @property
    def centre(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class TableData:
    rows: list[list[str]]
    bbox: tuple[float, float, float, float] | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.rows), max((len(row) for row in self.rows), default=0)


@dataclass
class PageData:
    index: int
    width: float
    height: float
    text: str
    words: list[Word] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)
    images: list[tuple[float, float, float, float]] = field(default_factory=list)
    ocr_used: bool = False

    @property
    def has_text(self) -> bool:
        return len(self.text.strip()) >= 8

    @property
    def lines(self) -> list[str]:
        return lines_from_words(self.words) if self.words else [ln for ln in self.text.splitlines() if ln.strip()]


# ------------------------------------------------------------------------ OCR
def ocr_available() -> bool:
    try:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_image(image: Image.Image, dpi: int) -> list[Word]:
    """Run OCR over a rendered page and return words in PDF points."""
    import numpy as np

    engine = _ocr_engine()
    result, _elapsed = engine(np.asarray(image.convert("RGB")))
    scale = 72.0 / max(dpi, 1)
    words: list[Word] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        if not str(text).strip():
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        words.append(
            Word(
                text=str(text).strip(),
                x0=min(xs) * scale,
                x1=max(xs) * scale,
                top=min(ys) * scale,
                bottom=max(ys) * scale,
                size=max(1.0, (max(ys) - min(ys)) * scale),
            )
        )
    return words


def lines_from_words(words: list[Word], tolerance: float | None = None) -> list[str]:
    """Group words into visual lines and join them with single spaces."""
    if not words:
        return []
    tolerance = tolerance or max(2.0, statistics.median(word.height for word in words) * 0.6)
    ordered = sorted(words, key=lambda word: (word.centre, word.x0))
    lines: list[list[Word]] = [[ordered[0]]]
    line_top = ordered[0].centre
    for word in ordered[1:]:
        if abs(word.centre - line_top) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
            line_top = word.centre
    return [
        " ".join(word.text for word in sorted(line, key=lambda item: item.x0)).strip()
        for line in lines
        if line
    ]


# --------------------------------------------------------------------- source
class PdfSource:
    """A PDF opened for reading: pages, renders and crops from one object."""

    def __init__(self, data: bytes, ocr_mode: str = "auto", dpi: int | None = None) -> None:
        if not data:
            raise PdfError("The file is empty.")
        self.data = data
        self.ocr_mode = (ocr_mode or "auto").strip().lower()
        self.dpi = int(dpi or settings.render_dpi)
        try:
            self._plumber = pdfplumber.open(io.BytesIO(data))
        except Exception as exc:  # pdfminer raises a wide range of errors
            raise PdfError(
                f"This file could not be opened as a PDF ({exc}). "
                "If it is password protected, unlock it first."
            ) from exc
        self._pdfium = None
        self._pages: dict[int, PageData] = {}
        self._renders: dict[int, Image.Image] = {}

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        for closer in (getattr(self._plumber, "close", None), getattr(self._pdfium, "close", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        self._pages.clear()
        self._renders.clear()

    def __enter__(self) -> "PdfSource":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- pages -------------------------------------------------------------
    @property
    def page_count(self) -> int:
        return len(self._plumber.pages)

    def pages(self) -> list[PageData]:
        return [self.page(index) for index in range(self.page_count)]

    def page(self, index: int) -> PageData:
        if index in self._pages:
            return self._pages[index]
        if index < 0 or index >= self.page_count:
            raise PdfError(f"Page {index + 1} is outside this document ({self.page_count} pages).")

        page = self._plumber.pages[index]
        # One layout pass per page, not two: the positioned words already carry
        # the page's text, and extract_text() is pdfminer's most expensive call.
        words = self._words(page)
        text = "\n".join(lines_from_words(words)) if words else self._page_text(page)

        tables = self._tables(page)
        images = self._image_boxes(page)

        ocr_used = False
        if self._needs_ocr(text, words) and ocr_available():
            try:
                image = self.render(index)
                ocr_words = ocr_image(image, self.dpi)
            except Exception:
                ocr_words = []
            if ocr_words:
                ocr_used = True
                words = ocr_words
                text = "\n".join(lines_from_words(ocr_words))

        data = PageData(
            index=index,
            width=float(page.width),
            height=float(page.height),
            text=text,
            words=words,
            tables=tables,
            images=images,
            ocr_used=ocr_used,
        )
        self._pages[index] = data
        return data

    def _needs_ocr(self, text: str, words: list[Word]) -> bool:
        if self.ocr_mode == "never":
            return False
        if self.ocr_mode == "always":
            return True
        visible = len(text.strip())
        return visible < OCR_CHAR_THRESHOLD and len(words) < 6

    def _page_text(self, page) -> str:
        """Text for pages that carry no positioned words (a font pdfminer cannot box)."""
        try:
            return page.extract_text() or ""
        except Exception:
            return ""

    def _words(self, page) -> list[Word]:
        try:
            raw = page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False)
        except Exception:
            try:
                raw = page.extract_words()
            except Exception:
                return []
        words: list[Word] = []
        for item in raw:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            font = (item.get("fontname") or "").lower()
            words.append(
                Word(
                    text=text,
                    x0=float(item.get("x0") or 0.0),
                    x1=float(item.get("x1") or 0.0),
                    top=float(item.get("top") or 0.0),
                    bottom=float(item.get("bottom") or 0.0),
                    size=float(item.get("size") or 0.0) or max(float(item.get("bottom", 0)) - float(item.get("top", 0)), 1.0),
                    bold="bold" in font or "black" in font,
                )
            )
        return words

    def _tables(self, page) -> list[TableData]:
        tables: list[TableData] = []
        try:
            found = page.find_tables()
        except Exception:
            return tables
        for table in found:
            try:
                rows = table.extract() or []
            except Exception:
                continue
            cleaned: list[list[str]] = []
            for row in rows:
                cleaned.append([(cell or "").strip() for cell in row])
            if any(any(cell for cell in row) for row in cleaned):
                bbox = getattr(table, "bbox", None)
                tables.append(TableData(rows=cleaned, bbox=tuple(bbox) if bbox else None))
        return tables

    def _image_boxes(self, page) -> list[tuple[float, float, float, float]]:
        boxes = []
        for image in getattr(page, "images", []) or []:
            try:
                boxes.append(
                    (
                        float(image.get("x0") or 0.0),
                        float(image.get("top") or 0.0),
                        float(image.get("x1") or 0.0),
                        float(image.get("bottom") or 0.0),
                    )
                )
            except (TypeError, ValueError):
                continue
        return boxes

    # -- rendering ---------------------------------------------------------
    def document(self):
        if self._pdfium is None:
            import pypdfium2 as pdfium

            try:
                self._pdfium = pdfium.PdfDocument(self.data)
            except Exception as exc:
                raise PdfError(f"This PDF could not be rendered ({exc}).") from exc
        return self._pdfium

    def render(self, index: int, dpi: int | None = None) -> Image.Image:
        if index in self._renders and dpi is None:
            return self._renders[index]
        document = self.document()
        target_dpi = int(dpi or self.dpi)
        page = document[index]
        image = page.render(scale=target_dpi / 72.0).to_pil().convert("RGB")
        if dpi is None:
            self._renders[index] = image
        return image

    def crop(self, index: int, bbox: tuple[float, float, float, float], dpi: int | None = None) -> Image.Image:
        """Crop a render to a PDF-point box."""
        image = self.render(index, dpi=dpi)
        source_page = self._plumber.pages[index]
        scale = image.width / float(source_page.width or 1)
        left, top, right, bottom = bbox
        box = (
            max(0, int(left * scale)),
            max(0, int(top * scale)),
            min(image.width, int(right * scale)),
            min(image.height, int(bottom * scale)),
        )
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            raise PdfError("That region is too small to extract.")
        return image.crop(box)

    def thumbnail(self, index: int, max_pixels: int = 520) -> Image.Image:
        image = self.render(index, dpi=110)
        image.thumbnail((max_pixels, max_pixels))
        return image


def open_pdf(data: bytes, ocr_mode: str = "auto", dpi: int | None = None) -> PdfSource:
    return PdfSource(data, ocr_mode=ocr_mode, dpi=dpi)
