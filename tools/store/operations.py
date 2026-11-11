"""
tools/store/operations.py

Exposes the shop ledger to Alex as a LiveKit function tool.

Reads (summary, day, pending, stock, find) are unrestricted.  Writes
(add_sale, add_purchase) go through the backup-first, audit-required path in
:class:`tools.store.tool.StoreTool`.
"""

from __future__ import annotations

import logging

from livekit.agents import llm

from termviz import get_visualizer

from .tool import StoreTool

from recovery import SafetyTag, execute_tool

logger = logging.getLogger("alex.tools.store_ops")

_store_tool = StoreTool()

_READ_ACTIONS = frozenset({"summary", "day", "pending", "stock", "find"})


@llm.function_tool
async def store_operation(
    action: str,
    name: str = "",
    amount: float = 0,
    buy: float = 0,
    sell: float = 0,
    pending: float = 0,
    quantity: int = 1,
    date: str = "",
    query: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Read the shop's ledgers and add today's entries.

    The shop keeps two plain-text ledgers: daily sales
    (``store_selling_list/selling_list.txt``) and supplier purchases
    (``malik_list/malik_list.txt``). Use this tool for anything about the
    shop's money instead of reading the files by hand.

    Actions:
        summary      - total selling, recharges, cash after recharges, pending,
                       and how far the computed figures drift from the
                       hand-written Final Hisab block. Optional range.
        day          - one day's sales and that day's totals. Defaults to the
                       most recent day in the ledger.
        pending      - every unpaid amount, with date and item.
        stock        - what was bought, expected selling value, expected
                       profit, and the cost of stock with no sell price.
        find         - search an item across sales and stock, with totals.
        add_sale     - record a sale: `name`, `amount` (total rupees),
                       optional `pending` rupees, `quantity`, `date`.
        add_purchase - record a purchase: `name`, `buy`, optional `sell`,
                       `quantity`, `date`.

    Amounts are plain rupees, not paise. Dates are ``dd/mm/yyyy``; writes
    default to today, ``day`` defaults to the newest day on file. Every write
    takes a verified backup of the ledger and is written to the controller
    audit log before the file changes.

    Args:
        action: summary, day, pending, stock, find, add_sale or add_purchase.
        name: Item name for add_sale / add_purchase.
        amount: Total rupees received for add_sale.
        buy: Purchase price for add_purchase (per piece when quantity > 1).
        sell: Selling price for add_purchase (per piece when quantity > 1).
        pending: Unpaid part of a sale, in rupees.
        quantity: Number of pieces, default 1.
        date: dd/mm/yyyy for day and the add_* actions.
        query: Item name to search for `find`.
        start_date: Optional dd/mm/yyyy lower bound for summary / stock.
        end_date: Optional dd/mm/yyyy upper bound for summary / stock.
    """
    label = name or action
    tag = SafetyTag.READ if action in _READ_ACTIONS else SafetyTag.WRITE_IRREVERSIBLE
    with get_visualizer().task(f"store.{action}", label):
        result = await execute_tool(
            tool="store",
            operation=action,
            safety_tag=tag,
            func=_store_tool.execute,
            error_prefixes=(),
            action=action,
            name=name,
            amount=amount,
            buy=buy,
            sell=sell,
            pending=pending,
            quantity=quantity,
            date=date,
            query=query,
            start_date=start_date,
            end_date=end_date,
        )
    logger.info("store_operation %s %s -> %s", action, label, str(result)[:80])
    return result


STORE_TOOLS = [store_operation]

__all__ = ["store_operation", "STORE_TOOLS"]
