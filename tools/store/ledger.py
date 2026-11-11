"""
tools/store/ledger.py

Pure parsing and arithmetic for the shop ledgers in ``~/Desktop/store``.

There is no code in the store folder itself — only two hand-written text
ledgers — so "store logic" means the bookkeeping the operator does by hand:

``malik_list/malik_list.txt``
    Stock bought from the supplier, one line per item::

        Realme C11 outer key - Buy: Rs 40, Sell: Rs 250
        Y12 glass guard (10 pieces) - Buy: Rs 190, Sell: Rs 100/piece
        D data cable 65W (10 pieces) - Buy: Rs 550 total

``store_selling_list/selling_list.txt``
    Daily counter sales, grouped under ``Date: dd/mm/yyyy`` headers::

        - Jio Recharge: Rs 350
        - Home Theater Repairing: Total Rs 700 (Paid Rs 500, Pending Rs 200)
        - CMF Neckband Pro: Rs 500 (Pending)

Both files end with a hand-written ``Final Hisab`` block.  That block is
parsed as *recorded* totals, never as entries: it is what the operator wrote
down, and it can drift from the lines above it.

This module deliberately does no I/O and imports nothing from Alex, so the
arithmetic can be tested with plain strings and reused by the CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Sequence

__all__ = [
    "Sale",
    "Bill",
    "SellingLedger",
    "StockItem",
    "StockLedger",
    "Totals",
    "StockTotals",
    "RecordedTotals",
    "parse_selling",
    "parse_stock",
    "totals",
    "totals_by_day",
    "stock_totals",
    "find_sales",
    "find_stock",
    "sale_line",
    "stock_line",
    "insert_entry",
    "inr",
    "parse_day",
    "day_key",
]

# ── patterns ──────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\s*(?:date\s*:\s*)?(\d{1,2})/(\d{1,2})/(\d{4})\s*$", re.I)
_FENCE_RE = re.compile(r"^\s*-{5,}\s*$")
_MONEY_RE = re.compile(r"(?:rs\.?|₹)\s*([\d,]+(?:\.\d+)?)", re.I)
_TOTAL_RE = re.compile(r"total\s*(?:rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", re.I)
_PENDING_RE = re.compile(r"pending\s*(?:rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", re.I)
_QTY_RE = re.compile(r"\(\s*(\d+)\s*(?:pieces?|pcs?\.?|pc\.?|nos?\.?)\s*\)", re.I)
_PER_UNIT_RE = re.compile(r"/\s*(?:piece|pc|pcs|unit)\b|\beach\b", re.I)
_LUMP_RE = re.compile(r"\btotal\b", re.I)
_RECHARGE_RE = re.compile(r"\brecharge\b", re.I)
_BILL_HEADER_RE = re.compile(r"^(?P<name>[^\d][^:]{2,60}):\s*$")

# Keys used by the hand-written summary blocks, normalised to lowercase.
_RECORDED_KEYS = {
    "total selling": "total",
    "total selling price of items": "total",
    "total sales": "total",
    "total recharges": "recharges",
    "cash after recharges": "cash_after_recharges",
    "total pending": "pending",
    "estimate buy price": "buy",
    "estimated buy price": "buy",
    "total buy": "buy",
    "total profit": "profit",
    "profit": "profit",
}


def _money(raw: str) -> float:
    return float(raw.replace(",", ""))


def inr(value: float) -> str:
    """Format an amount the way the ledgers do: ``1,12,712`` (Indian grouping)."""
    negative = value < 0
    value = abs(value)
    whole = int(value)
    paise = round((value - whole) * 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    text = f"{digits}.{paise:02d}" if paise else digits
    return f"-{text}" if negative else text


def parse_day(raw: str) -> date | None:
    """Parse ``dd/mm/yyyy`` (the shop's format, not ``mm/dd``)."""
    match = _DATE_RE.match(raw)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def day_key(day: date | None) -> str:
    return day.strftime("%d/%m/%Y") if day else ""


def today() -> date:
    return date.today()


def _sort_key(day: date | None) -> tuple[int, date]:
    return (0, day) if day else (1, date.min)


# ── data model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sale:
    """One line of the selling ledger (or one consolidated bill)."""

    date: date | None
    name: str
    amount: float
    pending: float = 0.0
    quantity: int = 1
    recharge: bool = False
    source: str = "entry"
    note: str = ""

    @property
    def collected(self) -> float:
        return max(self.amount - self.pending, 0.0)


@dataclass(frozen=True)
class Bill:
    """A named block such as ``Keshav Kaka Mobile List`` with a running total."""

    date: date | None
    name: str
    total: float
    paid: float
    pending: float
    items: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class RecordedTotals:
    """The hand-written ``Final Hisab`` block, as written."""

    values: dict[str, float] = field(default_factory=dict)
    period: str = ""
    lines: tuple[str, ...] = ()

    def get(self, key: str) -> float | None:
        return self.values.get(key)


@dataclass(frozen=True)
class Totals:
    entries: int
    total: float
    recharges: float
    cash_after_recharges: float
    pending: float
    pending_entries: int
    collected: float

    @property
    def non_recharge(self) -> float:
        return self.total - self.recharges


@dataclass(frozen=True)
class SellingLedger:
    entries: tuple[Sale, ...]
    bills: tuple[Bill, ...]
    recorded: RecordedTotals | None
    warnings: tuple[str, ...]
    skipped: tuple[str, ...]
    dates: tuple[date, ...]

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None


@dataclass(frozen=True)
class StockItem:
    date: date | None
    name: str
    quantity: int
    buy: float
    sell: float
    buy_per_unit: bool = False
    sell_per_unit: bool = False
    sell_inferred: bool = False

    @property
    def buy_total(self) -> float:
        return self.buy * self.quantity if self.buy_per_unit else self.buy

    @property
    def sell_total(self) -> float:
        return self.sell * self.quantity if self.sell_per_unit else self.sell

    @property
    def has_sell(self) -> bool:
        return self.sell_total > 0

    @property
    def profit(self) -> float:
        return self.sell_total - self.buy_total if self.has_sell else 0.0


@dataclass(frozen=True)
class StockTotals:
    items: int
    buy: float
    sell: float
    expected_profit: float
    unsold_cost: float
    priced_items: int

    @property
    def margin_pct(self) -> float:
        cost = self.buy - self.unsold_cost
        return (self.expected_profit / cost * 100.0) if cost > 0 else 0.0


@dataclass(frozen=True)
class StockLedger:
    items: tuple[StockItem, ...]
    recorded: RecordedTotals | None
    warnings: tuple[str, ...]
    skipped: tuple[str, ...]
    dates: tuple[date, ...]

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None


# ── selling ledger ────────────────────────────────────────────────────────────


def _parse_recorded(block: Sequence[str]) -> RecordedTotals | None:
    values: dict[str, float] = {}
    period = ""
    for raw in block:
        line = raw.strip()
        if not line or _FENCE_RE.match(line):
            continue
        lowered = line.lower()
        if lowered.startswith("final hisab"):
            inside = re.search(r"\(([^)]*)\)", line)
            if inside:
                period = inside.group(1).strip()
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = re.sub(r"\s+", " ", key.strip().lower()).rstrip(".")
        canonical = _RECORDED_KEYS.get(key)
        if not canonical:
            continue
        match = _MONEY_RE.search(rest) or re.search(r"([\d,]+(?:\.\d+)?)", rest)
        if match:
            values[canonical] = _money(match.group(1))
    if not values:
        return None
    return RecordedTotals(values=values, period=period, lines=tuple(block))


def _parse_sale_line(line: str) -> Sale | None:
    body = line.lstrip("- ").strip()
    if not body or ":" not in body:
        return None
    name, _, rest = body.partition(":")
    name = re.sub(r"\s+", " ", name).strip()
    rest = rest.strip()
    if not name:
        return None

    total_match = _TOTAL_RE.search(rest)
    if total_match:
        amount = _money(total_match.group(1))
    else:
        money_matches = _MONEY_RE.findall(rest)
        if not money_matches:
            return Sale(
                date=None,
                name=name,
                amount=0.0,
                note="price not specified",
            )
        amount = _money(money_matches[0])

    quantity = 1
    qty_match = _QTY_RE.search(name) or _QTY_RE.search(rest)
    if qty_match:
        quantity = int(qty_match.group(1))
    if _PER_UNIT_RE.search(rest) and quantity > 1:
        amount *= quantity

    lowered = rest.lower()
    if "fully paid" in lowered:
        pending = 0.0
    elif (pending_match := _PENDING_RE.search(rest)) is not None:
        pending = _money(pending_match.group(1))
    elif re.search(r"\(\s*pending\s*\)", lowered):
        pending = amount
    else:
        pending = 0.0

    pending = min(pending, amount)
    return Sale(
        date=None,
        name=name,
        amount=amount,
        pending=pending,
        quantity=quantity,
        recharge=bool(_RECHARGE_RE.search(name)),
    )


def parse_selling(text: str) -> SellingLedger:
    """Parse the selling ledger.

    Lines inside a dashed fence are treated as the recorded summary, never as
    sales.  A ``Name:`` header followed by ``-`` lines becomes a :class:`Bill`
    and is counted once, by its ``Total Bill`` when present, so the items are
    not double counted.
    """
    entries: list[Sale] = []
    bills: list[Bill] = []
    recorded: RecordedTotals | None = None
    skipped: list[str] = []
    dates: list[date] = []
    current: date | None = None

    bill_name: str | None = None
    bill_items: list[str] = []
    bill_total = 0.0
    bill_paid = 0.0
    bill_pending: float | None = None
    bill_has_total = False

    def flush_bill() -> None:
        nonlocal bill_name, bill_items, bill_total, bill_paid
        nonlocal bill_pending, bill_has_total
        if bill_name is None:
            return
        subtotal = bill_total if bill_has_total else sum(
            _sale_amount(item) for item in bill_items
        )
        pending = bill_pending
        if pending is None:
            pending = max(subtotal - bill_paid, 0.0) if bill_paid else 0.0
        pending = min(pending, subtotal)
        paid = bill_paid if bill_paid else max(subtotal - pending, 0.0)
        bills.append(
            Bill(
                date=current,
                name=bill_name,
                total=subtotal,
                paid=paid,
                pending=pending,
                items=tuple(bill_items),
            )
        )
        entries.append(
            Sale(
                date=current,
                name=bill_name,
                amount=subtotal,
                pending=pending,
                quantity=1,
                recharge=False,
                source="bill",
            )
        )
        bill_name, bill_items = None, []
        bill_total, bill_paid = 0.0, 0.0
        bill_pending, bill_has_total = None, False

    in_summary = False
    summary_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()

        if _FENCE_RE.match(line):
            if in_summary:
                recorded = _parse_recorded(summary_lines) or recorded
                summary_lines = []
            else:
                flush_bill()
            in_summary = not in_summary
            continue

        if in_summary:
            summary_lines.append(line)
            continue

        if not line:
            continue

        parsed_day = parse_day(line)
        if parsed_day is not None:
            flush_bill()
            current = parsed_day
            if parsed_day not in dates:
                dates.append(parsed_day)
            continue

        if line.startswith("-"):
            if bill_name is not None:
                bill_items.append(line)
                continue
            sale = _parse_sale_line(line)
            if sale is None:
                skipped.append(line)
                continue
            entries.append(
                Sale(
                    date=current,
                    name=sale.name,
                    amount=sale.amount,
                    pending=sale.pending,
                    quantity=sale.quantity,
                    recharge=sale.recharge,
                    note=sale.note,
                )
            )
            if current is not None and current not in dates:
                dates.append(current)
            continue

        # A bill header, or one of its summary lines.
        header = _BILL_HEADER_RE.match(line)
        lowered = line.lower()
        if lowered.startswith("total bill"):
            bill_total = _money(_MONEY_RE.findall(line)[0]) if _MONEY_RE.findall(line) else bill_total
            bill_has_total = True
            continue
        if lowered.startswith("paid") and bill_name is not None:
            match = _MONEY_RE.search(line)
            if match:
                bill_paid = _money(match.group(1))
            continue
        if lowered.startswith("pending") and bill_name is not None:
            match = _MONEY_RE.search(line)
            if match:
                bill_pending = _money(match.group(1))
            continue
        if header and "rs" not in lowered and not _MONEY_RE.search(line):
            flush_bill()
            bill_name = header.group("name").strip()
            continue
        skipped.append(line)

    flush_bill()

    bills, entries, duplicate_bills = _dedupe_bills(bills, entries)
    bill_lines = {item.strip() for bill in bills for item in bill.items}
    ordered = tuple(sorted(entries, key=lambda s: _sort_key(s.date)))
    ordered_dates = tuple(sorted(set(dates)))
    return SellingLedger(
        entries=ordered,
        bills=tuple(bills),
        recorded=recorded,
        warnings=_selling_warnings(ordered, duplicate_bills, skipped, text, bill_lines),
        skipped=tuple(skipped),
        dates=ordered_dates,
    )


def _bill_richness(bill: Bill) -> tuple[int, int, int, float]:
    """Prefer the copy of a repeated bill that carries the most detail."""
    return (
        len(bill.items),
        1 if bill.paid else 0,
        1 if bill.pending else 0,
        bill.total,
    )


def _dedupe_bills(
    bills: Sequence[Bill], entries: Sequence[Sale]
) -> tuple[list[Bill], list[Sale], list[Bill]]:
    """Count one bill once when the same block was written twice.

    Two blocks with the same date and header are the same customer bill when
    their item sets match or one contains the other (the shop wrote the list
    twice, once with the ``Total Bill``/``Paid``/``Pending`` lines).  The
    richer copy is kept, the other is reported as a duplicate.
    """
    kept: list[Bill] = []
    duplicates: list[Bill] = []
    for bill in bills:
        match: Bill | None = None
        for existing in kept:
            if existing.date != bill.date:
                continue
            if existing.name.strip().lower() != bill.name.strip().lower():
                continue
            left = {item.strip().lower() for item in existing.items}
            right = {item.strip().lower() for item in bill.items}
            if left == right or left <= right or right <= left:
                match = existing
                break
        if match is None:
            kept.append(bill)
            continue
        if _bill_richness(bill) > _bill_richness(match):
            duplicates.append(match)
            kept[kept.index(match)] = bill
        else:
            duplicates.append(bill)

    # Rebuild one entry per kept bill so totals match the deduplicated list.
    plain = [sale for sale in entries if sale.source != "bill"]
    rebuilt = [
        *plain,
        *(
            Sale(
                date=bill.date,
                name=bill.name,
                amount=bill.total,
                pending=bill.pending,
                quantity=1,
                recharge=False,
                source="bill",
            )
            for bill in kept
        ),
    ]
    return kept, rebuilt, duplicates


def _sale_amount(line: str) -> float:
    parsed = _parse_sale_line(line)
    return parsed.amount if parsed else 0.0


def _selling_warnings(
    entries: Sequence[Sale],
    duplicate_bills: Sequence[Bill],
    skipped: Sequence[str],
    text: str,
    bill_lines: set[str],
) -> tuple[str, ...]:
    warnings: list[str] = []

    unpriced = [sale.name for sale in entries if sale.note == "price not specified"]
    if unpriced:
        warnings.append(
            f"{len(unpriced)} entry(s) have no price recorded: "
            f"{', '.join(sorted(set(unpriced))[:5])}"
        )

    for bill in duplicate_bills:
        warnings.append(
            f"bill '{bill.name}' repeats on {day_key(bill.date)} — "
            f"counted once (Rs {inr(bill.total)})"
        )

    warnings.extend(_repeated_blocks(text, bill_lines))

    if skipped:
        warnings.append(f"{len(skipped)} line(s) could not be read")
    return tuple(warnings)


def _repeated_blocks(text: str, bill_lines: set[str]) -> tuple[str, ...]:
    """Flag an identical run of lines that repeats immediately in one day.

    Repeated single lines are normal (five Rs 350 recharges is a real day).
    A block of four or more identical lines repeated back to back is a
    copy-paste artefact, so it is reported — but never removed, because the
    totals must stay exactly what the file says.  Bill item lines are skipped:
    they are counted through the bill's total, not line by line.
    """
    sections: dict[date | None, list[str]] = {}
    current: date | None = None
    in_summary = False
    for raw in text.splitlines():
        line = raw.strip()
        if _FENCE_RE.match(line):
            in_summary = not in_summary
            continue
        if in_summary or not line:
            continue
        parsed = parse_day(line)
        if parsed is not None:
            current = parsed
            continue
        if line.startswith("-") and line not in bill_lines:
            sections.setdefault(current, []).append(line)

    found: list[str] = []
    for day, lines in sections.items():
        for start in range(len(lines)):
            remaining = (len(lines) - start) // 2
            for size in range(4, remaining + 1):
                if lines[start : start + size] != lines[start + size : start + 2 * size]:
                    continue
                copy = lines[start + size : start + 2 * size]
                amounts = [_parse_sale_line(item) for item in copy]
                amount = sum(a.amount for a in amounts if a)
                pending = sum(a.pending for a in amounts if a)
                detail = f"Rs {inr(amount)}"
                if pending:
                    detail += f" (Rs {inr(pending)} pending)"
                found.append(
                    f"an identical {size}-line block repeats on {day_key(day)} — "
                    f"{detail}; check for a double entry"
                )
                break
    return tuple(found)


def _in_range(
    day: date | None, start: date | None, end: date | None
) -> bool:
    if start is None and end is None:
        return True
    if day is None:
        return False
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def totals(
    entries: Iterable[Sale],
    *,
    start: date | None = None,
    end: date | None = None,
) -> Totals:
    """Sum sales, the recharge slice, cash after recharges and pending."""
    picked = [s for s in entries if _in_range(s.date, start, end)]
    total = sum(s.amount for s in picked)
    recharges = sum(s.amount for s in picked if s.recharge)
    pending = sum(s.pending for s in picked)
    return Totals(
        entries=len(picked),
        total=total,
        recharges=recharges,
        cash_after_recharges=total - recharges,
        pending=pending,
        pending_entries=sum(1 for s in picked if s.pending > 0),
        collected=sum(s.collected for s in picked),
    )


def totals_by_day(
    entries: Iterable[Sale],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[date, Totals]:
    grouped: dict[date, list[Sale]] = {}
    for sale in entries:
        if sale.date is None or not _in_range(sale.date, start, end):
            continue
        grouped.setdefault(sale.date, []).append(sale)
    return {day: totals(items) for day, items in sorted(grouped.items())}


def find_sales(entries: Iterable[Sale], query: str) -> tuple[Sale, ...]:
    needle = query.strip().lower()
    if not needle:
        return ()
    return tuple(
        sale
        for sale in entries
        if needle in sale.name.lower()
    )


# ── stock ledger ──────────────────────────────────────────────────────────────


def parse_stock(text: str) -> StockLedger:
    """Parse the supplier ledger (``malik_list``).

    Quantity markers set ``quantity``.  A price is multiplied by the quantity
    only when the line says ``each`` or ``/piece``; otherwise the amount is
    read as the price for the whole line, which is how lots such as
    ``Charger (6 pieces) - Buy: Rs 1300`` are written.
    """
    items: list[StockItem] = []
    dates: list[date] = []
    skipped: list[str] = []
    warnings: list[str] = []
    recorded: RecordedTotals | None = None
    current: date | None = None
    pending_line: str | None = None
    summary: list[str] = []
    in_summary = False

    def flush_continuation() -> None:
        nonlocal pending_line
        if pending_line is None:
            return
        item = _parse_stock_line(pending_line, current)
        if item is None:
            skipped.append(pending_line)
        else:
            items.append(item)
        pending_line = None

    for raw in text.splitlines():
        line = raw.strip()

        if _FENCE_RE.match(line):
            if in_summary:
                recorded = _parse_recorded(summary) or recorded
                summary = []
            else:
                flush_continuation()
            in_summary = not in_summary
            continue

        if in_summary:
            summary.append(line)
            continue

        if not line:
            continue

        parsed_day = parse_day(line)
        if parsed_day is not None:
            flush_continuation()
            current = parsed_day
            if parsed_day not in dates:
                dates.append(parsed_day)
            continue

        if line.startswith(","):
            if pending_line is not None:
                pending_line = f"{pending_line} {line}"
            else:
                skipped.append(line)
            continue

        # A fresh item either carries its own "Buy:" price on one line, or is
        # a bare name that follows a completed row (the shop sometimes writes
        # the name and the price on separate lines).  A leading "," or "-"
        # always continues the line being built.
        starts_item = "buy" in line.lower() and " - " in line
        continues = line.startswith(",") or line.startswith("-")
        if pending_line is not None and not starts_item:
            if continues or "buy" not in pending_line.lower():
                pending_line = f"{pending_line} {line}"
                continue

        flush_continuation()
        pending_line = line

    flush_continuation()

    inferred = [item for item in items if item.sell_inferred]
    if inferred:
        warnings.append(
            f"{len(inferred)} row(s) had no 'Sell:' label — the trailing amount "
            "was read as the sell price"
        )
    unpriced = [item.name for item in items if item.buy_total == 0 and not item.has_sell]
    if unpriced:
        warnings.append(
            f"{len(unpriced)} row(s) have neither a buy nor a sell price: "
            f"{', '.join(sorted(set(unpriced))[:5])}"
        )
    if skipped:
        warnings.append(f"{len(skipped)} line(s) could not be read")

    return StockLedger(
        items=tuple(items),
        recorded=recorded,
        warnings=tuple(warnings),
        skipped=tuple(skipped),
        dates=tuple(sorted(set(dates))),
    )


def _parse_stock_line(line: str, day: date | None) -> StockItem | None:
    text = re.sub(r"\s+", " ", line).strip()
    if " - " in text:
        name, _, rest = text.partition(" - ")
    else:
        match = re.search(r"\s*-\s*buy\s*:", text, re.I)
        if not match:
            return None
        name, rest = text[: match.start()], text[match.start() + 1 :]
    name = name.strip()
    rest = rest.strip()
    if not name:
        return None

    buy_match = re.search(r"buy\s*:\s*(?:rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", rest, re.I)
    buy = _money(buy_match.group(1)) if buy_match else 0.0

    sell_match = re.search(
        r"sell\s*:\s*(?:rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", rest, re.I
    )
    sell_inferred = False
    if sell_match:
        sell = _money(sell_match.group(1))
    else:
        bare = re.findall(r",\s*(?:rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", rest, re.I)
        sell = _money(bare[-1]) if bare else 0.0
        sell_inferred = bool(bare)

    quantity = 1
    qty_match = _QTY_RE.search(name)
    if qty_match:
        quantity = int(qty_match.group(1))

    # "each" and "/piece" apply to the side of the comma they appear on:
    # ``Buy: Rs 190, Sell: Rs 100/piece`` is a lot price plus a per-piece sell.
    sell_at = re.search(r"sell\s*:", rest, re.I)
    if sell_at:
        split_at: int | None = sell_at.start()
    elif sell_inferred:
        # ``Buy: Rs 190, Rs 100/piece`` — the tail after the last comma is the
        # sell price and may still be marked per unit.
        split_at = rest.rfind(",")
    else:
        split_at = None
    buy_segment = rest[:split_at] if split_at is not None else rest
    sell_segment = rest[split_at:] if split_at is not None else ""
    sell_per_unit = bool(_PER_UNIT_RE.search(sell_segment)) and not _LUMP_RE.search(
        sell_segment
    )
    buy_per_unit = bool(_PER_UNIT_RE.search(buy_segment)) and not _LUMP_RE.search(
        buy_segment
    )

    return StockItem(
        date=day,
        name=name,
        quantity=quantity,
        buy=buy,
        sell=sell,
        buy_per_unit=buy_per_unit,
        sell_per_unit=sell_per_unit,
        sell_inferred=sell_inferred,
    )


def stock_totals(
    items: Iterable[StockItem],
    *,
    start: date | None = None,
    end: date | None = None,
) -> StockTotals:
    picked = [item for item in items if _in_range(item.date, start, end)]
    buy = sum(item.buy_total for item in picked)
    sell = sum(item.sell_total for item in picked)
    priced = [item for item in picked if item.has_sell]
    profit = sum(item.profit for item in priced)
    unsold = sum(item.buy_total for item in picked if not item.has_sell)
    return StockTotals(
        items=len(picked),
        buy=buy,
        sell=sell,
        expected_profit=profit,
        unsold_cost=unsold,
        priced_items=len(priced),
    )


def find_stock(items: Iterable[StockItem], query: str) -> tuple[StockItem, ...]:
    needle = query.strip().lower()
    if not needle:
        return ()
    return tuple(item for item in items if needle in item.name.lower())


# ── writing new lines ─────────────────────────────────────────────────────────


def sale_line(name: str, amount: float, *, pending: float = 0.0, quantity: int = 1) -> str:
    """Build a selling-ledger line in the same style as the existing ones."""
    clean = re.sub(r"\s+", " ", name.strip())
    if not clean:
        raise ValueError("item name is required")
    suffix = f" ({quantity} pcs)" if quantity > 1 else ""
    line = f"- {clean}{suffix}: Rs {inr(amount)}"
    pending = min(max(pending, 0.0), amount)
    if pending >= amount and amount > 0:
        line += " (Pending)"
    elif pending > 0:
        line += f" (Pending Rs {inr(pending)})"
    return line


def stock_line(
    name: str,
    buy: float,
    sell: float = 0.0,
    *,
    quantity: int = 1,
) -> str:
    """Build a stock-ledger line; ``buy``/``sell`` are per-unit when qty > 1."""
    clean = re.sub(r"\s+", " ", name.strip())
    if not clean:
        raise ValueError("item name is required")
    if quantity > 1:
        head = f"{clean} ({quantity} pieces)"
        line = f"{head} - Buy: Rs {inr(buy)} each"
        if sell > 0:
            line += f", Sell: Rs {inr(sell)} each"
        return line
    line = f"{clean} - Buy: Rs {inr(buy)}"
    if sell > 0:
        line += f", Sell: Rs {inr(sell)}"
    return line


def insert_entry(text: str, entry_line: str, day: date, *, dated_prefix: bool) -> str:
    """Insert ``entry_line`` under ``day``'s header, creating it if missing.

    The section is detected with the same date rule the parser uses, so a new
    line always lands where a human would have written it: at the end of that
    day's block.  When the day has no block yet, one is appended at the end of
    the file, matching the file's existing style.
    """
    header = f"Date: {day_key(day)}" if dated_prefix else day_key(day)
    lines = text.splitlines()
    target = None
    for index, raw in enumerate(lines):
        parsed = parse_day(raw.strip())
        if parsed == day:
            target = index

    if target is None:
        body = text.rstrip("\n")
        joiner = "\n\n" if body else ""
        return f"{body}{joiner}{header}\n\n{entry_line}\n"

    # End of the section: next date header, a fence, or a named bill block.
    end = len(lines)
    for index in range(target + 1, len(lines)):
        stripped = lines[index].strip()
        if parse_day(stripped) is not None or _FENCE_RE.match(stripped):
            end = index
            break
        if _BILL_HEADER_RE.match(stripped) and not _MONEY_RE.search(stripped):
            end = index
            break

    insert_at = end
    while insert_at > target + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    updated = [*lines[:insert_at], entry_line, *lines[insert_at:]]
    return "\n".join(updated).rstrip("\n") + "\n"


def parse_timestamp(raw: str) -> datetime:
    """Parse either ``dd/mm/yyyy`` or ``yyyy-mm-dd`` (used by the tool)."""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r} (use dd/mm/yyyy)")
