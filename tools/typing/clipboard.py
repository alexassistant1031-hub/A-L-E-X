#!/usr/bin/env python3
"""
tools/typing/clipboard.py

Put Unicode text on the desktop clipboard long enough for a paste, then put
the user's previous clipboard text back.

Why this exists: ydotool/wtype/xdotool type by keycode, so they can only emit
characters that exist in the current keyboard layout. Hindi, Arabic, CJK,
emoji and most accented letters cannot be typed that way -- ydotool silently
drops them and still exits 0. Pasting through the clipboard is the only
reliable way to enter arbitrary Unicode.

This script must run with the *system* Python (the one that has PyGObject),
not the project venv. It is driven by tools/typing/keyboard.py.

Protocol:
    stdin   the text to place on the clipboard
    stdout  one line, "READY", once the clipboard is owned and being served
    exits   after --hold seconds, restoring the previous text first
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hold",
        type=float,
        default=2.5,
        help="seconds to keep the new text on the clipboard (default: 2.5)",
    )
    parser.add_argument(
        "--restore",
        type=float,
        default=0.5,
        help="seconds to keep the restored text alive before exiting (default: 0.5)",
    )
    args = parser.parse_args()

    text = sys.stdin.read()
    if not text:
        print("ERROR: empty input", file=sys.stderr, flush=True)
        return 2

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    # Remember what the user had, but only if it was plain text. Images,
    # rich content and files cannot be restored this way.
    previous: str | None = None
    try:
        if clipboard.wait_is_text_available():
            previous = clipboard.wait_for_text()
    except Exception:
        previous = None

    state = {"stage": "hold"}

    def finish() -> bool:
        if state["stage"] == "hold":
            state["stage"] = "restore"
            if previous:
                try:
                    clipboard.set_text(previous, -1)
                except Exception:
                    pass
            GLib.timeout_add(int(args.restore * 1000), finish)
            return False
        Gtk.main_quit()
        return False

    def announce() -> bool:
        # Runs inside the main loop, so the selection owner can answer
        # paste requests immediately.
        clipboard.set_text(text, -1)
        print("READY", flush=True)
        GLib.timeout_add(int(args.hold * 1000), finish)
        return False

    GLib.idle_add(announce)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
