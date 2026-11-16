"""Document-type detection, table extraction and clean records.

The goal of this module is one record per line of the source document, with one
field per column, and without touching the values themselves: a "1,234.50" stays
"1,234.50" in the sheet. Typing happens later, when the workbook is written.

Extraction runs page by page, so a statement that continues over several pages
keeps every page's rows and every record remembers the page it was read from.
"""
from __future__ import annotations

import bisect
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from app.converter.pdf_source import PageData, PdfSource

DOC_TYPES = ("bank_statement", "invoice", "ledger", "trial_balance", "generic")

# Points: a space between two words is about 2.5, a gutter between columns far more.
MIN_GUTTER = 4.0
DESCRIPTION_COLUMN = "Description"

DOC_LABELS = {
    "bank_statement": "Bank statement",
    "invoice": "Invoice",
    "ledger": "Ledger",
    "trial_balance": "Trial balance",
    "generic": "General document",
}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "bank_statement": (
        "bank statement",
        "statement of account",
        "account number",
        "opening balance",
        "closing balance",
        "withdrawals",
        "deposits",
        "balance brought forward",
        "balance carried forward",
    ),
    "invoice": (
        "tax invoice",
        "invoice no",
        "invoice number",
        "bill to",
        "due date",
        "subtotal",
        "sub-total",
        "vat",
        "qty",
        "unit price",
        "amount due",
    ),
    "ledger": (
        "ledger",
        "debit",
        "credit",
        "running balance",
        "narrative",
        "posting",
        "journal",
    ),
    "trial_balance": (
        "trial balance",
        "opening balance",
        "closing balance",
        "debit",
        "credit",
    ),
}

TOTAL_PATTERN = re.compile(
    r"^(total|totals?|sub-?total|grand total|transaction total|total (dr|cr)|"
    r"balance (brought|carried) forward|balance (c/f|b/f)|"
    r"opening balance|closing balance|carried forward|brought forward)\b",
    re.IGNORECASE,
)

# A column that only says whether the amount is a debit or a credit: Axis prints
# "Debit/Credit", ICICI "Cr/Dr". It is a direction flag, not an amount column.
DIRECTION_HEADER = re.compile(
    r"(debit|dr|withdrawal)\s*/\s*(credit|cr|deposit)|cr\s*/\s*dr|\bdrcr\b", re.IGNORECASE
)

CURRENCY_CHARS = "$€£¥₹₦₽₩"
DATE_PATTERNS = (
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("%d/%m/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%d-%m-%Y", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("%d.%m.%Y", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")),
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------- text helpers
def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00ad", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def looks_numeric(value: str) -> bool:
    return parse_number(value) is not None


def parse_number(value: object) -> float | None:
    """Read a printed amount, including ``1 234,56``, ``(1,234.50)`` and ``12.5-``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return None
    if text.lower() in {"n/a", "na", "-", "--", "—"}:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    if text.startswith("+"):
        text = text[1:]

    for char in CURRENCY_CHARS:
        text = text.replace(char, "")
    text = text.replace(" ", "").replace("\u2212", "-")
    text = re.sub(r"[A-Za-z%]", "", text)
    if not re.fullmatch(r"[\d.,]+", text or ""):
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif has_comma:
        parts = text.split(",")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) > 0):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    elif has_dot:
        parts = text.split(".")
        if len(parts) > 2:
            text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) != 3 else text.replace(".", "")
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            # "1.500" is a thousands separator far more often than a decimal.
            text = text.replace(".", "")

    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def date_order_for(samples: list[str]) -> str:
    """Pick day-first vs month-first from the sample values that disambiguate."""
    for sample in samples:
        text = clean_text(sample)
        match = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$", text)
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 and second <= 12:
            return "dmy"
        if second > 12 and first <= 12:
            return "mdy"
    return "dmy"


def parse_date(value: object, order: str = "dmy") -> date | None:
    text = clean_text(value)
    if not text:
        return None
    # ICICI prints "01/09/2026 07:55:29 AM" in one cell: the time is not part of
    # the date, and keeping it made the whole cell plain text.
    text = re.sub(r"\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?$", "", text).strip()
    if not text:
        return None
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    compact = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if compact:
        try:
            return date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
        except ValueError:
            return None

    numeric = re.match(r"^(\d{1,2})([/.\-])(\d{1,2})\2(\d{2,4})$", text)
    if numeric:
        first, second, year = int(numeric.group(1)), int(numeric.group(3)), int(numeric.group(4))
        if year < 100:
            year += 2000 if year < 70 else 1900
        day, month = (first, second) if order == "dmy" else (second, first)
        if day > 12 and month <= 12:
            day, month = first, second
        elif month > 12 and day <= 12:
            day, month = second, first
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # 12 Jan 2026 / 12-Jan-26
    named = re.match(r"^(\d{1,2})[\s\-/]*([A-Za-z]{3,9})[\s\-,/]*(\d{2,4})$", text)
    if named:
        month = MONTHS.get(named.group(2)[:4].lower()) or MONTHS.get(named.group(2)[:3].lower())
        if month:
            year = int(named.group(3))
            if year < 100:
                year += 2000 if year < 70 else 1900
            try:
                return date(year, month, int(named.group(1)))
            except ValueError:
                return None

    # Jan 12, 2026
    named_us = re.match(r"^([A-Za-z]{3,9})[\s\-/]*(\d{1,2})[\s\-,/]*(\d{2,4})$", text)
    if named_us:
        month = MONTHS.get(named_us.group(1)[:4].lower()) or MONTHS.get(named_us.group(1)[:3].lower())
        if month:
            year = int(named_us.group(3))
            if year < 100:
                year += 2000 if year < 70 else 1900
            try:
                return date(year, month, int(named_us.group(2)))
            except ValueError:
                return None
    return None


def as_excel_value(number: float) -> float | int:
    return int(number) if float(number).is_integer() else round(float(number), 6)


# ---------------------------------------------------------------------- result
@dataclass
class ExtractResult:
    doc_type: str
    confidence: float
    columns: list[str]
    records: list[dict]
    total_flags: list[bool]
    roles: dict[str, int | None]
    tables_found: int
    pages_total: int
    pages_with_text: int
    ocr_pages: int
    date_order: str = "dmy"
    record_pages: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def doc_label(self) -> str:
        return DOC_LABELS.get(self.doc_type, "General document")

    @property
    def row_count(self) -> int:
        return len(self.records)

    def numeric_values(self, role: str) -> list[float]:
        index = self.roles.get(role)
        if index is None:
            return []
        values = []
        for record in self.records:
            number = parse_number(record.get(self.columns[index]))
            if number is not None:
                values.append(number)
        return values


# --------------------------------------------------------------------- detect
STRONG_KEYWORDS = frozenset(
    {
        "bank statement",
        "statement of account",
        "tax invoice",
        "invoice number",
        "trial balance",
        "journal",
        "ledger",
    }
)


def detect_doc_type(filename: str, pages: list[PageData]) -> tuple[str, float]:
    haystack = " ".join([filename or ""] + [page.text for page in pages[:3]]).lower()
    # A distinctive phrase ("bank statement", "trial balance") is worth more than
    # a common one ("debit"), so one clear title is enough on its own.
    scores = {
        name: sum(3 if keyword in STRONG_KEYWORDS else 1 for keyword in words if keyword in haystack)
        for name, words in KEYWORDS.items()
    }
    best = max(scores, key=lambda key: scores[key])
    best_score = scores[best]
    if best_score < 3:
        return "generic", 0.3 if best_score else 0.0
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    confidence = round(min(0.95, 1.0 - (runner_up / (best_score + 1)) * 0.5), 2)
    return best, confidence


# --------------------------------------------------------------------- tables
def page_tables(page: PageData, starts: list[float] | None = None) -> list[list[list[str]]]:
    tables = [table.rows for table in page.tables if table.rows]
    clustered = cluster_table(page, starts=starts)
    if starts:
        # The columns were printed on another page; cut this page at the same
        # headings instead of guessing them a second time.
        return [clustered] if clustered else ([_pick_table(tables)] if tables else [])
    clustered_has_header = bool(clustered) and _header_index(clustered) is not None
    if not tables:
        return [clustered] if clustered else []
    best = _pick_table(tables)
    if _header_index(best) is None:
        # pdfplumber's box starts at the first ruling, which on Axis and SBI sits
        # below the printed header: the table then has no column names and every
        # value is read as a heading. The clustered pass keeps the header line.
        return [clustered] if clustered_has_header else [best]
    if clustered_has_header and (
        max(map(len, clustered)) > max(map(len, best))
        or not any(table.ruled for table in page.tables)
    ):
        # Both found the header, but on a page without vertical rules pdfplumber
        # has to guess where a column ends: it merges SBI's Description with Ref
        # No., ICICI's Debit with Credit, and shifts Axis' TOTAL row one cell
        # left. The clustered table cuts at the gutters and the headings instead.
        return [clustered]
    return [best]


def cluster_table(page: PageData, starts: list[float] | None = None) -> list[list[str]]:
    """Rebuild a table from positioned words when the PDF has no ruling lines.

    Column boundaries are taken from the page as a whole rather than from each
    line: a strip of x positions no line writes into is a gutter between columns,
    so every line splits at the same places. Splitting line by line used to glue
    a date to the description whenever they sat closer together than that row's
    median word gap, which then pushed every value one column to the left.
    """
    if not page.words:
        return []
    lines = [line for line in _group_rows(page.words) if line]
    if not lines:
        return []
    start = _table_start(lines)
    header_at = _header_line_index(lines, start)
    if not starts:
        starts = _label_starts(lines[header_at]) if header_at is not None else []
    if len(starts) >= 2:
        rows = _rows_from_labels(lines, starts)
    else:
        # No headings to go by: fall back to gutters learned from the data lines.
        # A wide heading such as "Transaction Date (dd/mm/yyyy)" runs across
        # them, so the heading line is left out of the count.
        band_lines = [
            line for index, line in enumerate(lines) if index >= start and index != header_at
        ]
        bands = _column_bands(band_lines if len(band_lines) >= 2 else lines[start:], page.width)
        rows = (
            [[_band_text(line, band) for band in bands] for line in lines]
            if bands
            else [_split_cells(line) for line in lines]
        )
    rows = [row for row in rows if any(cell for cell in row)]
    if len(rows) < 2:
        return rows
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    # A band that caught nothing in any line is the margin beside the table, not
    # a column: drop it rather than presenting an empty "Column 1".
    used = [index for index in range(width) if any(row[index] for row in rows)]
    return [[row[index] for index in used] for row in rows]


def _group_rows(words, tolerance: float | None = None) -> list[list]:
    ordered = sorted(words, key=lambda word: (word.centre, word.x0))
    if not ordered:
        return []
    tolerance = tolerance or max(2.5, 3.0)
    rows = [[ordered[0]]]
    anchor = ordered[0].centre
    for word in ordered[1:]:
        if abs(word.centre - anchor) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
            anchor = word.centre
    return [sorted(row, key=lambda word: word.x0) for row in rows]


def _split_cells(words) -> list[str]:
    """Split a line into cells at gaps wider than the words' own spacing.

    The limit comes from the gaps between the words, not from how wide they are:
    a line of dates made every token wide, so the old width-based limit joined
    the whole row into one cell, no row looked like data, and the printed header
    above it was never found.
    """
    if not words:
        return []
    gaps = sorted(
        later.x0 - earlier.x1 for earlier, later in zip(words, words[1:]) if later.x0 > earlier.x1
    )
    gap_limit = max(4.5, (gaps[len(gaps) // 2] if gaps else 0.0) * 2.2)
    cells: list[list] = [[words[0]]]
    for previous, word in zip(words, words[1:]):
        if word.x0 - previous.x1 > gap_limit:
            cells.append([word])
        else:
            cells[-1].append(word)
    return [" ".join(item.text for item in cell).strip() for cell in cells]


def _table_start(lines: list[list]) -> int:
    """Index of the first line of the table proper, ignoring the heading block.

    The two or three title lines above a table cross every column, so counting
    them as table lines hides all the gutters. The table starts two lines above
    its first printed amount, which leaves room for a header that wrapped.
    """
    for index, line in enumerate(lines):
        # Word by word: a line of dates can be split into one wide cell, and then
        # the amount inside it would never be seen.
        if any(_looks_like_amount(word.text) for word in line):
            return max(0, index - 2)
    return 0


def _header_line_index(lines: list[list], start: int = 0) -> int | None:
    """Index of the printed table heading, if this page carries one.

    The search starts a few lines above ``start``: that index is itself derived
    from the first printed amount, which on a tight table can already be below
    the heading.
    """
    offset = max(0, start - 4)
    window = lines[offset:offset + 24]
    # The table proper starts at the first line with several facts. Two are not
    # enough: the period line ("From : 01/09/2026 To : 30/09/2026") has exactly
    # two and sits above the real heading, which then went unseen.
    limit = len(window)
    for index, line in enumerate(window):
        # A transaction line carries both dates and amounts. A period line has
        # two dates but only an account number, and counting that number as an
        # amount made the title block look like the start of the table.
        dates = sum(1 for word in line if parse_date(word.text) is not None)
        # Printed amounts carry a decimal point or a thousands separator; an
        # account number is bare digits and must not pass for an amount.
        amounts = sum(
            1 for word in line
            if ("." in word.text or "," in word.text) and _looks_like_amount(word.text)
        )
        if dates >= 1 and amounts >= 2:
            limit = index
            break
    best: int | None = None
    best_score = 0
    for index in range(limit):
        line = window[index]
        # A title line can carry the statement's "From … To …" dates, and its
        # few words would make a three-column table; a heading names columns and
        # holds no dates.
        if any(parse_date(word.text) is not None for word in line):
            continue
        score = _header_score([" ".join(word.text for word in line)])
        if score >= best_score and score >= 3:
            best, best_score = index, score
    return offset + best if best is not None else None


def _label_starts(header: list) -> list[float]:
    """Left edges of the printed headings on one line.

    A label is a run of words that touch or sit inside one another; a new label
    begins only once a word starts past the end of the previous one. A heading
    that overruns its neighbour stays joined instead of inventing a column.
    """
    if not header:
        return []
    # A space inside a heading is about 2.5 pt at statement type sizes, while the
    # gap between two columns is wider; 4.5 pt keeps "Value Date" whole and still
    # separates "Value Date" from "Txn Posted Date".
    threshold = 4.5
    spans: list[list[float]] = [[header[0].x0, header[0].x1]]
    for word in header[1:]:
        if word.x0 <= spans[-1][1] + threshold:
            spans[-1][1] = max(spans[-1][1], word.x1)
        else:
            spans.append([word.x0, word.x1])
    return [span[0] for span in spans]


def _rows_from_labels(
    lines: list[list], starts: list[float]
) -> list[list[str]]:
    """Cut every line at the printed headings instead of at inferred gutters.

    Looking for empty vertical strips fails exactly where it matters: one long
    description closes the gutter to the next column, so SBI's Description and
    Ref No., and a narrow Branch/Debit/Credit block, came out as one cell. The
    headings do not move, so each word is given to the heading whose left edge
    is the last one before it.
    """
    rows: list[list[str]] = []
    for line in lines:
        cells: list[list] = [[] for _ in starts]
        for word in line:
            index = bisect.bisect_right(starts, word.x0 + 0.5) - 1
            cells[max(0, index)].append(word)
        rows.append([" ".join(word.text for word in cell).strip() for cell in cells])
    return rows


def _column_bands(lines: list[list], page_width: float) -> list[tuple[float, float]]:
    """The page's own columns: strips of x that no line writes into are gutters.

    A gutter has to be free in nearly every line (a handful of long descriptions
    are tolerated) and wider than a word space, so text inside one cell is never
    torn apart while a real column boundary is found even when it is narrow.
    """
    if len(lines) < 2:
        return []
    right = max(word.x1 for line in lines for word in line)
    width = int(max(page_width, right)) + 2
    if width < 16 or width > 20000:
        return []

    coverage = [0] * (width + 1)
    for line in lines:
        touched: set[int] = set()
        for word in line:
            start = max(0, int(word.x0))
            end = min(width, int(word.x1) + 1)
            for x in range(start, end):
                touched.add(x)
        for x in touched:
            coverage[x] += 1

    # A gutter has to be clear in most lines, but not in every one: the two or
    # three title lines above the table cross every column, and a page footer
    # does too, so a strict "free in all lines" rule would hide every column.
    occupied = max(0, int(len(lines) * 0.25))
    gutters: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width + 1):
        if coverage[x] <= occupied:
            if start is None:
                start = x
        elif start is not None:
            if x - start >= MIN_GUTTER:
                gutters.append((start, x))
            start = None
    if start is not None and width - start >= MIN_GUTTER:
        gutters.append((start, width))
    if not gutters:
        return []

    bands: list[tuple[float, float]] = []
    edge = 0.0
    for gutter_start, gutter_end in gutters:
        if gutter_start > edge:
            bands.append((edge, float(gutter_start)))
        edge = float(gutter_end)
    if edge < width:
        bands.append((edge, float(width)))
    return bands if len(bands) >= 2 else []


def _band_text(line: list, band: tuple[float, float]) -> str:
    """The words of one line that start inside one column band.

    A heading is often wider than its column ("Transaction Date (dd/mm/yyyy)"),
    so the word's left edge decides where it belongs; its centre would pull the
    heading into the next column and interleave two labels.
    """
    low, high = band
    return " ".join(
        word.text for word in line if low <= word.x0 < high
    ).strip()


# -------------------------------------------------------------------- columns
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "direction": (),
    "date": ("date", "posted", "value date", "txn date", "transaction date", "period", "day"),
    "description": (
        "description", "details", "narrative", "particulars", "memo", "reference",
        "payee", "item", "account", "supplier", "customer", "transaction",
    ),
    "debit": ("debit", "withdrawal", "withdrawals", "paid out", "money out", "dr"),
    "credit": ("credit", "deposit", "deposits", "paid in", "money in", "cr"),
    "amount": ("amount", "value", "net", "gross", "price", "subtotal", "total"),
    "balance": ("balance", "running balance", "closing balance", "bal"),
}


def _header_matches(name: str, keyword: str) -> bool:
    """Match a header word or phrase at a word boundary.

    ``"cr"`` must not match ``"description"``; ``"date"`` should match
    ``"value date"`` and ``"date"`` only, not ``"update"``.
    """
    if name == keyword:
        return True
    return re.search(rf"\b{re.escape(keyword)}", name) is not None


def classify_columns(columns: list[str]) -> dict[str, int | None]:
    roles: dict[str, int | None] = {role: None for role in ROLE_KEYWORDS}
    taken: set[int] = set()
    normalised = [clean_text(column).lower() for column in columns]

    for index, name in enumerate(normalised):
        if name and DIRECTION_HEADER.search(name):
            roles["direction"] = index
            taken.add(index)
            break

    for role in ("date", "balance", "debit", "credit", "description", "amount"):
        for index, name in enumerate(normalised):
            if index in taken or not name:
                continue
            if role == "amount" and any(
                token in name for token in ("balance", "debit", "credit", "date")
            ):
                continue
            if any(_header_matches(name, keyword) for keyword in ROLE_KEYWORDS[role]):
                roles[role] = index
                taken.add(index)
                break

    if roles["amount"] is None and roles["balance"] is not None:
        # "Transaction Amount" split by a narrow gutter becomes "Transaction" +
        # "(2)", so the heading no longer says "amount". In a statement the
        # unclaimed column just left of the balance is the amount.
        for index in range(int(roles["balance"]) - 1, -1, -1):
            if index not in taken:
                roles["amount"] = index
                break
    return roles


def _dedupe_columns(raw_columns: list[str]) -> list[str]:
    columns: list[str] = []
    seen: dict[str, int] = {}
    for position, name in enumerate(raw_columns, start=1):
        label = clean_text(name) or f"Column {position}"
        if label in seen:
            seen[label] += 1
            label = f"{label} ({seen[label]})"
        else:
            seen[label] = 1
        columns.append(label)
    return columns


AMOUNT_TEXT = re.compile(r"^[\(\-\+]?[$€£¥₹]?\s?\d[\d,. ]*\)?-?$")


def _looks_like_amount(value: str) -> bool:
    """Is this cell a printed amount? ``account 12345678`` is not one.

    ``parse_number`` strips letters, which is right for reading a value but wrong
    here: it turned a title line into "a row of numbers", so the title was taken
    as the table header and became the sheet's column names.
    """
    text = clean_text(value)
    return bool(text) and AMOUNT_TEXT.match(text) is not None and parse_number(text) is not None


HEADER_WORDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [keyword for keywords in ROLE_KEYWORDS.values() for keyword in keywords]
        + ["ref", "cheque", "chq", "branch", "particulars", "narration", "txn", "value", "s.no", "sl", "type"]
    )
)


def _header_score(row: list[str]) -> int:
    """How many distinct column headings this row names.

    Counted across the whole row, not per cell: a heading that wrapped or was
    split by a narrow gutter arrives as one long cell ("Transaction Date
    (dd/mm/yyyy) Value Date (dd/mm/yyyy) Particulars …") and still has to be
    recognised as the header.
    """
    text = " ".join(clean_text(cell) for cell in row).lower()
    if not text.strip():
        return 0
    return sum(1 for keyword in HEADER_WORDS if _header_matches(text, keyword))


def _is_data_row(row: list[str]) -> bool:
    """A row of facts: two or more printed dates or amounts."""
    facts = sum(
        1 for cell in row if parse_date(cell) is not None or _looks_like_amount(cell)
    )
    return facts >= 2


def _header_index(rows: list[list[str]], limit: int = 16) -> int | None:
    """The row that names the columns, if the page prints one.

    The old rule took the first row of mostly text, which on a statement whose
    header sits outside pdfplumber's box was the first *data* row: every column
    was then named after a value ("1", "05/09/2026", …) and no date, amount or
    balance was ever recognised.

    The header is found by position instead: it is the labelled row just above
    the first row of facts. Scanning upwards from the data keeps a title block
    ("Account Name : …", "Opening Balance as on …") from being mistaken for it.
    """
    first_data = None
    for index, row in enumerate(rows[:limit]):
        if _is_data_row(row):
            first_data = index
            break
    if first_data is None:
        return None
    for index in range(first_data - 1, max(-1, first_data - 4), -1):
        if _header_score(rows[index]) >= 3:
            return index
    return None


def _header_row(rows: list[list[str]]) -> int | None:
    index = _header_index(rows)
    if index is not None:
        return index
    for index, row in enumerate(rows[:6]):
        filled = [cell for cell in row if clean_text(cell)]
        if len(filled) < 2:
            continue
        text_cells = sum(
            1 for cell in filled if not _looks_like_amount(cell) and not parse_date(cell)
        )
        following = rows[index + 1] if index + 1 < len(rows) else []
        has_numbers = any(_looks_like_amount(cell) for cell in following)
        if text_cells >= max(2, int(len(filled) * 0.6)) and (has_numbers or index == len(rows) - 1):
            return index
    return None


def _is_total_row(cells: list[str]) -> bool:
    for cell in cells:
        text = clean_text(cell)
        if text and TOTAL_PATTERN.match(text):
            return True
    return False


FOOTER_PATTERN = re.compile(
    r"does not require a signature|computer generated|constituent of any discrepancy|"
    r"no signature (is )?required|system generated|please examine|for any discrepancy|"
    r"registered office|this is a (system|computer)|subject to (realisation|verification)",
    re.IGNORECASE,
)


def _pick_table(tables: list[list[list[str]]]) -> list[list[str]]:
    if not tables:
        return []
    def score(table: list[list[str]]) -> tuple[int, int]:
        rows = len(table)
        width = max((len(row) for row in table), default=0)
        return (rows * width, rows)
    return max(tables, key=score)


def _records_from_table(
    table: list[list[str]], known: list[str] | None = None
) -> tuple[list[str], list[dict], list[bool]]:
    """Rows of one page's table, with its column names when the page prints them.

    ``known`` carries the names found on an earlier page: a continuation page
    usually has no heading of its own, and without them its first data row used
    to be consumed as one and silently lost.
    """
    if not table:
        return [], [], []
    header = _header_row(table)
    if header is None and known and len(known) == len(table[0]):
        header_index = -1
        raw_columns = list(known)
    else:
        header_index = header if header is not None else 0
        raw_columns = table[header_index]
    columns = _dedupe_columns(raw_columns)
    records: list[dict] = []
    flags: list[bool] = []
    for row in table[header_index + 1:]:
        cells = [clean_text(cell) for cell in row]
        if not any(cells):
            continue
        cells = (cells + [""] * len(columns))[: len(columns)]
        if [cell.lower() for cell in cells] == [column.lower() for column in columns]:
            continue  # repeated header
        records.append(dict(zip(columns, cells)))
        flags.append(_is_total_row(cells))
    columns, records, flags = _merge_continuations(columns, records, flags, _description_name(columns))
    return columns, records, flags


def _description_name(columns: list[str]) -> str | None:
    """The column that holds the narrative, whatever this bank calls it."""
    index = classify_columns(columns).get("description")
    if index is not None and index < len(columns):
        return columns[index]
    return DESCRIPTION_COLUMN if DESCRIPTION_COLUMN in columns else None


def _merge_continuations(
    columns: list[str],
    records: list[dict],
    flags: list[bool],
    description_column: str | None = None,
) -> tuple[list[str], list[dict], list[bool]]:
    """Join a wrapped description to the row above it.

    A long narrative that wraps onto a second printed line arrives as a row with
    words but no date and no amounts. Left alone it became its own record with
    every value blank, which reads as a broken row in the sheet; here the words
    are appended to the description of the record they belong to.
    """
    description_column = description_column or (DESCRIPTION_COLUMN if DESCRIPTION_COLUMN in columns else None)
    if len(records) < 2 or not description_column:
        return columns, records, flags
    roles = classify_columns(columns)
    date_column = (
        columns[roles["date"]] if roles.get("date") is not None and roles["date"] < len(columns) else None
    )
    number_columns = [
        columns[roles[role]]
        for role in ("amount", "debit", "credit", "balance")
        if roles.get(role) is not None and roles[role] < len(columns)
    ]
    others = [column for column in columns if column != description_column]
    text_columns = [
        column for column in others
        if column != date_column and column not in number_columns
    ]
    kept: list[dict] = []
    kept_flags: list[bool] = []
    for record, flag in zip(records, flags):
        text = clean_text(record.get(description_column, ""))
        has_date = bool(date_column and parse_date(record.get(date_column)))
        has_number = any(parse_number(record.get(column)) is not None for column in number_columns)
        empty_elsewhere = not any(clean_text(record.get(column, "")) for column in others)
        words_only = not has_date and not has_number
        boilerplate = bool(FOOTER_PATTERN.search(text))
        after_total = bool(kept_flags) and kept_flags[-1]
        # A line with no date and no amount is a wrapped narrative, a section
        # heading or a page footer. It joins the row above only while that row
        # is a real transaction: after the TOTAL row, and for the boilerplate
        # every statement prints under the table, it is noise and is dropped
        # instead of being glued onto the last transaction.
        if words_only and (boilerplate or after_total):
            continue
        if kept and text and not flag and (empty_elsewhere or words_only):
            previous = kept[-1]
            extra = " ".join(
                clean_text(record.get(column, "")) for column in text_columns if column != description_column
            ).strip()
            parts = [clean_text(previous.get(description_column, "")), text, extra]
            previous[description_column] = " ".join(part for part in parts if part).strip()
            continue
        kept.append(dict(record))
        kept_flags.append(flag)
    return columns, kept, kept_flags


LINE_DATE = re.compile(
    r"^(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}[\s\-][A-Za-z]{3,9}[\s\-]\d{2,4})\b"
)
TRAILING_NUMBER = re.compile(r"^[\(\-\+]?[$€£¥₹]?\s?\d[\d,.\s]*\)?-?$")


def _records_from_lines(lines: list[str], doc_type: str) -> tuple[list[str], list[dict], list[bool]]:
    rows: list[tuple[str, str, list[str]]] = []
    for line in lines:
        text = clean_text(line)
        if not text:
            continue
        match = LINE_DATE.match(text)
        leading_date = match.group(0) if match else ""
        rest = text[len(leading_date):].strip() if leading_date else text
        tokens = rest.split()
        numbers: list[str] = []
        while tokens and TRAILING_NUMBER.match(tokens[-1]) and parse_number(tokens[-1]) is not None:
            numbers.insert(0, tokens.pop())
        description = " ".join(tokens).strip()
        if not leading_date and not numbers:
            continue
        rows.append((leading_date, description, numbers))

    if not rows:
        return [], [], []

    max_numbers = max(len(numbers) for _date, _desc, numbers in rows)
    columns = ["Date", "Description"]
    numeric_names: list[str]
    if max_numbers >= 3:
        numeric_names = ["Amount", "Debit", "Credit", "Balance"][-max_numbers:]
    elif max_numbers == 2:
        numeric_names = ["Debit", "Credit"] if doc_type in {"ledger", "trial_balance"} else ["Amount", "Balance"]
    else:
        numeric_names = ["Amount"] if max_numbers else []
    columns += numeric_names

    records: list[dict] = []
    flags: list[bool] = []
    for leading_date, description, numbers in rows:
        values = dict.fromkeys(columns, "")
        values["Date"] = leading_date
        values["Description"] = description
        # Right-align the numbers so a missing debit still lands in its column.
        for offset, number in enumerate(numbers, start=max(0, len(numeric_names) - len(numbers))):
            if offset < len(numeric_names):
                values[numeric_names[offset]] = number
        records.append(values)
        flags.append(_is_total_row([description]))
    return columns, records, flags


# --------------------------------------------------------------------- public
def _signature(columns: list[str]) -> tuple[str, ...]:
    return tuple(clean_text(column).lower() for column in columns)


def extract(source: PdfSource, filename: str = "") -> ExtractResult:
    pages = source.pages()
    if not pages:
        raise ValueError("This PDF has no pages.")

    doc_type, confidence = detect_doc_type(filename, pages)
    notes: list[str] = []

    # One table per page, so a statement that runs over several pages keeps every
    # page's rows and every row remembers the page it came from. The largest table
    # decides the columns; matching tables on the other pages continue the set.
    # A continuation page often prints no headings. The page that does print
    # them supplies the column positions for the rest, so those pages are cut at
    # the same places instead of at bands guessed from their own rows.
    own_starts: dict[int, list[float]] = {}
    for page in pages:
        lines = [line for line in _group_rows(page.words) if line]
        if not lines:
            continue
        header_at = _header_line_index(lines, _table_start(lines))
        starts = _label_starts(lines[header_at]) if header_at is not None else []
        if len(starts) >= 2:
            own_starts[page.index] = starts
    shared_starts = max(own_starts.values(), key=len) if own_starts else []

    blocks: list[tuple[int, list[list[str]]]] = []
    tables_found = 0
    for page in pages:
        tables = page_tables(page, starts=own_starts.get(page.index, shared_starts or None))
        tables_found += len(tables)
        best = _pick_table(tables) if tables else []
        if best:
            blocks.append((page.index + 1, best))

    columns: list[str] = []
    records: list[dict] = []
    flags: list[bool] = []
    record_pages: list[int] = []

    if blocks:
        primary = max(
            blocks,
            key=lambda item: (len(item[1]), max((len(row) for row in item[1]), default=0)),
        )
        signature: tuple[str, ...] | None = None
        collected: list[tuple[int, int, dict, bool]] = []
        for number, table in [primary] + [block for block in blocks if block is not primary]:
            page_columns, page_records, page_flags = _records_from_table(
                table, columns if signature is not None else None
            )
            if not page_records:
                continue
            if signature is None:
                signature = _signature(page_columns)
                columns = page_columns
            elif len(page_columns) != len(columns):
                notes.append(
                    f"Page {number} has a different table layout; those rows were left out."
                )
                continue
            elif _signature(page_columns) != signature:
                # Same table, different wording (SBI repeats its headings on every
                # page, some banks print none after page one). Keep the first
                # page's names and line the columns up by position instead of
                # throwing the page away, which used to lose every page but one.
                page_records = [
                    {columns[index]: record.get(page_columns[index], "")
                     for index in range(len(columns)) if index < len(page_columns)}
                    for record in page_records
                ]
            collected.extend(
                (number, index, record, flag)
                for index, (record, flag) in enumerate(zip(page_records, page_flags))
            )
        collected.sort(key=lambda item: (item[0], item[1]))
        records = [item[2] for item in collected]
        flags = [item[3] for item in collected]
        record_pages = [item[0] for item in collected]

    if not records:
        # No ruled table: read the lines, still one block per page for the pages.
        for page in pages:
            page_columns, page_records, page_flags = _records_from_lines(page.lines, doc_type)
            if not page_records:
                continue
            if not columns:
                columns = page_columns
            elif _signature(page_columns) != _signature(columns):
                notes.append(f"Page {page.index + 1} has different columns; its lines were skipped.")
                continue
            records.extend({column: record.get(column, "") for column in columns} for record in page_records)
            flags.extend(page_flags)
            record_pages.extend([page.index + 1] * len(page_records))
        if records:
            notes.append("No ruled table was found; the lines were read instead.")

    if not records:
        notes.append("No tabular rows were found on these pages.")

    order = date_order_for([record.get(columns[0], "") for record in records]) if columns else "dmy"
    roles = classify_columns(columns)

    return ExtractResult(
        doc_type=doc_type,
        confidence=confidence,
        columns=columns,
        records=records,
        total_flags=flags,
        roles=roles,
        tables_found=tables_found,
        pages_total=len(pages),
        pages_with_text=sum(1 for page in pages if page.has_text),
        ocr_pages=sum(1 for page in pages if page.ocr_used),
        date_order=order,
        record_pages=record_pages,
        notes=notes,
    )
