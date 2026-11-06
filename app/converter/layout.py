"""Rebuild a page visually on a cell grid.

The grid is learned from the page itself: column boundaries come from where
words actually start and end, row bands from where lines sit. That keeps the
"Original Layout" sheet readable instead of stamping everything into column A,
and it keeps headings, table columns and totals in the same place they appear in
the PDF.
"""
from __future__ import annotations

import bisect
import io
import statistics
from dataclasses import dataclass, field

from PIL import Image

from app.converter.pdf_source import PageData, PdfSource, Word

MAX_COLUMNS = 80
MAX_ROWS = 160
MAX_IMAGES = 12


@dataclass
class GridCell:
    value: str
    bold: bool = False
    size: float = 10.0
    fill: str | None = None
    align: str = "left"


@dataclass
class GridImage:
    row: int
    col: int
    data: bytes
    width: int
    height: int


@dataclass
class SheetLayout:
    name: str
    cells: dict[tuple[int, int], GridCell] = field(default_factory=dict)
    col_widths: dict[int, float] = field(default_factory=dict)
    row_heights: dict[int, float] = field(default_factory=dict)
    images: list[GridImage] = field(default_factory=list)
    rows: int = 0
    cols: int = 0

    def is_empty(self) -> bool:
        return not self.cells and not self.images


def build_layout(
    source: PdfSource,
    page: PageData,
    name: str,
    include_images: bool = True,
    include_fills: bool = True,
) -> SheetLayout:
    words = list(page.words)
    if not words:
        return _text_fallback(page, name)

    median_height = statistics.median(word.height for word in words) or 10.0
    median_char = statistics.median(
        [(word.x1 - word.x0) / max(len(word.text), 1) for word in words if word.text]
    ) or (median_height * 0.5)

    boundaries = _column_boundaries(words, page.width, median_char)
    bands = _row_bands(words, median_height)

    layout = SheetLayout(name=name, rows=len(bands), cols=max(len(boundaries) - 1, 1))

    for col in range(layout.cols):
        width_points = boundaries[col + 1] - boundaries[col]
        layout.col_widths[col] = max(1.2, round((width_points * 96 / 72) / 7, 1))

    for row, band in enumerate(bands):
        height_points = max(8.0, (band["bottom"] - band["top"]) * 1.18)
        layout.row_heights[row] = round(min(height_points, 36.0), 1)

    fills = _fills(source, page) if include_fills else None

    for word in words:
        row = _index_for(bands, word.centre, key="centre")
        col = min(max(_column_index(boundaries, word.x0), 0), layout.cols - 1)
        key = (row, col)
        cell = layout.cells.get(key)
        size = _excel_font_size(word, median_height)
        bold = word.bold or (word.size and word.size >= median_height * 1.18)
        if cell is None:
            layout.cells[key] = GridCell(value=word.text, bold=bool(bold), size=size)
        else:
            separator = " " if abs(word.centre - _band_centre(bands[row])) < median_height * 0.6 else "\n"
            cell.value = f"{cell.value}{separator}{word.text}"
            cell.bold = cell.bold or bool(bold)
            cell.size = max(cell.size, size)

    if fills:
        for (row, col), cell in layout.cells.items():
            if cell.value.strip():
                colour = _sample_fill(fills, boundaries, bands, row, col)
                if colour:
                    cell.fill = colour

    if include_images:
        layout.images = _images(source, page, boundaries, bands)

    return layout


# --------------------------------------------------------------------- columns
def _column_boundaries(words: list[Word], page_width: float, median_char: float) -> list[float]:
    tolerance = max(1.5, median_char * 1.3)
    edges = sorted({round(word.x0, 2) for word in words} | {round(word.x1, 2) for word in words})
    boundaries = [0.0]
    previous = 0.0
    for edge in _cluster(edges, tolerance):
        if edge - previous < max(4.0, median_char * 2):
            continue
        boundaries.append(round(edge, 2))
        previous = edge
        if len(boundaries) > MAX_COLUMNS:
            break
    boundaries.append(round(page_width, 2))
    if len(boundaries) < 2:
        boundaries = [0.0, round(page_width, 2)]
    return boundaries


def _column_index(boundaries: list[float], x: float) -> int:
    return max(0, bisect.bisect_right(boundaries, x) - 1)


# ------------------------------------------------------------------------ rows
def _row_bands(words: list[Word], median_height: float) -> list[dict]:
    tolerance = max(2.0, median_height * 0.55)
    centres = _cluster([word.centre for word in words], tolerance)
    bands = [
        {"centre": centre, "top": centre - median_height / 2, "bottom": centre + median_height / 2}
        for centre in centres
    ]
    for band in bands:
        assigned = [word for word in words if abs(word.centre - band["centre"]) <= tolerance]
        if assigned:
            band["top"] = min(word.top for word in assigned)
            band["bottom"] = max(word.bottom for word in assigned)
    bands = bands[:MAX_ROWS]
    return bands


def _band_centre(band: dict) -> float:
    return (band["top"] + band["bottom"]) / 2


def _index_for(bands: list[dict], value: float, key: str = "centre") -> int:
    if not bands:
        return 0
    best, best_gap = 0, float("inf")
    for index, band in enumerate(bands):
        gap = abs(band[key] - value)
        if gap < best_gap:
            best, best_gap = index, gap
    return best


def _cluster(values: list[float], tolerance: float) -> list[float]:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return []
    groups: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


# ----------------------------------------------------------------------- fills
def _fills(source: PdfSource, page: PageData) -> Image.Image | None:
    try:
        return source.render(page.index, dpi=72)
    except Exception:
        return None


def _sample_fill(image: Image.Image, boundaries: list[float], bands: list[dict], row: int, col: int) -> str | None:
    scale = image.width / max(page_width_for(image, boundaries), 1.0)
    left = int((boundaries[col] + 1) * scale)
    top = int((bands[row]["top"] + 1) * scale)
    right = min(image.width, int(boundaries[col + 1] * scale))
    bottom = min(image.height, int((bands[row]["bottom"] + 1) * scale))
    if right - left < 1 or bottom - top < 1:
        return None
    box = image.crop((left, top, min(right, left + 6), min(bottom, top + 6)))
    pixels = list(box.getdata())
    if not pixels:
        return None
    red = sum(pixel[0] for pixel in pixels) / len(pixels)
    green = sum(pixel[1] for pixel in pixels) / len(pixels)
    blue = sum(pixel[2] for pixel in pixels) / len(pixels)
    if min(red, green, blue) >= 246:
        return None
    return f"{int(red):02X}{int(green):02X}{int(blue):02X}"


def page_width_for(image: Image.Image, boundaries: list[float]) -> float:
    return boundaries[-1] if boundaries else float(image.width)


# ---------------------------------------------------------------------- images
def _images(source: PdfSource, page: PageData, boundaries: list[float], bands: list[dict]) -> list[GridImage]:
    pieces: list[GridImage] = []
    for bbox in page.images[:MAX_IMAGES]:
        x0, top, x1, bottom = bbox
        if (x1 - x0) < 12 or (bottom - top) < 12:
            continue
        try:
            crop = source.crop(page.index, (x0, top, x1, bottom), dpi=130)
        except Exception:
            continue
        width = max(20, int((x1 - x0) * 96 / 72))
        height = max(20, int((bottom - top) * 96 / 72))
        if width > 900 or height > 900:
            shrink = min(900 / width, 900 / height)
            width, height = int(width * shrink), int(height * shrink)
            crop.thumbnail((width, height))
        buffer = io.BytesIO()
        # Level 3 is smaller and several times quicker than zlib's filter search.
        crop.save(buffer, format="PNG", compress_level=3)
        pieces.append(
            GridImage(
                row=_index_for(bands, (top + bottom) / 2),
                col=min(_column_index(boundaries, x0), max(len(boundaries) - 2, 0)),
                data=buffer.getvalue(),
                width=width,
                height=height,
            )
        )
    return pieces


# --------------------------------------------------------------------- helpers
def _excel_font_size(word: Word, median_height: float) -> float:
    if not word.size:
        return 10.0
    ratio = word.size / max(median_height, 0.1)
    return float(min(18, max(8, round(9.5 * ratio))))


def _text_fallback(page: PageData, name: str) -> SheetLayout:
    layout = SheetLayout(name=name, rows=1, cols=1)
    lines = page.lines or ([page.text] if page.text else [])
    layout.col_widths[0] = 120.0
    for row, line in enumerate(lines[:MAX_ROWS]):
        layout.cells[(row, 0)] = GridCell(value=line, size=10.0)
        layout.row_heights[row] = 15.0
        layout.rows = row + 1
    return layout
