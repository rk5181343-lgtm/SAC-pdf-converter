"""Tool registry.

One place lists every tool the workspace offers: its group, what it accepts, and
the options the UI should render. The UI builds itself from
``GET /api/tools``, so adding a tool here is enough to expose it.

Handlers live in :mod:`app.tools.pdf_ops`; PDF → Excel is not listed as a handler
because it runs through the conversion pipeline (see ``app.excel_runner``).
"""
from __future__ import annotations

TOOLS: tuple[dict, ...] = (
    {
        "id": "pdf-to-excel",
        "label": "PDF → Excel",
        "group": "Convert",
        "summary": "Workbooks with Original Layout, Clean Data and Validation sheets.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "",
        "options": (
            {"name": "combine", "label": "Also build one combined workbook", "type": "bool", "default": True},
            {"name": "ocr", "label": "Scanned pages", "type": "select", "default": "auto",
             "choices": ("auto", "always", "never"),
             "help": "auto reads scanned pages with OCR only when there is no text layer."},
            {"name": "layout", "label": "Original Layout sheet", "type": "bool", "default": True},
            {"name": "images", "label": "Keep embedded images", "type": "bool", "default": True},
            {"name": "fills", "label": "Keep cell background colours", "type": "bool", "default": True},
        ),
    },
    {
        "id": "images-to-pdf",
        "label": "Images → PDF",
        "group": "Convert",
        "summary": "JPG, PNG or WebP scans into one PDF, one image per page.",
        "accepts": "images",
        "multiple": True,
        "handler": "images_to_pdf",
        "options": (
            {"name": "size", "label": "Page size", "type": "select", "default": "A4",
             "choices": ("A4", "A3", "A5", "Letter", "Legal", "auto")},
            {"name": "margin", "label": "Margin (points)", "type": "number", "default": 18, "min": 0, "max": 72},
        ),
    },
    {
        "id": "pdf-to-images",
        "label": "PDF → Images",
        "group": "Convert",
        "summary": "Every page as PNG or JPG; multi-page results arrive as a ZIP.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "pdf_to_images",
        "options": (
            {"name": "dpi", "label": "Resolution", "type": "select", "default": "150",
             "choices": ("96", "120", "150", "200", "300")},
            {"name": "format", "label": "Image format", "type": "select", "default": "png", "choices": ("png", "jpg")},
        ),
    },
    {
        "id": "ocr-pdf",
        "label": "Make searchable (OCR)",
        "group": "Convert",
        "summary": "Adds an invisible OCR text layer to scanned pages.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "ocr_pdf",
        "options": (
            {"name": "dpi", "label": "OCR resolution", "type": "select", "default": "200",
             "choices": ("150", "200", "300")},
        ),
    },
    {
        "id": "pdf-to-word",
        "label": "PDF → Word",
        "group": "Convert",
        "summary": "Text-level .docx export for editing copy.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "pdf_to_word",
        "options": (
            {"name": "page_breaks", "label": "Page break per PDF page", "type": "bool", "default": True},
        ),
    },
    {
        "id": "pdf-to-powerpoint",
        "label": "PDF → PowerPoint",
        "group": "Convert",
        "summary": "One slide per page, each page as a full-slide picture.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "pdf_to_powerpoint",
        "options": (
            {"name": "dpi", "label": "Picture resolution", "type": "select", "default": "150",
             "choices": ("120", "150", "200")},
        ),
    },
    {
        "id": "extract-images",
        "label": "Extract images",
        "group": "Convert",
        "summary": "Every picture embedded in the PDF, as PNG or JPG in a ZIP.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "extract_images",
        "options": (
            {"name": "format", "label": "Image format", "type": "select", "default": "png", "choices": ("png", "jpg")},
            {"name": "min_size", "label": "Ignore images narrower than (px)", "type": "number", "default": 40,
             "min": 8, "max": 400},
        ),
    },
    {
        "id": "optimize-scans",
        "label": "Optimize scans",
        "group": "Convert",
        "summary": "Straighten crooked pages, clean the paper, flatten to black & white.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "optimize_scans",
        "options": (
            {"name": "mode", "label": "Output", "type": "select", "default": "balanced",
             "choices": ("balanced", "grayscale", "black-and-white")},
            {"name": "straighten", "label": "Straighten pages", "type": "bool", "default": True},
            {"name": "invert", "label": "Invert (white on black → black on white)", "type": "bool", "default": False},
        ),
    },
    {
        "id": "merge-pdf",
        "label": "Merge PDFs",
        "group": "Organise",
        "summary": "Join several PDFs; the order in the file list is the order in the result.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "merge_pdf",
        "options": (
            {"name": "order", "label": "Order (e.g. 3,1,2 — blank keeps the file list order)",
             "type": "text", "default": ""},
        ),
    },
    {
        "id": "split-pdf",
        "label": "Split / Extract pages",
        "group": "Organise",
        "summary": "Page ranges (1-3,5,8-) or one PDF per page.",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "split_pdf",
        "options": (
            {"name": "mode", "label": "Mode", "type": "select", "default": "ranges", "choices": ("ranges", "each")},
            {"name": "pages", "label": "Pages", "type": "text", "default": "1-", "help": "Separate groups with commas."},
        ),
    },
    {
        "id": "reorder-pages",
        "label": "Reorder pages",
        "group": "Organise",
        "summary": "New order (3,1,2), reverse (5-1), repeat, drop pages or insert blanks (0).",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "reorder_pages",
        "options": (
            {"name": "order", "label": "New order", "type": "text", "default": "",
             "help": "1-based page numbers; 5-1 reverses; 0 inserts a blank page."},
        ),
    },
    {
        "id": "rotate-pages",
        "label": "Rotate pages",
        "group": "Organise",
        "summary": "90°, 180° or 270° on all or selected pages.",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "rotate_pages",
        "options": (
            {"name": "angle", "label": "Rotation", "type": "select", "default": "90", "choices": ("90", "180", "270")},
            {"name": "pages", "label": "Pages", "type": "text", "default": ""},
        ),
    },
    {
        "id": "compress-pdf",
        "label": "Compress PDF",
        "group": "Organise",
        "summary": "Re-render at a lower resolution with JPEG quality control.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "compress_pdf",
        "options": (
            {"name": "dpi", "label": "Resolution", "type": "select", "default": "120", "choices": ("96", "120", "150")},
            {"name": "quality", "label": "JPEG quality", "type": "number", "default": 70, "min": 30, "max": 95},
            {"name": "grayscale", "label": "Grayscale", "type": "bool", "default": False},
        ),
    },
    {
        "id": "pages-per-sheet",
        "label": "Pages per sheet",
        "group": "Layout",
        "summary": "2/3/4/6/8/9-up with optional borders, plus booklet imposition.",
        "accepts": "pdf",
        "multiple": False,
        "handler": "pages_per_sheet",
        "options": (
            {"name": "per_sheet", "label": "Pages per sheet", "type": "select", "default": "2",
             "choices": ("2", "3", "4", "6", "8", "9")},
            {"name": "size", "label": "Sheet size", "type": "select", "default": "A4",
             "choices": ("A4", "A3", "Letter", "Legal")},
            {"name": "borders", "label": "Draw cell borders", "type": "bool", "default": True},
            {"name": "booklet", "label": "Booklet imposition", "type": "bool", "default": False},
        ),
    },
    {
        "id": "change-page-size",
        "label": "Change page size",
        "group": "Layout",
        "summary": "Fit onto A4/A3/A5/Letter/Legal, flip orientation, add margins.",
        "accepts": "pdf",
        "multiple": False,
        "handler": "change_page_size",
        "options": (
            {"name": "size", "label": "Page size", "type": "select", "default": "A4",
             "choices": ("A4", "A3", "A5", "Letter", "Legal")},
            {"name": "orientation", "label": "Orientation", "type": "select", "default": "portrait",
             "choices": ("portrait", "landscape")},
            {"name": "margin", "label": "Margin (points)", "type": "number", "default": 18, "min": 0, "max": 90},
        ),
    },
    {
        "id": "split-sheets",
        "label": "Split multi-page sheets",
        "group": "Layout",
        "summary": "Cut 2-up, 3-up or 4-up sheets back into one PDF page per part.",
        "accepts": "pdf",
        "multiple": False,
        "handler": "split_sheets",
        "options": (
            {"name": "parts", "label": "Parts per sheet", "type": "select", "default": "2", "choices": ("2", "3", "4")},
            {"name": "direction", "label": "Cut", "type": "select", "default": "vertical",
             "choices": ("vertical", "horizontal", "grid")},
        ),
    },
    {
        "id": "crop-margins",
        "label": "Crop margins",
        "group": "Layout",
        "summary": "Trim scanner edges or white borders by percentage.",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "crop_margins",
        "options": (
            {"name": "top", "label": "Top %", "type": "number", "default": 2, "min": 0, "max": 40},
            {"name": "bottom", "label": "Bottom %", "type": "number", "default": 2, "min": 0, "max": 40},
            {"name": "left", "label": "Left %", "type": "number", "default": 2, "min": 0, "max": 40},
            {"name": "right", "label": "Right %", "type": "number", "default": 2, "min": 0, "max": 40},
            {"name": "pages", "label": "Pages", "type": "text", "default": ""},
        ),
    },
    {
        "id": "header-footer",
        "label": "Header / footer",
        "group": "Layout",
        "summary": "Header/footer text and page numbers with (x) (y) (file) placeholders.",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "header_footer",
        "options": (
            {"name": "header", "label": "Header text", "type": "text", "default": ""},
            {"name": "footer", "label": "Footer text", "type": "text", "default": ""},
            {"name": "page_numbers", "label": "Add “Page X of Y”", "type": "bool", "default": True},
            {"name": "start_number", "label": "First page number", "type": "number", "default": 1, "min": 1, "max": 9999},
            {"name": "position", "label": "Alignment", "type": "select", "default": "center",
             "choices": ("left", "center", "right")},
            {"name": "font_size", "label": "Font size", "type": "number", "default": 9, "min": 6, "max": 18},
            {"name": "pages", "label": "Pages", "type": "text", "default": ""},
        ),
    },
    {
        "id": "watermark",
        "label": "Watermark",
        "group": "Protect",
        "summary": "Translucent text once or tiled, such as “DRAFT” or the client name.",
        "accepts": "pdf",
        "multiple": False,
        "page_ranges": True,
        "handler": "watermark",
        "options": (
            {"name": "text", "label": "Watermark text", "type": "text", "default": "DRAFT"},
            {"name": "opacity", "label": "Opacity %", "type": "number", "default": 12, "min": 2, "max": 60},
            {"name": "tiled", "label": "Tile across the page", "type": "bool", "default": False},
            {"name": "angle", "label": "Rotation", "type": "number", "default": 45, "min": 0, "max": 90},
            {"name": "pages", "label": "Pages", "type": "text", "default": ""},
        ),
    },
    {
        "id": "protect-pdf",
        "label": "Protect PDF",
        "group": "Protect",
        "summary": "Adds a password and print, copy and edit permissions.",
        "accepts": "pdf",
        "multiple": True,
        "handler": "protect_pdf",
        "options": (
            {"name": "password", "label": "Open password", "type": "password", "default": ""},
            {"name": "owner_password", "label": "Owner password (optional)", "type": "password", "default": ""},
            {"name": "allow_printing", "label": "Allow printing", "type": "bool", "default": True},
            {"name": "allow_copying", "label": "Allow copying", "type": "bool", "default": False},
            {"name": "allow_editing", "label": "Allow editing", "type": "bool", "default": False},
        ),
    },
)

TOOL_INDEX = {tool["id"]: tool for tool in TOOLS}


def all_tools() -> list[dict]:
    return [_public(tool) for tool in TOOLS]


def get_tool(tool_id: str) -> dict:
    tool = TOOL_INDEX.get(tool_id)
    if not tool:
        raise KeyError(tool_id)
    return tool


def tool_or_none(tool_id: str) -> dict | None:
    return TOOL_INDEX.get(tool_id)


def run_tool(tool_id: str, files: list[dict], options: dict | None = None) -> list[dict]:
    tool = get_tool(tool_id)
    if not tool.get("handler"):
        raise ValueError(f"{tool['label']} runs through the conversion pipeline, not a PDF operation.")
    from app.tools import pdf_ops

    handler = getattr(pdf_ops, tool["handler"], None)
    if handler is None:  # pragma: no cover - registry/handler mismatch
        raise ValueError(f"{tool['label']} is not implemented.")
    return handler(files, options or {})


def groups() -> list[str]:
    seen: list[str] = []
    for tool in TOOLS:
        if tool["group"] not in seen:
            seen.append(tool["group"])
    return seen


def _public(tool: dict) -> dict:
    return {
        "id": tool["id"],
        "label": tool["label"],
        "group": tool["group"],
        "summary": tool["summary"],
        "accepts": tool.get("accepts", "pdf"),
        "multiple": tool.get("multiple", True),
        "page_ranges": tool.get("page_ranges", False),
        "options": [
            {
                "name": option["name"],
                "label": option["label"],
                "type": option.get("type", "text"),
                "default": option.get("default"),
                "choices": list(option.get("choices", ())),
                "min": option.get("min"),
                "max": option.get("max"),
                "help": option.get("help", ""),
            }
            for option in tool.get("options", ())
        ],
    }
