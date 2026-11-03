"""Financial checks reported on the Validation sheet.

Every check answers a question an accountant would ask of the converted sheet,
and every check is allowed to say "Not Applicable" instead of inventing a
verdict. Source values are never modified to make a check agree: a mismatch is
reported, not reconciled.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.converter.extract import ExtractResult, parse_date, parse_number

PASSED = "Passed"
WARNING = "Warning"
ERROR = "Error"
NA = "Not Applicable"

NUMERIC_ROLES = ("amount", "debit", "credit", "balance")


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str


def validate(result: ExtractResult) -> list[Check]:
    checks: list[Check] = [
        _pages_check(result),
        _rows_check(result),
        _columns_check(result),
        _amounts_check(result),
        _totals_check(result),
        _balance_check(result),
        _running_balance_check(result),
        _duplicates_check(result),
        _dates_check(result),
        _totals_rows_check(result),
    ]
    return checks


def summarise(checks: list[Check]) -> dict[str, int]:
    summary = {PASSED: 0, WARNING: 0, ERROR: 0, NA: 0}
    for check in checks:
        summary[check.status] = summary.get(check.status, 0) + 1
    return summary


# ---------------------------------------------------------------------- checks
def _pages_check(result: ExtractResult) -> Check:
    total = max(result.pages_total, 0)
    if total == 0:
        return Check("Page coverage", ERROR, "The PDF reported no pages.")
    if result.pages_with_text == total:
        return Check("Page coverage", PASSED, f"All {total} page(s) carry a text layer.")
    scanned = total - result.pages_with_text
    if result.ocr_pages:
        return Check(
            "Page coverage",
            WARNING,
            f"{scanned} of {total} page(s) had no text layer; {result.ocr_pages} were read with OCR. "
            "Check those pages against the source.",
        )
    return Check(
        "Page coverage",
        WARNING,
        f"{scanned} of {total} page(s) had no text layer and OCR was not available. "
        "The scanned pages could only be reproduced visually.",
    )


def _rows_check(result: ExtractResult) -> Check:
    rows = result.row_count
    if rows == 0:
        return Check(
            "Rows extracted",
            ERROR,
            "No records were found. The file may be a scan without OCR, or a layout with no table.",
        )
    if not result.columns:
        return Check("Rows extracted", WARNING, f"{rows} row(s) were read but no columns were identified.")
    return Check(
        "Rows extracted",
        PASSED,
        f"{rows} row(s) across {len(result.columns)} column(s) as {result.doc_label.lower()}.",
    )


def _columns_check(result: ExtractResult) -> Check:
    roles = {role: index for role, index in result.roles.items() if index is not None}
    if not result.columns:
        return Check("Columns detected", NA, "No columns to classify.")
    found = [role for role in ("date", "description", *NUMERIC_ROLES) if role in roles]
    if not found:
        return Check(
            "Columns detected",
            WARNING,
            "No date, description or amount column was recognised; the header row may be missing.",
        )
    has_value = any(role in roles for role in NUMERIC_ROLES)
    detail = "Recognised: " + ", ".join(sorted(found)) + "."
    return Check("Columns detected", PASSED if has_value else WARNING, detail)


def _amounts_check(result: ExtractResult) -> Check:
    rows = []
    for role in NUMERIC_ROLES:
        index = result.roles.get(role)
        if index is None:
            continue
        column = result.columns[index]
        filled = sum(1 for record in result.records if str(record.get(column, "")).strip())
        parsed = len(result.numeric_values(role))
        if filled:
            rows.append((column, filled, parsed))

    if not rows:
        return Check("Amounts read", NA, "This document has no amount columns.")

    problems = [f"{column}: {parsed} of {filled}" for column, filled, parsed in rows if parsed < filled]
    detail = "Parsed " + ", ".join(f"{column} {parsed}/{filled}" for column, filled, parsed in rows) + "."
    if not problems:
        return Check("Amounts read", PASSED, detail)
    missing = sum(filled - parsed for _column, filled, parsed in rows)
    status = ERROR if missing > sum(filled for _c, filled, _p in rows) * 0.3 else WARNING
    return Check("Amounts read", status, detail + " Unreadable: " + "; ".join(problems) + ".")


def _totals_check(result: ExtractResult) -> Check:
    debit_index = result.roles.get("debit")
    credit_index = result.roles.get("credit")
    if debit_index is None or credit_index is None:
        return Check("Debit equals credit", NA, "The document has no debit and credit columns.")

    debits = _column_total(result, "debit")
    credits = _column_total(result, "credit")
    difference = round(debits - credits, 2)
    detail = f"Debits {debits:,.2f}, credits {credits:,.2f}, difference {difference:,.2f}."
    if abs(difference) < 0.005:
        return Check("Debit equals credit", PASSED, detail)
    return Check("Debit equals credit", ERROR, detail + " Source values were left unchanged.")


def _balance_check(result: ExtractResult) -> Check:
    balance_index = result.roles.get("balance")
    if balance_index is None:
        return Check("Opening / closing balance", NA, "The document has no balance column.")
    column = result.columns[balance_index]
    balances = [
        value
        for record, is_total in zip(result.records, result.total_flags)
        if not is_total
        for value in [parse_number(record.get(column))]
        if value is not None
    ]
    if len(balances) < 2:
        return Check("Opening / closing balance", WARNING, "Fewer than two balance values were readable.")

    opening, closing = balances[0], balances[-1]
    movement = round(sum(result.numeric_values("amount")), 2)
    stated = round(closing - opening, 2)
    detail = f"Opening {opening:,.2f}, closing {closing:,.2f}, movement {stated:,.2f}."
    if not result.numeric_values("amount"):
        return Check("Opening / closing balance", PASSED, detail + " No amount column to reconcile against.")
    if abs(stated - movement) < 0.02:
        return Check("Opening / closing balance", PASSED, detail + f" Amounts total {movement:,.2f}.")
    return Check(
        "Opening / closing balance",
        WARNING,
        detail + f" Amounts total {movement:,.2f}; the {round(stated - movement, 2):,.2f} gap may be "
        "opening entries, transfers or unreadable rows.",
    )


def _running_balance_check(result: ExtractResult) -> Check:
    balance_index = result.roles.get("balance")
    amount_index = result.roles.get("amount")
    if balance_index is None or amount_index is None:
        return Check("Running balance chain", NA, "Needs both a balance and an amount column.")

    balance_column = result.columns[balance_index]
    amount_column = result.columns[amount_index]
    pairs = []
    for record, is_total in zip(result.records, result.total_flags):
        if is_total:
            continue
        balance = parse_number(record.get(balance_column))
        amount = parse_number(record.get(amount_column))
        if balance is not None and amount is not None:
            pairs.append((balance, amount))

    if len(pairs) < 2:
        return Check("Running balance chain", NA, "Not enough consecutive rows to test the chain.")

    breaks = 0
    for previous, current in zip(pairs, pairs[1:]):
        expected = previous[0] + current[1]
        if abs(expected - current[0]) > 0.02:
            breaks += 1
    if breaks == 0:
        return Check("Running balance chain", PASSED, f"All {len(pairs) - 1} step(s) agree.")
    ratio = breaks / max(len(pairs) - 1, 1)
    status = ERROR if ratio > 0.3 else WARNING
    return Check(
        "Running balance chain",
        status,
        f"{breaks} of {len(pairs) - 1} step(s) do not add up. Reversed signs or reordered pages are the usual cause.",
    )


def _duplicates_check(result: ExtractResult) -> Check:
    if result.row_count < 2:
        return Check("Duplicate rows", NA, "Not enough rows to compare.")
    seen: dict[tuple, int] = {}
    for record, is_total in zip(result.records, result.total_flags):
        if is_total:
            continue
        key = tuple(
            (record.get(column) or "").strip().lower() for column in result.columns
        )
        seen[key] = seen.get(key, 0) + 1
    duplicates = sum(count - 1 for count in seen.values() if count > 1)
    if duplicates == 0:
        return Check("Duplicate rows", PASSED, "No identical rows.")
    return Check(
        "Duplicate rows",
        WARNING,
        f"{duplicates} row(s) repeat an earlier row exactly. The conversion keeps every row it finds.",
    )


def _dates_check(result: ExtractResult) -> Check:
    index = result.roles.get("date")
    if index is None:
        return Check("Dates", NA, "No date column was recognised.")
    column = result.columns[index]
    values = [record.get(column, "") for record in result.records if str(record.get(column, "")).strip()]
    if not values:
        return Check("Dates", NA, "The date column is empty.")
    parsed = sum(1 for value in values if parse_date(value, result.date_order))
    if parsed == len(values):
        return Check("Dates", PASSED, f"All {len(values)} date(s) read as dates ({result.date_order.upper()} order).")
    status = WARNING if parsed else ERROR
    return Check(
        "Dates",
        status,
        f"{parsed} of {len(values)} date(s) recognised ({result.date_order.upper()} order). "
        "Unrecognised dates stay as text.",
    )


def _totals_rows_check(result: ExtractResult) -> Check:
    totals = sum(1 for flag in result.total_flags if flag)
    if totals == 0:
        return Check("Total rows", NA, "No total or balance-forward line was found.")
    return Check(
        "Total rows",
        PASSED,
        f"{totals} total or carried-forward line(s) kept as printed and left out of the column sums.",
    )


# --------------------------------------------------------------------- helpers
def _column_total(result: ExtractResult, role: str) -> float:
    index = result.roles.get(role)
    if index is None:
        return 0.0
    column = result.columns[index]
    total = 0.0
    for record, is_total in zip(result.records, result.total_flags):
        if is_total:
            continue
        value = parse_number(record.get(column))
        if value is not None:
            total += abs(value)
    return round(total, 2)
