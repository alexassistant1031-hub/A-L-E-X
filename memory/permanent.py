"""
memory/permanent.py

Facts Alex must never lose.

The operator designated one folder as the shop's complete record and asked
that Alex remember it for a lifetime.  That promise cannot depend on a row in
SQLite: consolidation, a rebuilt database or an instruction to "forget" could
remove a stored fact.  So these facts live in code and are layered three deep:

* they are rendered into the memory block first, and prompt trimming only ever
  cuts the tail, so a permanent fact is never the part that gets dropped;
* they are re-seeded into ``semantic_memory`` on every startup, so a deleted or
  rebuilt database heals itself;
* the root path itself is read from settings, never from a remembered row, so
  nothing the user (or the model) says can override it.

The inventory is read live from disk.  A new file inside the store therefore
shows up in Alex's memory at the next session without Alex having to be told
about it, and the root path stays fixed even if the inventory changes.

This module does no logging and no database work; it only reads the store
folder, so it is safe to import from tests and from the prompt builder.
"""

from __future__ import annotations

import os
from pathlib import Path

import settings

__all__ = [
    "INVENTORY_LIMIT",
    "store_root",
    "store_inventory",
    "permanent_facts",
    "render_block",
]

# A store with hundreds of files must not eat the prompt budget.  Everything
# beyond the limit is represented by a count, and the root rule still covers it.
INVENTORY_LIMIT = 40

_SKIP_DIRS = frozenset({"__pycache__", ".git", ".obsidian", ".trash"})


def store_root() -> Path:
    """Return the shop folder Alex must always know about."""
    return settings.store_root().expanduser()


def store_inventory(
    root: Path | None = None, *, limit: int = INVENTORY_LIMIT
) -> tuple[list[str], int]:
    """Return ``(relative paths, total)`` for the files inside the store.

    Directories are listed with a trailing ``/`` so the shape of the folder is
    visible.  Symlinks are skipped: the permanent promise is about this folder,
    not about wherever a link points.
    """
    base = Path(root) if root is not None else store_root()
    found: list[str] = []
    if not base.is_dir():
        return found, 0

    for current, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS
            and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )
        for name in dirnames:
            relative = (current_path / name).relative_to(base).as_posix()
            found.append(f"{relative}/")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path.relative_to(base).as_posix())

    total = len(found)
    if total > limit:
        return found[:limit], total
    return found, total


def permanent_facts(root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Return ``(category, text)`` pairs that must survive everything.

    The text is written to be useful to the model directly: each line says what
    the folder is, what "memory" means, and what is forbidden.
    """
    base = Path(root) if root is not None else store_root()
    return (
        (
            "permanent",
            f"Store folder (permanent): {base} — it holds the shop's complete "
            "records and Alex has full read and edit access to every file "
            "inside it.",
        ),
        (
            "permanent",
            "'Memory' means this store folder. When the user says memory, they "
            "mean these files, not a separate notebook or a new folder.",
        ),
        (
            "permanent",
            "Never create folders inside the store and never move its files "
            "out; work with the existing structure exactly as it is.",
        ),
        (
            "permanent",
            "Never forget this path or any file path inside it, even if the "
            "user asks to forget it or to clear memory. The operator made this "
            "permanent: say that plainly, then keep using the folder.",
        ),
        (
            "permanent",
            "Every change inside the store is backed up and written to the "
            "permanent controller audit log before the file changes.",
        ),
    )


def render_block(root: Path | None = None) -> str:
    """Render the permanent section of Alex's memory block."""
    base = Path(root) if root is not None else store_root()
    lines = [
        ">> PERMANENT — NEVER FORGET",
        f"  [Store] {base}",
        "  [Access] full read and edit access to every file inside it, forever.",
        "  [Meaning] \"memory\" = this store folder.",
        "  [Rule] never create folders there, never move files out, never",
        "         forget this path even if asked to.",
    ]

    inventory, total = store_inventory(base)
    if inventory:
        shown = f"{total} entries" if total <= len(inventory) else f"{total} entries, first {len(inventory)}"
        lines.append(f"  [Inventory] {shown} under {base}")
        for relative in inventory:
            lines.append(f"    - {relative}")
        if total > len(inventory):
            lines.append(f"    ... and {total - len(inventory)} more")
    else:
        lines.append(f"  [Inventory] folder is empty or missing: {base}")

    return "\n".join(lines)
