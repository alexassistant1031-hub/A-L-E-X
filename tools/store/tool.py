"""
tools/store/tool.py

Shop bookkeeping for Alex: read the two ledgers in ``~/Desktop/store``, work out
the hisaab, and append new entries.

Reads are unrestricted.  A write follows the same pipeline as every other Alex
write path:

    1. the file must be writable under the controller's write policy,
    2. a verified snapshot of the ledger is taken first,
    3. the intent is written to the controller-owned ``store`` audit stream and
       the write is refused if that fails (fail closed),
    4. the new text is written atomically,
    5. the outcome is audited with the snapshot name.

The arithmetic itself lives in :mod:`tools.store.ledger`, which does no I/O, so
the totals can be tested without files.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from pathlib import Path

import settings
from controller import (
    AuditError,
    ControllerConfig,
    ControllerError,
    ModificationController,
    PathGuardError,
    SecureAuditLog,
    WritePathGuard,
    get_audit,
    get_config,
)

from ..base import BaseTool
from memory.permanent import store_inventory
from . import ledger as ledger_mod
from .ledger import inr

logger = logging.getLogger("alex.tools.store")

_READ_ACTIONS = frozenset(
    {"summary", "day", "pending", "stock", "find", "files", "read_file"}
)
_WRITE_ACTIONS = frozenset(
    {"add_sale", "add_purchase", "write_file", "append_file"}
)

# A store file read for the model is capped: voice needs a summary, not a
# whole 600 KB export, and the cap keeps one call from flooding the prompt.
_READ_FILE_MAX_BYTES = 512 * 1024
_READ_REPLY_CHARS = 12000
_FILES_REPLY_LIMIT = 120


class StoreTool(BaseTool):
    """Shop ledger tool: totals, pending, stock and new entries."""

    name = "store"
    description = (
        "SHOP LEDGER TOOL — read and edit the shop's store folder. Computes "
        "the hisaab (total selling, recharges, cash after recharges, pending, "
        "stock cost, expected profit), searches items, adds a sale or a "
        "purchase, and reads or writes any file inside the store folder "
        "(orders, stock lists, ledgers) with a backup and an audit record."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "summary, day, pending, stock, find, files, read_file, "
                    "add_sale, add_purchase, write_file, append_file"
                ),
            },
            "name": {
                "type": "STRING",
                "description": "Item name (required for add_sale/add_purchase).",
            },
            "amount": {
                "type": "NUMBER",
                "description": (
                    "Amount received in rupees for add_sale — the total for the "
                    "entry, all pieces included."
                ),
            },
            "buy": {
                "type": "NUMBER",
                "description": "Purchase price in rupees for add_purchase (per piece when quantity > 1).",
            },
            "sell": {
                "type": "NUMBER",
                "description": "Selling price in rupees for add_purchase (per piece when quantity > 1).",
            },
            "pending": {
                "type": "NUMBER",
                "description": "Unpaid part of the amount, for add_sale. Default 0.",
            },
            "quantity": {
                "type": "INTEGER",
                "description": "Number of pieces. Default 1.",
            },
            "date": {
                "type": "STRING",
                "description": "dd/mm/yyyy. Defaults to today for writes, to the last recorded day for day.",
            },
            "query": {
                "type": "STRING",
                "description": "Item name to search in find.",
            },
            "start_date": {
                "type": "STRING",
                "description": "Optional dd/mm/yyyy lower bound for summary/stock.",
            },
            "end_date": {
                "type": "STRING",
                "description": "Optional dd/mm/yyyy upper bound for summary/stock.",
            },
            "path": {
                "type": "STRING",
                "description": (
                    "File inside the store, relative to the store folder "
                    "(for example 'Order Accessories/charging_order.txt'), "
                    "for read_file/write_file/append_file."
                ),
            },
            "content": {
                "type": "STRING",
                "description": "Full text for write_file, or the text to add for append_file.",
            },
            "append": {
                "type": "BOOLEAN",
                "description": "write_file only: append instead of replacing the file.",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        *,
        config: ControllerConfig | None = None,
        guard: WritePathGuard | None = None,
        audit: SecureAuditLog | None = None,
        modifications: ModificationController | None = None,
        selling_path: Path | None = None,
        stock_path: Path | None = None,
        root: Path | None = None,
    ) -> None:
        self.config = config or get_config()
        self.guard = guard or WritePathGuard(self.config)
        self._audit = audit
        self.modifications = modifications or ModificationController(
            self.config, guard=self.guard, audit=audit
        )
        self._selling_path = selling_path
        self._stock_path = stock_path
        self._root = root

    # ── paths ─────────────────────────────────────────────────────────────────

    @property
    def selling_path(self) -> Path:
        return self._selling_path or settings.store_selling_ledger()

    @property
    def stock_path(self) -> Path:
        return self._stock_path or settings.store_stock_ledger()

    def _resolve(self, path: Path) -> Path:
        """Use the canonical name, or the folder's single ``.txt`` file."""
        if path.exists():
            return path
        folder = path.parent
        if folder.is_dir():
            candidates = sorted(folder.glob("*.txt"))
            if len(candidates) == 1:
                return candidates[0]
        return path

    def _read(self, path: Path, label: str) -> str:
        path = self._resolve(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"{label} ledger not found at {path} "
                f"(set {settings.STORE_ENV_VAR} to the store folder)"
            )
        return path.read_text(encoding="utf-8")

    # ── arbitrary files inside the store ──────────────────────────────────────

    @property
    def root(self) -> Path:
        """The store folder: the one path Alex must always keep access to."""
        base = self._root or settings.store_root()
        return base.expanduser().resolve(strict=False)

    def _store_path(self, raw: str) -> Path:
        """Resolve ``raw`` inside the store, refusing any escape from it.

        Relative paths are relative to the store root, so the model can say
        ``Order Accessories/charging_order.txt``.  Symlinks are resolved first:
        a link inside the store that points outside is refused, because the
        permission is for this folder only.
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("a file path inside the store is required")
        root = self.root
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            # A plain ValueError, not PathGuardError: the same check runs for
            # reads, so "write denied" would be the wrong wording.
            raise ValueError(
                f"{text} is outside the store folder {root}; Alex may only "
                "read and edit files inside it"
            )
        return resolved

    def _relative(self, target: Path) -> str:
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return str(target)

    # ── audit ─────────────────────────────────────────────────────────────────

    def _audit_log(self) -> SecureAuditLog:
        return self._audit or get_audit("store", self.config)

    def _record(self, event: str, *, required: bool = False, **fields) -> bool:
        try:
            self._audit_log().write(event, **fields)
            return True
        except (ControllerError, OSError) as exc:
            if required:
                raise PathGuardError(
                    str(fields.get("path", "")),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure store audit write failed (%s): %s", event, exc)
            return False
        except Exception as exc:  # the audit sink must never crash the tool
            if required:
                raise PathGuardError(
                    str(fields.get("path", "")),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure store audit write failed (%s): %s", event, exc)
            return False

    # ── entry point ───────────────────────────────────────────────────────────

    async def execute(self, action: str, **kwargs) -> str:
        action = (action or "").strip().lower()
        handlers = {
            "summary": self._summary,
            "day": self._day,
            "pending": self._pending,
            "stock": self._stock,
            "find": self._find,
            "files": self._files,
            "read_file": self._read_file,
            "add_sale": self._add_sale,
            "add_purchase": self._add_purchase,
            "write_file": self._write_file,
            "append_file": self._append_file,
        }
        handler = handlers.get(action)
        if handler is None:
            return (
                f"Unknown store action '{action}'. Use one of: "
                f"{', '.join(sorted(handlers))}."
            )
        try:
            return await handler(**kwargs)
        except FileNotFoundError as exc:
            return f"STORE ERROR: {exc}"
        except (PathGuardError, AuditError) as exc:
            # A denied path or an unusable audit log both mean "not written".
            return f"STORE REFUSED: {exc}"
        except (ControllerError, ValueError) as exc:
            return f"STORE ERROR: {exc}"
        except OSError as exc:
            return f"STORE ERROR: could not access the ledger: {exc}"

    # ── reads ─────────────────────────────────────────────────────────────────

    def _window(
        self, start_date: str, end_date: str
    ) -> tuple[date_type | None, date_type | None]:
        start = _optional_day(start_date)
        end = _optional_day(end_date)
        if start and end and start > end:
            raise ValueError("start_date is after end_date")
        return start, end

    async def _summary(
        self,
        start_date: str = "",
        end_date: str = "",
        **_: object,
    ) -> str:
        start, end = self._window(start_date, end_date)
        ledger = ledger_mod.parse_selling(self._read(self.selling_path, "selling"))
        totals = ledger_mod.totals(ledger.entries, start=start, end=end)
        if not totals.entries:
            return "STORE: no sales recorded in that window."

        first = start or (ledger.dates[0] if ledger.dates else None)
        last = end or ledger.last_date
        lines = [
            f"STORE SUMMARY — {ledger_mod.day_key(first)} to {ledger_mod.day_key(last)}"
            f" ({totals.entries} sales)",
            f"Total selling: Rs {inr(totals.total)}",
            f"Recharges: Rs {inr(totals.recharges)}",
            f"Cash after recharges: Rs {inr(totals.cash_after_recharges)}",
            f"Pending: Rs {inr(totals.pending)} across {totals.pending_entries} entries",
            f"Collected: Rs {inr(totals.collected)}",
        ]

        recorded = ledger.recorded
        if recorded is not None and start is None and end is None:
            pairs = []
            for key, label in (
                ("total", "total"),
                ("recharges", "recharges"),
                ("pending", "pending"),
            ):
                value = recorded.get(key)
                if value is None:
                    continue
                computed = {
                    "total": totals.total,
                    "recharges": totals.recharges,
                    "pending": totals.pending,
                }[key]
                pairs.append(f"{label} {inr(value)} ({inr(computed - value)})")
            if pairs:
                lines.append("File's own Final Hisab: " + ", ".join(pairs))
                lines.append(
                    "The bracket is computed minus the hand-written figure; "
                    "the ledger lines are the source of truth."
                )

        if ledger.warnings:
            lines.append("NOTES:")
            lines.extend(f"- {warning}" for warning in ledger.warnings)
        return "\n".join(lines)

    async def _day(self, date: str = "", **_: object) -> str:  # noqa: A002
        ledger = ledger_mod.parse_selling(self._read(self.selling_path, "selling"))
        day = _optional_day(date) or ledger.last_date
        if day is None:
            return "STORE: the selling ledger has no dates yet."

        entries = [sale for sale in ledger.entries if sale.date == day]
        if not entries:
            return f"STORE: no sales recorded on {ledger_mod.day_key(day)}."

        totals = ledger_mod.totals(entries)
        lines = [
            f"DAY {ledger_mod.day_key(day)} — {totals.entries} sales",
            f"Total: Rs {inr(totals.total)} | Recharges: Rs {inr(totals.recharges)}"
            f" | Cash after recharges: Rs {inr(totals.cash_after_recharges)}"
            f" | Pending: Rs {inr(totals.pending)}",
            "",
        ]
        for sale in entries[:40]:
            detail = f"- {sale.name}: Rs {inr(sale.amount)}"
            if sale.pending:
                detail += f" (pending Rs {inr(sale.pending)})"
            lines.append(detail)
        if len(entries) > 40:
            lines.append(f"... and {len(entries) - 40} more")
        return "\n".join(lines)

    async def _pending(self, **_: object) -> str:
        ledger = ledger_mod.parse_selling(self._read(self.selling_path, "selling"))
        outstanding = [sale for sale in ledger.entries if sale.pending > 0]
        if not outstanding:
            return "STORE: nothing pending — every sale is settled."

        total = sum(sale.pending for sale in outstanding)
        lines = [f"PENDING — Rs {inr(total)} across {len(outstanding)} entries"]
        for sale in sorted(
            outstanding, key=lambda s: (s.date is None, s.date or date_type.min)
        ):
            lines.append(
                f"{ledger_mod.day_key(sale.date)}  {sale.name}  "
                f"Rs {inr(sale.pending)} of Rs {inr(sale.amount)}"
            )
        return "\n".join(lines)

    async def _stock(
        self,
        start_date: str = "",
        end_date: str = "",
        query: str = "",
        **_: object,
    ) -> str:
        start, end = self._window(start_date, end_date)
        ledger = ledger_mod.parse_stock(self._read(self.stock_path, "stock"))
        items = ledger_mod.find_stock(ledger.items, query) if query else ledger.items
        totals = ledger_mod.stock_totals(items, start=start, end=end)
        if not totals.items:
            return "STORE: no stock rows matched."

        first = start or (ledger.dates[0] if ledger.dates else None)
        last = end or ledger.last_date
        lines = [
            f"STOCK — {ledger_mod.day_key(first)} to {ledger_mod.day_key(last)}"
            f" ({totals.items} rows)",
            f"Bought: Rs {inr(totals.buy)}",
            f"Expected sell: Rs {inr(totals.sell)}",
            f"Expected profit on priced rows: Rs {inr(totals.expected_profit)}"
            f" ({totals.margin_pct:.0f}% on their cost)",
            f"Zero/unsold stock cost: Rs {inr(totals.unsold_cost)}",
        ]

        recorded = ledger.recorded
        if recorded is not None and not query and start is None and end is None:
            sell = recorded.get("total")
            buy = recorded.get("buy")
            parts = []
            if sell is not None:
                parts.append(f"sell {inr(sell)} ({inr(totals.sell - sell)})")
            if buy is not None:
                parts.append(f"buy {inr(buy)} ({inr(totals.buy - buy)})")
            if parts:
                lines.append("File's own Final Hisab: " + ", ".join(parts))
                lines.append(
                    "That buy figure is a 50k estimate, not a computed total."
                )

        if ledger.warnings and not query:
            lines.append("NOTES:")
            lines.extend(f"- {warning}" for warning in ledger.warnings)
        return "\n".join(lines)

    async def _find(self, query: str = "", **_: object) -> str:
        if not query.strip():
            return "STORE: give me an item name to search for."

        sales_ledger = ledger_mod.parse_selling(self._read(self.selling_path, "selling"))
        stock_ledger = ledger_mod.parse_stock(self._read(self.stock_path, "stock"))
        sales = ledger_mod.find_sales(sales_ledger.entries, query)
        stock = ledger_mod.find_stock(stock_ledger.items, query)

        if not sales and not stock:
            return f"STORE: nothing matching '{query.strip()}' in either ledger."

        lines: list[str] = []
        if sales:
            total = sum(sale.amount for sale in sales)
            lines.append(
                f"SOLD '{query.strip()}' — {len(sales)} line(s), Rs {inr(total)}"
            )
            for sale in sales[-15:]:
                lines.append(
                    f"{ledger_mod.day_key(sale.date)}  {sale.name}  "
                    f"Rs {inr(sale.amount)}"
                )
        if stock:
            totals = ledger_mod.stock_totals(stock)
            lines.append(
                f"STOCK '{query.strip()}' — {len(stock)} row(s), bought "
                f"Rs {inr(totals.buy)}, expected sell Rs {inr(totals.sell)}, "
                f"expected profit Rs {inr(totals.expected_profit)}"
            )
            for item in stock[-10:]:
                lines.append(
                    f"{ledger_mod.day_key(item.date)}  {item.name}  "
                    f"buy Rs {inr(item.buy_total)}, sell Rs {inr(item.sell_total)}"
                )
        return "\n".join(lines)

    # ── arbitrary files inside the store ──────────────────────────────────────

    async def _files(self, **_: object) -> str:
        root = self.root
        if not root.is_dir():
            raise FileNotFoundError(
                f"store folder not found at {root} "
                f"(set {settings.STORE_ENV_VAR} to the store folder)"
            )
        inventory, total = store_inventory(root, limit=_FILES_REPLY_LIMIT)
        if not inventory:
            return f"STORE FILES — the store is empty: {root}"
        lines = [f"STORE FILES — {root} ({total} entries)"]
        lines.extend(f"- {relative}" for relative in inventory)
        if total > len(inventory):
            lines.append(f"... and {total - len(inventory)} more")
        return "\n".join(lines)

    async def _read_file(self, path: str = "", **_: object) -> str:
        target = self._store_path(path)
        relative = self._relative(target)
        if target.is_dir():
            return (
                f"STORE: {relative} is a folder. Use action=\"files\" to list "
                "what is inside it."
            )
        if not target.exists():
            return f"STORE: no such file inside the store: {relative}"
        if not target.is_file():
            return f"STORE: {relative} is not a regular file."
        size = target.stat().st_size
        if size > _READ_FILE_MAX_BYTES:
            return (
                f"STORE: {relative} is {size} bytes, larger than the "
                f"{_READ_FILE_MAX_BYTES} byte read limit. Read it on the "
                "desktop, or ask for a smaller file."
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > _READ_REPLY_CHARS:
            text = (
                text[:_READ_REPLY_CHARS]
                + f"\n[...first {_READ_REPLY_CHARS} characters of {len(text)}]"
            )
        return f"FILE {relative} ({size} bytes)\n---\n{text}"

    # ── writes ────────────────────────────────────────────────────────────────

    async def _add_sale(
        self,
        name: str = "",
        amount: float | str = 0,
        pending: float | str = 0,
        quantity: int | str = 1,
        date: str = "",  # noqa: A002
        **_: object,
    ) -> str:
        clean_name = (name or "").strip()
        if not clean_name:
            return "STORE: an item name is required."
        value = _number(amount, "amount")
        if value <= 0:
            return "STORE: amount must be greater than zero."
        owed = _number(pending, "pending")
        if owed < 0 or owed > value:
            return "STORE: pending must be between 0 and the amount."
        pieces = _count(quantity)
        day = _optional_day(date) or date_type.today()

        line = ledger_mod.sale_line(
            clean_name, value, pending=owed, quantity=pieces
        )
        return await self._append(
            path=self.selling_path,
            label="selling",
            line=line,
            day=day,
            dated_prefix=True,
            action="add_sale",
            detail={
                "item": clean_name,
                "amount": value,
                "pending": owed,
                "quantity": pieces,
            },
        )

    async def _add_purchase(
        self,
        name: str = "",
        buy: float | str = 0,
        sell: float | str = 0,
        quantity: int | str = 1,
        date: str = "",  # noqa: A002
        **_: object,
    ) -> str:
        clean_name = (name or "").strip()
        if not clean_name:
            return "STORE: an item name is required."
        buy_price = _number(buy, "buy")
        sell_price = _number(sell, "sell")
        if buy_price < 0 or sell_price < 0:
            return "STORE: prices cannot be negative."
        if buy_price == 0 and sell_price == 0:
            return "STORE: give at least a buy price or a sell price."
        pieces = _count(quantity)
        day = _optional_day(date) or date_type.today()

        line = ledger_mod.stock_line(
            clean_name, buy_price, sell_price, quantity=pieces
        )
        return await self._append(
            path=self.stock_path,
            label="stock",
            line=line,
            day=day,
            dated_prefix=False,
            action="add_purchase",
            detail={
                "item": clean_name,
                "buy": buy_price,
                "sell": sell_price,
                "quantity": pieces,
            },
        )

    async def _write_file(
        self,
        path: str = "",
        content: str = "",
        append: bool | str = False,
        **_: object,
    ) -> str:
        """Create, replace or append one text file inside the store.

        The store is the only tree this action touches: the resolved path must
        stay inside the store root, the parent folder must already exist (Alex
        never creates folders there), and every change is snapshotted and
        audited before the file is written.
        """
        target = self._store_path(path)
        relative = self._relative(target)
        root = self.root
        if target == root:
            return "STORE: give a file path inside the store, not the folder itself."
        if target.is_dir():
            return f"STORE: {relative} is a folder, not a file."
        if not target.parent.is_dir():
            return (
                f"STORE REFUSED: the folder {self._relative(target.parent)} does "
                "not exist. Alex never creates folders inside the store; use an "
                "existing folder, or create it yourself on the desktop."
            )

        text = "" if content is None else str(content)
        append_mode = _truthy(append)
        if append_mode:
            if not text:
                return f"STORE: nothing to append to {relative}."
            if not text.endswith("\n"):
                text += "\n"

        action = "append_file" if append_mode else "write_file"

        # Policy first: a denied write must not leave a snapshot behind.
        self.guard.require_writable(target)
        snapshot = self.modifications.ensure_snapshot(
            action,
            [target],
            reason=f"store {action}",
            snapshot_external=True,
            coalesce=False,
        )

        # Fail closed: no audit trail, no write.
        self._record(
            "store_change_intent",
            required=True,
            action=action,
            path=str(target),
            relative=relative,
            bytes=len(text.encode("utf-8")),
            append=append_mode,
            snapshots=sorted(snapshot.names),
            reused=snapshot.reused,
        )

        try:
            self.modifications.write_text(target, text, append=append_mode)
        except Exception as exc:  # audit the failure, then report it
            self._record(
                "store_change_failed",
                action=action,
                path=str(target),
                relative=relative,
                error=str(exc)[:200],
            )
            logger.error("store %s failed for %s: %s", action, target, exc)
            raise

        self._record(
            "store_change_completed",
            action=action,
            path=str(target),
            relative=relative,
            bytes=len(text.encode("utf-8")),
            append=append_mode,
        )

        verb = "APPENDED TO" if append_mode else "WROTE"
        return "\n".join(
            [
                f"{verb} — {relative}",
                f"{len(text.encode('utf-8'))} bytes written",
                f"Backup: {', '.join(snapshot.names) or 'no prior file'}",
            ]
        )

    async def _append_file(
        self,
        path: str = "",
        content: str = "",
        **_: object,
    ) -> str:
        """Append text to a store file without replacing what is already there."""
        return await self._write_file(path=path, content=content, append=True)

    async def _append(
        self,
        *,
        path: Path,
        label: str,
        line: str,
        day: date_type,
        dated_prefix: bool,
        action: str,
        detail: dict,
    ) -> str:
        target = self._resolve(path)
        text = self._read(target, label)
        updated = ledger_mod.insert_entry(
            text, line, day, dated_prefix=dated_prefix
        )

        # Policy first: a denied write must not leave a snapshot behind.
        self.guard.require_writable(target)
        snapshot = self.modifications.ensure_snapshot(
            action,
            [target],
            reason=f"store {action}",
            snapshot_external=True,
            coalesce=False,
        )
        self.modifications.record(
            "safety_snapshot",
            action=action,
            paths=sorted(snapshot.names),
            reused=snapshot.reused,
        )

        # Fail closed: no audit trail, no write.
        self._record(
            "store_change_intent",
            required=True,
            action=action,
            path=str(target),
            **detail,
        )

        try:
            self.modifications.write_text(target, updated)
        except Exception as exc:  # audit the failure, then report it
            self._record(
                "store_change_failed",
                action=action,
                path=str(target),
                error=str(exc)[:200],
            )
            logger.error("store %s failed for %s: %s", action, target, exc)
            raise

        self._record(
            "store_change_completed",
            action=action,
            path=str(target),
            day=ledger_mod.day_key(day),
            **detail,
        )

        # The write is on disk and audited; nothing below may claim otherwise.
        summary = self._day_summary_after_write(target, day) if label == "selling" else ""
        result = [
            f"ADDED — {ledger_mod.day_key(day)} {label} ledger",
            line,
            f"Backup: {', '.join(snapshot.names) or 'no prior file'}",
        ]
        if summary:
            result.append(summary)
        return "\n".join(result)

    def _day_summary_after_write(self, target: Path, day: date_type) -> str:
        """Report the day's new total straight from the file that was written."""
        try:
            ledger = ledger_mod.parse_selling(target.read_text(encoding="utf-8"))
            totals = ledger_mod.totals(
                [sale for sale in ledger.entries if sale.date == day]
            )
        except (OSError, ValueError):
            return ""
        if not totals.entries:
            return ""
        return (
            f"Day {ledger_mod.day_key(day)} now: Rs {inr(totals.total)} "
            f"({totals.entries} sales, Rs {inr(totals.pending)} pending)"
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _optional_day(raw: str | None) -> date_type | None:
    text = (raw or "").strip()
    if not text:
        return None
    return ledger_mod.parse_timestamp(text).date()


def _number(raw: float | str | int, label: str) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "").replace("Rs", "").replace("₹", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, got {raw!r}") from exc


def _truthy(raw: bool | str | int) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _count(raw: int | str) -> int:
    if isinstance(raw, bool):
        return 1
    if isinstance(raw, int):
        return max(1, raw)
    text = str(raw).strip()
    if not text:
        return 1
    try:
        return max(1, int(float(text)))
    except ValueError as exc:
        raise ValueError(f"quantity must be a whole number, got {raw!r}") from exc
