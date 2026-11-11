"""Tests for the shop ledger tool and its pure arithmetic.

Everything runs on temporary files: the real ``~/Desktop/store`` ledgers are
never read, written or backed up by the suite.
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from controller import AuditError

from tests.support import EnvMixin, read_jsonl
from tools.store import ledger as L
from tools.store.tool import StoreTool

SELLING = """\
Date: 01/11/2026

- Xerox: Rs 10
- Jio Recharge: Rs 350
- Home Theater Repairing: Total Rs 700 (Paid Rs 500, Pending Rs 200)
- Half Photo: (Price not specified)

Date: 02/11/2026
- Tempered Glass (2 pcs): Rs 200
- 4A Full Photo (2 pcs): Rs 60 each
- CMF Neckband Pro: Rs 500 (Pending)
- Airtel Recharge: Rs 310 (Pending Rs 40)
- Table Fan Repair: Pending Rs 50 paid (Fully Paid)

Keshav Kaka Mobile List:
- Combo: Rs 1700
- Outer Key: Rs 200
Total Bill: Rs 1900
Paid: Rs 1000
Pending: Rs 900

--------------------------------------------------
Final Hisab (01/11/2026 - 02/11/2026):
Total Selling: Rs 6,000
Total Recharges: Rs 700
Cash After Recharges: Rs 5,300
Total Pending: Rs 1,140
--------------------------------------------------
"""

STOCK = """\
01/09/2026
Realme C11 outer key - Buy: Rs 40, Sell: Rs 250
Y12 glass guard (10 pieces) - Buy: Rs 190, Sell: Rs 100/piece
Charger (6 pieces) - Buy: Rs 1300
Samsung J5 Prime combo - Buy: Rs 0
, Sell: Rs 2000
KGF combo Virat (2 pieces)
 - Buy: Rs 1280 (total for 2)

02/09/2026
Oppo A57 combo (2 pieces) - Buy: Rs 640 each, Sell: Rs 2000 each

--------------------------------------------------
Final Hisab (50k Estimate):
Total Selling Price of Items: Rs 4,000
Estimate Buy Price: Rs 50,000
Total Profit: Rs 62,712
--------------------------------------------------
"""


class LedgerArithmeticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = L.parse_selling(SELLING)

    def test_dates_and_entries_are_read(self) -> None:
        self.assertEqual(self.ledger.dates, (date(2026, 11, 1), date(2026, 11, 2)))
        self.assertEqual(self.ledger.last_date, date(2026, 11, 2))
        names = {sale.name for sale in self.ledger.entries}
        self.assertIn("Xerox", names)
        self.assertIn("Tempered Glass (2 pcs)", names)

    def test_summary_block_is_never_counted_as_sales(self) -> None:
        self.assertNotIn(
            "Final Hisab", " ".join(sale.name for sale in self.ledger.entries)
        )
        self.assertEqual(self.ledger.recorded.get("total"), 6000.0)

    def test_all_documented_pending_forms(self) -> None:
        by_name = {sale.name: sale for sale in self.ledger.entries}
        self.assertEqual(by_name["Home Theater Repairing"].pending, 200.0)
        self.assertEqual(by_name["CMF Neckband Pro"].pending, 500.0)
        self.assertEqual(by_name["Airtel Recharge"].pending, 40.0)
        # "Pending Rs 50 paid (Fully Paid)" is settled, not outstanding.
        self.assertEqual(by_name["Table Fan Repair"].pending, 0.0)

    def test_each_and_multi_piece_lines(self) -> None:
        by_name = {sale.name: sale for sale in self.ledger.entries}
        self.assertEqual(by_name["4A Full Photo (2 pcs)"].amount, 120.0)
        self.assertEqual(by_name["Tempered Glass (2 pcs)"].amount, 200.0)

    def test_unpriced_entry_is_kept_but_warned(self) -> None:
        unpriced = [s for s in self.ledger.entries if s.amount == 0]
        self.assertEqual(len(unpriced), 1)
        self.assertTrue(any("no price recorded" in w for w in self.ledger.warnings))

    def test_recharges_are_separated_from_counter_sales(self) -> None:
        totals = L.totals(self.ledger.entries)
        self.assertEqual(totals.recharges, 660.0)  # 350 + 310
        self.assertEqual(totals.cash_after_recharges, totals.total - 660.0)

    def test_bill_block_is_counted_once_by_its_total(self) -> None:
        bill = self.ledger.bills[0]
        self.assertEqual(bill.name, "Keshav Kaka Mobile List")
        self.assertEqual(bill.total, 1900.0)
        self.assertEqual(bill.pending, 900.0)
        named = [s for s in self.ledger.entries if s.source == "bill"]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0].amount, 1900.0)

    def test_duplicate_bill_is_counted_once(self) -> None:
        duplicated = SELLING.replace(
            "Keshav Kaka Mobile List:\n- Combo: Rs 1700\n- Outer Key: Rs 200\n"
            "Total Bill: Rs 1900\nPaid: Rs 1000\nPending: Rs 900\n",
            "Keshav Kaka Mobile List:\n- Combo: Rs 1700\n- Outer Key: Rs 200\n"
            "Total Bill: Rs 1900\nPaid: Rs 1000\nPending: Rs 900\n\n"
            "Keshav Kaka Mobile List:\n- Combo: Rs 1700\n- Outer Key: Rs 200\n",
        )
        ledger = L.parse_selling(duplicated)
        self.assertEqual(len(ledger.bills), 1)
        self.assertTrue(any("repeats on 02/11/2026" in w for w in ledger.warnings))
        self.assertEqual(
            sum(s.amount for s in ledger.entries if s.source == "bill"), 1900.0
        )

    def test_repeated_block_is_reported_without_changing_totals(self) -> None:
        block = "- Xerox: Rs 10\n- Black Tape: Rs 20\n- 2 Pin: Rs 20\n- OTG: Rs 20\n"
        text = f"Date: 03/11/2026\n{block}{block}"
        ledger = L.parse_selling(text)
        totals = L.totals(ledger.entries)
        self.assertEqual(totals.total, 140.0)  # reported, never silently removed
        self.assertTrue(
            any("identical 4-line block repeats" in w for w in ledger.warnings)
        )

    def test_repeated_single_lines_are_normal(self) -> None:
        text = "Date: 03/11/2026\n" + "- Jio Recharge: Rs 350\n" * 5
        ledger = L.parse_selling(text)
        self.assertEqual(L.totals(ledger.entries).total, 1750.0)
        self.assertFalse(any("block repeats" in w for w in ledger.warnings))

    def test_totals_window(self) -> None:
        first_day = L.totals(
            self.ledger.entries, end=date(2026, 11, 1)
        )
        self.assertEqual(
            first_day.total,
            sum(s.amount for s in self.ledger.entries if s.date == date(2026, 11, 1)),
        )
        self.assertEqual(L.totals(self.ledger.entries, start=date(2026, 11, 3)).entries, 0)

    def test_total_drift_against_recorded_summary(self) -> None:
        recorded = self.ledger.recorded
        computed = L.totals(self.ledger.entries)
        self.assertEqual(recorded.get("recharges"), 700.0)
        self.assertNotEqual(computed.recharges, recorded.get("recharges"))

    # ── stock ─────────────────────────────────────────────────────────────────

    def test_stock_rows_and_quantities(self) -> None:
        ledger = L.parse_stock(STOCK)
        names = [item.name for item in ledger.items]
        self.assertEqual(len(ledger.items), 6, names)
        y12 = next(item for item in ledger.items if item.name.startswith("Y12"))
        self.assertEqual((y12.quantity, y12.buy_total, y12.sell_total), (10, 190.0, 1000.0))

    def test_stock_lot_price_is_not_multiplied(self) -> None:
        ledger = L.parse_stock(STOCK)
        charger = next(item for item in ledger.items if item.name.startswith("Charger"))
        self.assertEqual((charger.quantity, charger.buy_total), (6, 1300.0))
        kgf = next(item for item in ledger.items if item.name.startswith("KGF"))
        self.assertEqual(kgf.buy_total, 1280.0)

    def test_stock_per_piece_prices_multiply(self) -> None:
        ledger = L.parse_stock(STOCK)
        oppo = next(item for item in ledger.items if item.name.startswith("Oppo"))
        self.assertEqual((oppo.buy_total, oppo.sell_total), (1280.0, 4000.0))

    def test_stock_continuation_line_joins_its_item(self) -> None:
        ledger = L.parse_stock(STOCK)
        samsung = next(item for item in ledger.items if item.name.startswith("Samsung"))
        self.assertEqual(samsung.sell_total, 2000.0)
        self.assertEqual(samsung.quantity, 1)

    def test_per_piece_sell_without_a_sell_label(self) -> None:
        ledger = L.parse_stock(
            "01/09/2026\nY12 glass guard (10 pieces) - Buy: Rs 190, Rs 100/piece\n"
        )
        item = ledger.items[0]
        self.assertEqual((item.buy_total, item.sell_total), (190.0, 1000.0))
        self.assertTrue(item.sell_inferred)
        self.assertTrue(any("no 'Sell:' label" in w for w in ledger.warnings))

    def test_stock_totals_exclude_unpriced_rows_from_profit(self) -> None:
        totals = L.stock_totals(L.parse_stock(STOCK).items)
        self.assertEqual(totals.buy, 4090.0)
        self.assertEqual(totals.sell, 7250.0)
        self.assertEqual(totals.expected_profit, 5740.0)
        self.assertEqual(totals.unsold_cost, 2580.0)

    def test_stock_recorded_summary_is_parsed(self) -> None:
        recorded = L.parse_stock(STOCK).recorded
        self.assertEqual(recorded.get("total"), 4000.0)
        self.assertEqual(recorded.get("buy"), 50000.0)

    # ── writing lines ─────────────────────────────────────────────────────────

    def test_indian_amount_formatting(self) -> None:
        self.assertEqual(L.inr(112712), "1,12,712")
        self.assertEqual(L.inr(2000), "2,000")
        self.assertEqual(L.inr(0), "0")
        self.assertEqual(L.inr(1234.5), "1,234.50")

    def test_sale_line_shapes(self) -> None:
        self.assertEqual(L.sale_line("Xerox", 10), "- Xerox: Rs 10")
        self.assertEqual(
            L.sale_line("Neckband", 500, pending=500), "- Neckband: Rs 500 (Pending)"
        )
        self.assertEqual(
            L.sale_line("Airtel Recharge", 310, pending=40),
            "- Airtel Recharge: Rs 310 (Pending Rs 40)",
        )
        self.assertEqual(
            L.sale_line("Tempered Glass", 200, quantity=2),
            "- Tempered Glass (2 pcs): Rs 200",
        )

    def test_stock_line_shapes(self) -> None:
        self.assertEqual(
            L.stock_line("BG5 Battery", 580, 1250),
            "BG5 Battery - Buy: Rs 580, Sell: Rs 1,250",
        )
        self.assertEqual(
            L.stock_line("Oppo A57 combo", 640, 2000, quantity=2),
            "Oppo A57 combo (2 pieces) - Buy: Rs 640 each, Sell: Rs 2,000 each",
        )

    def test_insert_into_existing_day(self) -> None:
        text = L.insert_entry(
            SELLING, "- Repair: Rs 300", date(2026, 11, 1), dated_prefix=True
        )
        ledger = L.parse_selling(text)
        added = [s for s in ledger.entries if s.name == "Repair"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].date, date(2026, 11, 1))
        # It landed in the 01/11 block, before the 02/11 header.
        lines = text.splitlines()
        self.assertLess(
            lines.index("- Repair: Rs 300"),
            lines.index("Date: 02/11/2026"),
        )

    def test_insert_goes_before_a_named_bill(self) -> None:
        text = L.insert_entry(
            SELLING, "- Repair: Rs 300", date(2026, 11, 2), dated_prefix=True
        )
        lines = text.splitlines()
        self.assertLess(
            lines.index("- Repair: Rs 300"),
            lines.index("Keshav Kaka Mobile List:"),
        )
        self.assertEqual(
            sum(1 for s in L.parse_selling(text).entries if s.source == "bill"), 1
        )

    def test_insert_creates_a_new_day(self) -> None:
        text = L.insert_entry(
            SELLING, "- Repair: Rs 300", date(2026, 11, 9), dated_prefix=True
        )
        ledger = L.parse_selling(text)
        self.assertEqual(ledger.last_date, date(2026, 11, 9))
        self.assertEqual(L.totals(ledger.entries, start=date(2026, 11, 9)).total, 300.0)


class StoreToolTests(EnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.work / "store"
        self.selling = self.store / "store_selling_list" / "selling_list.txt"
        self.stock = self.store / "malik_list" / "malik_list.txt"
        self.selling.parent.mkdir(parents=True)
        self.stock.parent.mkdir(parents=True)
        self.selling.write_text(SELLING, encoding="utf-8")
        self.stock.write_text(STOCK, encoding="utf-8")
        self.tool = StoreTool(
            config=self.config,
            selling_path=self.selling,
            stock_path=self.stock,
        )
        self.audit_path = self.config.audit_root / "store.jsonl"

    def _events(self) -> list[dict]:
        return read_jsonl(self.audit_path)

    async def test_summary_reports_totals_and_drift(self) -> None:
        result = await self.tool.execute("summary")
        self.assertIn("Total selling", result)
        self.assertIn("Recharges", result)
        self.assertIn("Cash after recharges", result)
        self.assertIn("File's own Final Hisab", result)

    async def test_day_defaults_to_newest_day(self) -> None:
        result = await self.tool.execute("day")
        self.assertIn("02/11/2026", result)
        self.assertIn("Keshav Kaka Mobile List", result)

    async def test_pending_lists_every_unpaid_entry(self) -> None:
        result = await self.tool.execute("pending")
        self.assertIn("CMF Neckband Pro", result)
        self.assertIn("Rs 1,640", result)  # 200 + 500 + 40 + 900 (the bill)
        self.assertNotIn("Table Fan Repair", result)  # fully paid

    async def test_stock_reports_expected_profit(self) -> None:
        result = await self.tool.execute("stock")
        self.assertIn("Bought: Rs 4,090", result)
        self.assertIn("Expected profit", result)
        self.assertIn("Zero/unsold stock cost: Rs 2,580", result)

    async def test_find_searches_both_ledgers(self) -> None:
        result = await self.tool.execute("find", query="glass")
        self.assertIn("SOLD", result)
        self.assertIn("STOCK", result)

    async def test_unknown_action_is_reported(self) -> None:
        result = await self.tool.execute("nonsense")
        self.assertIn("Unknown store action", result)

    async def test_missing_ledger_is_reported(self) -> None:
        tool = StoreTool(
            config=self.config,
            selling_path=self.store / "nope" / "selling_list.txt",
            stock_path=self.stock,
        )
        result = await tool.execute("summary")
        self.assertIn("STORE ERROR", result)
        self.assertIn("ALEX_STORE_ROOT", result)

    async def test_add_sale_writes_backs_up_and_audits(self) -> None:
        result = await self.tool.execute(
            "add_sale", name="Tempered Glass", amount=200, quantity=2
        )
        self.assertIn("ADDED", result)
        self.assertIn("Backup:", result)

        text = self.selling.read_text(encoding="utf-8")
        self.assertIn("- Tempered Glass (2 pcs): Rs 200", text)
        self.assertTrue(
            list((self.config.backup_root / "paths").glob("*.tar.gz")),
            "a snapshot of the ledger is required before the write",
        )

        events = [event["event"] for event in self._events()]
        self.assertIn("store_change_intent", events)
        self.assertIn("store_change_intent", events)
        self.assertIn("store_change_completed", events)

    async def test_add_sale_respects_pending_and_report_new_day_total(self) -> None:
        result = await self.tool.execute(
            "add_sale", name="Neckband", amount=600, pending=600
        )
        self.assertIn("(Pending)", result)
        self.assertIn("now:", result)
        ledger = L.parse_selling(self.selling.read_text(encoding="utf-8"))
        added = [s for s in ledger.entries if s.name == "Neckband"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].pending, 600.0)

    async def test_add_sale_rejects_bad_input(self) -> None:
        for kwargs in (
            {"name": "", "amount": 100},
            {"name": "X", "amount": 0},
            {"name": "X", "amount": 100, "pending": 500},
        ):
            with self.subTest(**kwargs):
                before = self.selling.read_text(encoding="utf-8")
                result = await self.tool.execute("add_sale", **kwargs)
                self.assertIn("STORE:", result)
                self.assertEqual(self.selling.read_text(encoding="utf-8"), before)

    async def test_add_purchase_writes_per_piece_prices(self) -> None:
        result = await self.tool.execute(
            "add_purchase", name="Oppo A57 combo", buy=640, sell=2000, quantity=2
        )
        self.assertIn("ADDED", result)
        text = self.stock.read_text(encoding="utf-8")
        self.assertIn(
            "Oppo A57 combo (2 pieces) - Buy: Rs 640 each, Sell: Rs 2,000 each",
            text,
        )
        ledger = L.parse_stock(text)
        item = ledger.items[-1]
        self.assertEqual((item.buy_total, item.sell_total), (1280.0, 4000.0))

    async def test_write_fails_closed_when_audit_is_unavailable(self) -> None:
        class BrokenAudit:
            def write(self, *args, **kwargs):
                raise AuditError("audit disk is full")

        tool = StoreTool(
            config=self.config,
            audit=BrokenAudit(),
            selling_path=self.selling,
            stock_path=self.stock,
        )
        before = self.selling.read_text(encoding="utf-8")
        result = await tool.execute("add_sale", name="Xerox", amount=10)
        self.assertIn("STORE REFUSED", result)
        self.assertEqual(self.selling.read_text(encoding="utf-8"), before)

    async def test_write_outside_policy_is_refused(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        ledger = outside / "selling_list.txt"
        ledger.write_text(SELLING, encoding="utf-8")
        tool = StoreTool(
            config=self.config,
            selling_path=ledger,
            stock_path=self.stock,
        )
        before = ledger.read_text(encoding="utf-8")
        result = await tool.execute("add_sale", name="Xerox", amount=10)
        self.assertIn("STORE REFUSED", result)
        self.assertEqual(ledger.read_text(encoding="utf-8"), before)

    async def test_reads_work_outside_the_write_policy(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        ledger = outside / "selling_list.txt"
        ledger.write_text(SELLING, encoding="utf-8")
        tool = StoreTool(
            config=self.config,
            selling_path=ledger,
            stock_path=self.stock,
        )
        result = await tool.execute("summary")
        self.assertIn("Total selling", result)

    async def test_falls_back_to_the_single_txt_in_the_folder(self) -> None:
        renamed = self.selling.with_name("renamed.txt")
        self.selling.rename(renamed)
        result = await self.tool.execute("summary")
        self.assertIn("Total selling", result)
        self.assertTrue(renamed.exists())

    async def test_audit_chain_stays_valid(self) -> None:
        await self.tool.execute("add_sale", name="Xerox", amount=10)
        from controller import SecureAuditLog

        ok, count, message = SecureAuditLog(self.audit_path, fsync=False).verify()
        self.assertTrue(ok, message)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
