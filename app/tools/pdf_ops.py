"""Everyday PDF operations: merge, split, rotate, crop, watermark and friends.

All handlers share one shape::

    handler(files, options) -> [{"filename", "data", "mimetype", "meta"}]

``files`` is a list of ``{"filename", "data", "mimetype"}``. Pages are rendered
with pdfium and re-assembled with reportlab whenever a layout changes; page-level
edits that PDF supports natively (rotate, crop box, encrypt, merge) go through
pypdf so the text layer and links survive.
"""
from __future__ import annotations

import io
import re
import zipfile
from math import ceil
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

PAGE_SIZES = {
    "A4": (595.28, 841.89),
    "A3": (841.89, 1190.55),
    "A5": (419.53, 595.28),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}

IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


class PdfPasswordRequired(ValueError):
    """A locked PDF needs its password before any tool can read it.

    The ``code`` travels with the error so a job can tell the browser to ask for
    the password instead of leaving the user at a dead end.
    """

    code = "password_required"

    def __init__(self, filename: str = "This PDF", wrong: bool = False) -> None:
        self.filename = filename or "This PDF"
        self.wrong = wrong
        if wrong:
            message = (
                f"{self.filename}: that password did not open the file. "
                "Check it, or try the owner password."
            )
        else:
            message = f"{self.filename} is password protected. Enter its password to continue."
        super().__init__(message)


# --------------------------------------------------------------------- helpers
def _num(options: dict, name: str, default: float) -> float:
    try:
        return float(options.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(options: dict, name: str, default: int) -> int:
    return int(round(_num(options, name, default)))


def _bool(options: dict, name: str, default: bool = False) -> bool:
    value = options.get(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _text(options: dict, name: str, default: str = "") -> str:
    value = options.get(name, default)
    return str(value if value is not None else default)


def _stem(filename: str) -> str:
    name = Path(str(filename or "document")).name
    return (name.rsplit(".", 1)[0] if "." in name else name)[:80] or "document"


def _out(filename: str, data: bytes, mimetype: str = "application/pdf", meta: dict | None = None) -> dict:
    return {"filename": filename, "data": data, "mimetype": mimetype, "meta": meta or {}}


def _pdfs(files: list[dict]) -> list[dict]:
    selected = [file for file in files if (file.get("mimetype") or "").endswith("pdf") or str(file.get("filename", "")).lower().endswith(".pdf")]
    if not selected:
        raise ValueError("This tool needs at least one PDF.")
    return selected


def _open_pdfium(data: bytes):
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(data)


def _render(doc, index: int, dpi: float = 150.0) -> Image.Image:
    return doc[index].render(scale=float(dpi) / 72.0).to_pil().convert("RGB")


def _png_bytes(image: Image.Image, level: int = 3) -> bytes:
    """PNG bytes for a rendered page.

    ``optimize=True`` makes zlib try every filter and costs more than the render
    itself: measured at 278 ms for a 150 dpi page against 40 ms here, and the
    faster setting even came out smaller. ``level=1`` is for images that are only
    embedded into a PDF, where the writer compresses the pixels again anyway.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=max(1, min(int(level), 9)))
    return buffer.getvalue()


def _jpg_bytes(image: Image.Image, quality: int = 80) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=int(quality))
    return buffer.getvalue()


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _page_size(name: str, orientation: str = "portrait") -> tuple[float, float]:
    width, height = PAGE_SIZES.get(str(name), PAGE_SIZES["A4"])
    if str(orientation).lower().startswith("land"):
        return (height, width)
    return (width, height)


def _flatten(image: Image.Image) -> Image.Image:
    """EXIF-orient the picture and put any transparency on white paper."""
    oriented = ImageOps.exif_transpose(image)
    if oriented.mode in {"RGBA", "LA", "P"}:
        rgba = oriented.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return oriented.convert("RGB")


def _fit(image: Image.Image, box_width: float, box_height: float) -> tuple[float, float]:
    if image.width <= 0 or image.height <= 0:
        return box_width, box_height
    scale = min(box_width / image.width, box_height / image.height)
    return image.width * scale, image.height * scale


def _draw_fitted(c, image: Image.Image, page_width: float, page_height: float, margin: float = 0.0) -> None:
    width, height = _fit(image, max(page_width - 2 * margin, 1), max(page_height - 2 * margin, 1))
    x = (page_width - width) / 2
    y = (page_height - height) / 2
    # The PDF writer re-compresses the pixels itself, so the PNG here is only a
    # transport: the fastest lossless setting is the right one.
    c.drawImage(ImageReader(io.BytesIO(_png_bytes(image, level=1))), x, y, width=width, height=height)


def _draw_embedded(c, data: bytes, page_width: float, page_height: float, margin: float = 0.0) -> None:
    """Place already-compressed bytes on the page, copied into the PDF as they are.

    A JPEG stays a JPEG: reportlab writes the original stream with a DCT filter
    instead of decoding it, re-encoding it and inflating it back to raw pixels.
    """
    reader = ImageReader(io.BytesIO(data))
    pixels_w, pixels_h = reader.getSize()
    if not pixels_w or not pixels_h:
        raise ValueError("That image could not be read.")
    width, height = _fit_pixels(pixels_w, pixels_h, max(page_width - 2 * margin, 1), max(page_height - 2 * margin, 1))
    c.drawImage(reader, (page_width - width) / 2, (page_height - height) / 2, width=width, height=height)


def _fit_pixels(width: int, height: int, box_width: float, box_height: float) -> tuple[float, float]:
    scale = min(box_width / max(width, 1), box_height / max(height, 1))
    return width * scale, height * scale


def _assemble(items: list[dict], default_size: tuple[float, float] | None = None) -> bytes:
    """Build a PDF from ``{"image": PIL, "size": (w, h), "draw": fn(c, ...)}`` items."""
    buffer = io.BytesIO()
    first_size = items[0]["size"] if items and items[0].get("size") else (default_size or PAGE_SIZES["A4"])
    c = rl_canvas.Canvas(buffer, pagesize=first_size)
    for item in items:
        size = item.get("size") or first_size
        c.setPageSize(size)
        if item.get("draw"):
            item["draw"](c, size[0], size[1])
        elif item.get("jpeg"):
            _draw_embedded(c, item["jpeg"], size[0], size[1], item.get("margin", 0.0))
        else:
            _draw_fitted(c, item["image"], size[0], size[1], item.get("margin", 0.0))
        c.showPage()
    c.save()
    return buffer.getvalue()


def _ranges(spec: str, page_count: int) -> list[list[int]]:
    """Parse ``1-3,5,8-`` into 0-based page groups (one group per comma part)."""
    groups: list[list[int]] = []
    for part in str(spec or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        pages: list[int] = []
        for token in part.split():
            if re.fullmatch(r"\d+-\d+", token):
                start, end = (int(value) for value in token.split("-"))
                step = 1 if end >= start else -1
                pages.extend(range(start, end + step, step))
            elif re.fullmatch(r"\d+-", token):
                pages.extend(range(int(token[:-1]), page_count + 1))
            elif re.fullmatch(r"-\d+", token):
                pages.extend(range(1, int(token[1:]) + 1))
            elif token.isdigit():
                pages.append(int(token))
        groups.append([page - 1 for page in pages if 1 <= page <= page_count])
    if not groups:
        groups = [list(range(page_count))]
    return [group for group in groups if group]


def _order(spec: str, page_count: int) -> list[int | None]:
    """Parse a page order. ``0`` becomes a blank page; unknown tokens are ignored."""
    tokens = [token for token in re.split(r"[,\s]+", str(spec or "").strip()) if token]
    if not tokens:
        return list(range(page_count))
    order: list[int | None] = []
    for token in tokens:
        if token == "0":
            order.append(None)
        elif re.fullmatch(r"\d+-\d+", token):
            start, end = (int(value) for value in token.split("-"))
            step = 1 if end >= start else -1
            order.extend(page - 1 for page in range(start, end + step, step) if 1 <= page <= page_count)
        elif token.isdigit():
            page = int(token)
            if 1 <= page <= page_count:
                order.append(page - 1)
    return order or list(range(page_count))


def _booklet_order(page_count: int, per_sheet: int = 2) -> list[int | None]:
    """Saddle-stitch order: first and last page side by side, working inwards."""
    sheet_pages = max(per_sheet, 2)
    padded = int(ceil(page_count / sheet_pages) * sheet_pages)
    order: list[int | None] = []
    left, right = 0, padded - 1
    while left < right:
        order.append(left if left < page_count else None)
        order.append(right if right < page_count else None)
        left += 1
        right -= 1
    return order


def _pdf_page(reader: PdfReader, index: int):
    return reader.pages[index]


def _merge_overlay(page, overlay_reader: PdfReader) -> None:
    page.merge_page(overlay_reader.pages[0])


def _overlay_reader(overlay_bytes: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(overlay_bytes))


def _write_bytes(writer: PdfWriter) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------- convert
def images_to_pdf(files: list[dict], options: dict) -> list[dict]:
    images: list[Image.Image] = []
    for file in files:
        try:
            image = Image.open(io.BytesIO(file["data"]))
        except Exception as exc:
            raise ValueError(f"{file.get('filename')}: not an image ({exc}).") from exc
        images.append(_flatten(image))
    if not images:
        raise ValueError("Choose at least one image.")

    requested = _text(options, "size", "A4")
    margin = _num(options, "margin", 18)
    page_size = PAGE_SIZES.get(requested)
    items = []
    for image in images:
        size = page_size or ((image.width * 72 / 96) + 2 * margin, (image.height * 72 / 96) + 2 * margin)
        items.append({"image": image, "size": size, "margin": margin})
    return [_out(f"{_stem(files[0]['filename'])}-images.pdf", _assemble(items), meta={"pages": len(items)})]


def pdf_to_images(files: list[dict], options: dict) -> list[dict]:
    dpi = _int(options, "dpi", 150)
    fmt = _text(options, "format", "png").lower()
    outputs = []
    for file in _pdfs(files):
        doc = _open_pdfium(file["data"])
        stem = _stem(file["filename"])
        entries: list[tuple[str, bytes]] = []
        pages = []
        for index in range(len(doc)):
            image = _render(doc, index, dpi)
            data = _jpg_bytes(image, 88) if fmt == "jpg" else _png_bytes(image)
            name = f"{stem}-page-{index + 1:03d}.{fmt}"
            entries.append((name, data))
            pages.append((name, data))
        if len(pages) == 1:
            name, data = pages[0]
            outputs.append(_out(name, data, IMAGE_MIME.get(fmt, "image/png"), {"page": 1}))
        else:
            outputs.append(_out(f"{stem}-pages.zip", _zip(entries), "application/zip", {"pages": len(pages)}))
    return outputs


def ocr_pdf(files: list[dict], options: dict) -> list[dict]:
    from app.converter.pdf_source import ocr_available, ocr_image

    if not ocr_available():
        raise ValueError(
            "The OCR engine is not installed on this deployment. "
            "Install rapidocr-onnxruntime (see requirements.txt) or run the tool locally."
        )
    dpi = _int(options, "dpi", 200)
    outputs = []
    for file in _pdfs(files):
        doc = _open_pdfium(file["data"])
        reader = PdfReader(io.BytesIO(file["data"]))
        items = []
        recognised = 0
        for index in range(len(doc)):
            image = _render(doc, index, dpi)
            page = reader.pages[index]
            size = (float(page.mediabox.width), float(page.mediabox.height))
            words = ocr_image(image, dpi)
            recognised += len(words)

            def draw(c, width, height, _image=image, _words=words, _size=size):
                c.drawImage(ImageReader(io.BytesIO(_png_bytes(_image, level=1))), 0, 0, width=width, height=height)
                c.setFillColorRGB(0, 0, 0)
                for word in _words:
                    font_size = max(4.0, min(word.height * 0.95, 24))
                    try:
                        c.setFont("Helvetica", font_size)
                        c.setTextRenderMode(3)  # invisible: selectable, not printed
                        c.drawString(word.x0, height - word.bottom, word.text)
                    except Exception:
                        continue

            items.append({"image": image, "size": size, "draw": draw})
        name = f"{_stem(file['filename'])}-searchable.pdf"
        outputs.append(_out(name, _assemble(items), meta={"words": recognised}))
    return outputs


def pdf_to_word(files: list[dict], options: dict) -> list[dict]:
    from docx import Document

    page_breaks = _bool(options, "page_breaks", True)
    outputs = []
    for file in _pdfs(files):
        document = Document()
        reader = PdfReader(io.BytesIO(file["data"]))
        document.add_heading(_stem(file["filename"]), level=1)
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if not text.strip():
                document.add_paragraph(f"Page {index} has no text layer. Run “Make searchable (OCR)” first.")
            else:
                for line in text.splitlines():
                    if line.strip():
                        document.add_paragraph(line.strip())
            if page_breaks and index < len(reader.pages):
                document.add_page_break()
        buffer = io.BytesIO()
        document.save(buffer)
        outputs.append(_out(f"{_stem(file['filename'])}.docx", buffer.getvalue(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            {"pages": len(reader.pages)}))
    return outputs


def pdf_to_powerpoint(files: list[dict], options: dict) -> list[dict]:
    from pptx import Presentation
    from pptx.util import Emu

    dpi = _int(options, "dpi", 150)
    outputs = []
    for file in _pdfs(files):
        doc = _open_pdfium(file["data"])
        presentation = Presentation()
        presentation.slide_width = Emu(int(13.333 * 914400))
        presentation.slide_height = Emu(int(7.5 * 914400))
        blank = presentation.slide_layouts[6]
        for index in range(len(doc)):
            image = _render(doc, index, dpi)
            slide = presentation.slides.add_slide(blank)
            width, height = _fit(image, presentation.slide_width, presentation.slide_height)
            left = int((presentation.slide_width - width) / 2)
            top = int((presentation.slide_height - height) / 2)
            slide.shapes.add_picture(io.BytesIO(_png_bytes(image)), left, top, int(width), int(height))
        buffer = io.BytesIO()
        presentation.save(buffer)
        outputs.append(_out(f"{_stem(file['filename'])}.pptx", buffer.getvalue(),
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            {"slides": len(doc)}))
    return outputs


def extract_images(files: list[dict], options: dict) -> list[dict]:
    from app.converter.pdf_source import PdfSource

    fmt = _text(options, "format", "png").lower()
    min_size = _int(options, "min_size", 40)
    outputs = []
    for file in _pdfs(files):
        source = PdfSource(file["data"], ocr_mode="never")
        stem = _stem(file["filename"])
        entries: list[tuple[str, bytes]] = []
        try:
            for page in source.pages():
                for order, (x0, top, x1, bottom) in enumerate(page.images, start=1):
                    if (x1 - x0) < min_size * 72 / 200 or (bottom - top) < min_size * 72 / 200:
                        continue
                    try:
                        crop = source.crop(page.index, (x0, top, x1, bottom), dpi=200)
                    except Exception:
                        continue
                    data = _jpg_bytes(crop, 90) if fmt == "jpg" else _png_bytes(crop)
                    entries.append((f"{stem}-p{page.index + 1}-{order}.{fmt}", data))
        finally:
            source.close()
        if not entries:
            raise ValueError(f"{file['filename']}: no embedded images were found above {min_size}px.")
        outputs.append(_out(f"{stem}-images.zip", _zip(entries), "application/zip", {"images": len(entries)}))
    return outputs


def optimize_scans(files: list[dict], options: dict) -> list[dict]:
    mode = _text(options, "mode", "balanced").lower()
    straighten = _bool(options, "straighten", True)
    invert = _bool(options, "invert", False)
    outputs = []
    for file in _pdfs(files):
        doc = _open_pdfium(file["data"])
        reader = PdfReader(io.BytesIO(file["data"]))
        items = []
        for index in range(len(doc)):
            image = _render(doc, index, 200)
            if straighten:
                image = _straighten(image)
            grey = image.convert("L")
            if mode == "black-and-white":
                grey = ImageOps.autocontrast(grey, cutoff=2).point(lambda value: 255 if value > 165 else 0)
                image = grey.convert("RGB")
            elif mode == "grayscale":
                image = ImageOps.autocontrast(grey, cutoff=1).convert("RGB")
            else:
                image = ImageOps.autocontrast(grey, cutoff=1).convert("RGB")
            if invert:
                image = ImageOps.invert(image)
            page = reader.pages[index]
            items.append({"image": image, "size": (float(page.mediabox.width), float(page.mediabox.height))})
        outputs.append(_out(f"{_stem(file['filename'])}-optimized.pdf", _assemble(items), meta={"pages": len(items)}))
    return outputs


def _straighten(image: Image.Image) -> Image.Image:
    """Rotate mildly crooked scans upright using row-variance projection."""
    try:
        import numpy as np
    except ModuleNotFoundError:
        return image
    try:
        grey = image.convert("L")
        small = grey.resize((max(grey.width // 3, 40), max(grey.height // 3, 40)))
        array = np.asarray(small, dtype=np.uint8)
        best_angle, best_score = 0.0, -1.0
        for angle in (-4.0, -3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
            rotated = small.rotate(angle, resample=Image.BILINEAR, fillcolor=255) if angle else small
            data = np.asarray(rotated, dtype=np.uint8)
            score = float(np.var(np.sum(255 - data, axis=1)))
            if score > best_score:
                best_angle, best_score = angle, score
        if abs(best_angle) < 0.4:
            return image
        return image.rotate(best_angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255), expand=False)
    except Exception:
        return image


# -------------------------------------------------------------------- organise
def merge_pdf(files: list[dict], options: dict) -> list[dict]:
    pdfs = _pdfs(files)
    spec = _text(options, "order", "")
    order = [index for index in (int(token) - 1 for token in re.split(r"[,\s]+", spec) if token.isdigit()) if 0 <= index < len(pdfs)]
    ordered = [pdfs[index] for index in order] if order else pdfs

    writer = PdfWriter()
    for file in ordered:
        reader = PdfReader(io.BytesIO(file["data"]))
        for page in reader.pages:
            writer.add_page(page)
    return [_out("merged.pdf", _write_bytes(writer), meta={"files": len(ordered)})]


def split_pdf(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    stem = _stem(file["filename"])
    mode = _text(options, "mode", "ranges").lower()

    if mode == "each":
        entries = []
        for index in range(len(reader.pages)):
            writer = PdfWriter()
            writer.add_page(reader.pages[index])
            entries.append((f"{stem}-page-{index + 1:03d}.pdf", _write_bytes(writer)))
        return [_out(f"{stem}-pages.zip", _zip(entries), "application/zip", {"files": len(entries)})]

    groups = _ranges(_text(options, "pages", "1-"), len(reader.pages))
    outputs = []
    for number, group in enumerate(groups, start=1):
        writer = PdfWriter()
        for index in group:
            writer.add_page(reader.pages[index])
        label = f"{stem}-part-{number}.pdf" if len(groups) > 1 else f"{stem}-extract.pdf"
        outputs.append(_out(label, _write_bytes(writer), meta={"pages": len(group)}))
    return outputs


def reorder_pages(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    order = _order(_text(options, "order", ""), len(reader.pages))
    writer = PdfWriter()
    first_box = reader.pages[0].mediabox
    for index in order:
        if index is None:
            writer.add_blank_page(width=float(first_box.width), height=float(first_box.height))
        else:
            writer.add_page(reader.pages[index])
    return [_out(f"{_stem(file['filename'])}-reordered.pdf", _write_bytes(writer), meta={"pages": len(order)})]


def rotate_pages(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    angle = _int(options, "angle", 90)
    spec = _text(options, "pages", "")
    selected = set(range(len(reader.pages))) if not spec.strip() else {
        index for group in _ranges(spec, len(reader.pages)) for index in group
    }
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index in selected and angle % 360:
            page.rotate(angle % 360)
        writer.add_page(page)
    return [_out(f"{_stem(file['filename'])}-rotated.pdf", _write_bytes(writer), meta={"pages": len(reader.pages)})]


def compress_pdf(files: list[dict], options: dict) -> list[dict]:
    dpi = _int(options, "dpi", 120)
    quality = _int(options, "quality", 70)
    grayscale = _bool(options, "grayscale", False)
    outputs = []
    for file in _pdfs(files):
        doc = _open_pdfium(file["data"])
        reader = PdfReader(io.BytesIO(file["data"]))
        items = []
        for index in range(len(doc)):
            image = _render(doc, index, dpi)
            if grayscale:
                image = image.convert("L").convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            page = reader.pages[index]
            # Hand the JPEG to the writer untouched: decoding it here only to have
            # reportlab encode it again inflated the file it was meant to shrink.
            items.append({"jpeg": buffer.getvalue(), "size": (float(page.mediabox.width), float(page.mediabox.height))})
        data = _assemble(items)
        outputs.append(_out(f"{_stem(file['filename'])}-compressed.pdf", data,
                            meta={"pages": len(items), "before": len(file["data"]), "after": len(data)}))
    return outputs


# ---------------------------------------------------------------------- layout
def pages_per_sheet(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    doc = _open_pdfium(file["data"])
    per_sheet = _int(options, "per_sheet", 2)
    per_sheet = per_sheet if per_sheet in (2, 3, 4, 6, 8, 9) else 2
    columns = {2: 2, 3: 3, 4: 2, 6: 3, 8: 4, 9: 3}[per_sheet]
    rows = {2: 1, 3: 1, 4: 2, 6: 2, 8: 2, 9: 3}[per_sheet]
    booklet = _bool(options, "booklet", False)
    borders = _bool(options, "borders", True)
    size = _page_size(_text(options, "size", "A4"), "landscape" if columns > rows else "portrait")

    order: list[int | None] = _booklet_order(len(doc), per_sheet) if booklet else list(range(len(doc)))
    if not booklet:
        while len(order) % per_sheet:
            order.append(None)

    items = []
    for start in range(0, len(order), per_sheet):
        group = order[start:start + per_sheet]

        def draw(c, width, height, _group=group):
            cell_width = width / columns
            cell_height = height / rows
            for position, index in enumerate(_group):
                col = position % columns
                row = position // columns
                x = col * cell_width
                y = height - (row + 1) * cell_height
                if index is not None:
                    image = _render(doc, index, 120)
                    fitted_width, fitted_height = _fit(image, cell_width * 0.96, cell_height * 0.96)
                    c.drawImage(
                        ImageReader(io.BytesIO(_png_bytes(image, level=1))),
                        x + (cell_width - fitted_width) / 2,
                        y + (cell_height - fitted_height) / 2,
                        width=fitted_width,
                        height=fitted_height,
                    )
                if borders:
                    c.setStrokeColorRGB(0.78, 0.8, 0.83)
                    c.setLineWidth(0.6)
                    c.rect(x, y, cell_width, cell_height)

        items.append({"image": None, "size": size, "draw": draw})
    label = "booklet.pdf" if booklet else f"{_stem(file['filename'])}-{per_sheet}up.pdf"
    return [_out(label, _assemble(items), meta={"pages": len(items)})]


def change_page_size(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    doc = _open_pdfium(file["data"])
    size = _page_size(_text(options, "size", "A4"), _text(options, "orientation", "portrait"))
    margin = _num(options, "margin", 18)
    items = [{"image": _render(doc, index, 150), "size": size, "margin": margin} for index in range(len(doc))]
    return [_out(f"{_stem(file['filename'])}-{_text(options, 'size', 'A4')}.pdf", _assemble(items), meta={"pages": len(items)})]


def split_sheets(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    doc = _open_pdfium(file["data"])
    reader = PdfReader(io.BytesIO(file["data"]))
    parts = _int(options, "parts", 2)
    parts = parts if parts in (2, 3, 4) else 2
    direction = _text(options, "direction", "vertical").lower()

    items = []
    for index in range(len(doc)):
        image = _render(doc, index, 200)
        page = reader.pages[index]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if direction == "grid":
            cols, rows = (2, parts // 2 if parts in (2, 4) else 2)
            cells = [(col, row) for row in range(rows) for col in range(cols)]
        elif direction == "horizontal":
            cols, rows = 1, parts
            cells = [(0, row) for row in range(rows)]
        else:
            cols, rows = parts, 1
            cells = [(col, 0) for col in range(cols)]

        cell_px_w = image.width / cols
        cell_px_h = image.height / rows
        for order, (col, row) in enumerate(cells, start=1):
            crop = image.crop((int(col * cell_px_w), int(row * cell_px_h),
                               int((col + 1) * cell_px_w), int((row + 1) * cell_px_h)))
            size = (width / cols, height / rows)
            items.append({"image": crop, "size": size})
    return [_out(f"{_stem(file['filename'])}-split.pdf", _assemble(items), meta={"pages": len(items)})]


def crop_margins(files: list[dict], options: dict) -> list[dict]:
    from pypdf.generic import RectangleObject

    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    spec = _text(options, "pages", "")
    selected = set(range(len(reader.pages))) if not spec.strip() else {
        index for group in _ranges(spec, len(reader.pages)) for index in group
    }
    top = _num(options, "top", 2) / 100
    bottom = _num(options, "bottom", 2) / 100
    left = _num(options, "left", 2) / 100
    right = _num(options, "right", 2) / 100
    if top + bottom >= 1 or left + right >= 1:
        raise ValueError("Those margins would remove the whole page.")

    for index, page in enumerate(reader.pages):
        if index not in selected:
            continue
        box = page.mediabox
        width = float(box.width)
        height = float(box.height)
        x0 = float(box.left) + width * left
        y0 = float(box.bottom) + height * bottom
        x1 = float(box.right) - width * right
        y1 = float(box.top) - height * top
        page.cropbox = RectangleObject((x0, y0, x1, y1))

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    return [_out(f"{_stem(file['filename'])}-cropped.pdf", _write_bytes(writer), meta={"pages": len(reader.pages)})]


def header_footer(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    total = len(reader.pages)
    start = _int(options, "start_number", 1)
    spec = _text(options, "pages", "")
    selected = set(range(total)) if not spec.strip() else {
        index for group in _ranges(spec, total) for index in group
    }
    header = _text(options, "header", "")
    footer = _text(options, "footer", "")
    page_numbers = _bool(options, "page_numbers", True)
    position = _text(options, "position", "center").lower()
    font_size = _num(options, "font_size", 9)

    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if index in selected and (header or footer or page_numbers):
            number = start + index
            overlay = _text_overlay(
                width, height, header, footer, number, total, position, font_size,
                file["filename"], page_numbers,
            )
            _merge_overlay(page, _overlay_reader(overlay))
        writer.add_page(page)
    return [_out(f"{_stem(file['filename'])}-stamped.pdf", _write_bytes(writer), meta={"pages": total})]


def _text_overlay(width, height, header: str, footer: str, number: int, total: int, position: str, font_size: float, filename: str, page_numbers: bool = True) -> bytes:
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(width, height))
    c.setFillColorRGB(0.18, 0.24, 0.31)
    page_label = f"Page {number} of {total}"
    substitutions = {"{x}": str(number), "(x)": str(number), "{y}": str(total), "(y)": str(total),
                     "{file}": _stem(filename), "(file)": _stem(filename), "{pages}": str(total)}
    header_text = _substitute(header, substitutions)
    footer_text = _substitute(footer, substitutions)
    margin = max(18.0, font_size * 2)

    def place(text: str, y: float) -> None:
        if not text:
            return
        text_width = c.stringWidth(text, "Helvetica", font_size)
        if position == "left":
            x = margin
        elif position == "right":
            x = width - margin - text_width
        else:
            x = (width - text_width) / 2
        c.setFont("Helvetica", font_size)
        c.drawString(x, y, text)

    place(header_text, height - margin)
    place(footer_text, margin - font_size / 2)
    if footer_text and page_numbers:
        place(page_label, margin - font_size / 2 - font_size - 2)
    elif page_numbers and not footer_text:
        place(page_label, margin - font_size / 2)
    c.showPage()
    c.save()
    return buffer.getvalue()


def _substitute(template: str, values: dict[str, str]) -> str:
    text = template or ""
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def watermark(files: list[dict], options: dict) -> list[dict]:
    file = _pdfs(files)[0]
    reader = PdfReader(io.BytesIO(file["data"]))
    text = _text(options, "text", "DRAFT") or "DRAFT"
    opacity = max(0.02, min(_num(options, "opacity", 12) / 100, 0.6))
    tiled = _bool(options, "tiled", False)
    angle = _num(options, "angle", 45)
    spec = _text(options, "pages", "")
    selected = set(range(len(reader.pages))) if not spec.strip() else {
        index for group in _ranges(spec, len(reader.pages)) for index in group
    }

    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index in selected:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            overlay = _watermark_overlay(width, height, text, opacity, tiled, angle)
            _merge_overlay(page, _overlay_reader(overlay))
        writer.add_page(page)
    return [_out(f"{_stem(file['filename'])}-watermarked.pdf", _write_bytes(writer), meta={"pages": len(reader.pages)})]


def _watermark_overlay(width: float, height: float, text: str, opacity: float, tiled: bool, angle: float) -> bytes:
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(width, height))
    c.setFillColorRGB(0.45, 0.5, 0.58)
    try:
        c.setFillAlpha(opacity)
    except Exception:  # pragma: no cover - very old reportlab
        pass

    if tiled:
        size = max(16.0, min(width, height) / 14)
        step_x = c.stringWidth(text, "Helvetica-Bold", size) * 1.8 + 24
        step_y = size * 4
        row = 0
        y = step_y / 2
        while y < height:
            offset = (step_x / 2) if row % 2 else 0
            x = -step_x + offset
            while x < width:
                c.saveState()
                c.translate(x, y)
                c.rotate(angle)
                c.setFont("Helvetica-Bold", size)
                c.drawString(0, 0, text)
                c.restoreState()
                x += step_x
            y += step_y
            row += 1
    else:
        size = max(24.0, min(width, height) / 6)
        c.saveState()
        c.translate(width / 2, height / 2)
        c.rotate(angle)
        c.setFont("Helvetica-Bold", size)
        c.drawCentredString(0, 0, text)
        c.restoreState()
    c.showPage()
    c.save()
    return buffer.getvalue()


# --------------------------------------------------------------------- protect
def unlock_bytes(data: bytes, password: str = "", filename: str = "This PDF") -> bytes:
    """Return PDF bytes any tool can read.

    An unencrypted file comes back untouched. A locked file is opened with
    ``password`` and rewritten without encryption, so nothing downstream has to
    know it was ever protected. A missing or wrong password raises
    :class:`PdfPasswordRequired`, which carries the code the UI prompts on.
    """
    reader = PdfReader(io.BytesIO(data))
    if not reader.is_encrypted:
        return data
    try:
        opened = reader.decrypt(password or "")
    except Exception as exc:  # malformed key, unsupported handler
        raise PdfPasswordRequired(filename, wrong=bool(password)) from exc
    if not opened:
        raise PdfPasswordRequired(filename, wrong=bool(password))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    return _write_bytes(writer)


def unlock_pdf(files: list[dict], options: dict) -> list[dict]:
    password = _text(options, "password", "")
    outputs = []
    for file in _pdfs(files):
        data = unlock_bytes(file["data"], password, file["filename"])
        pages = len(PdfReader(io.BytesIO(data)).pages)
        outputs.append(_out(f"{_stem(file['filename'])}-unlocked.pdf", data, meta={"pages": pages}))
    return outputs


def protect_pdf(files: list[dict], options: dict) -> list[dict]:
    from pypdf.constants import UserAccessPermissions as UAP

    password = _text(options, "password", "")
    owner = _text(options, "owner_password", "") or password
    if not password and not owner:
        raise ValueError("Set an open password (or an owner password) to protect the file.")

    flag = 0
    if _bool(options, "allow_printing", True):
        flag |= int(UAP.PRINT)
    if _bool(options, "allow_copying", False):
        flag |= int(UAP.EXTRACT)
    if _bool(options, "allow_editing", False):
        flag |= int(UAP.MODIFY)

    outputs = []
    for file in _pdfs(files):
        reader = PdfReader(io.BytesIO(file["data"]))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        try:
            writer.encrypt(user_password=password, owner_password=owner, permissions_flag=flag)
        except TypeError:  # older pypdf positional signature
            writer.encrypt(password, owner, flag)
        outputs.append(_out(f"{_stem(file['filename'])}-protected.pdf", _write_bytes(writer),
                            meta={"pages": len(reader.pages)}))
    return outputs
