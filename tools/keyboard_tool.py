"""
tools/keyboard_tool.py

Real keyboard control with multiple Linux backends:
  Wayland : wtype (preferred), ydotool
  X11     : pyautogui, xdotool

Ported from RIKO and cleaned up:
  - fixed the stray "0" that wtype typed before every string
  - honours typing speed on wtype/xdotool/ydotool, not only pyautogui
  - coordinate clicks work through every available backend
  - non-Latin/Unicode text is pasted through the clipboard, because every
    keycode backend silently drops characters missing from the keyboard
    layout (ydotool still exits 0, which used to look like success)
  - backends that keep failing are skipped instead of retried every call
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os as _os
import random
import re
import shutil
import subprocess
import time

from .base import BaseTool

logger = logging.getLogger("alex.tools.keyboard")

# ── Backend detection ─────────────────────────────────────────────────────────
_HAS_PYAUTOGUI = False
_HAS_XDOTOOL = shutil.which("xdotool") is not None
_HAS_WTYPE = shutil.which("wtype") is not None
_HAS_YDOTOOL = shutil.which("ydotool") is not None
_IS_WAYLAND = _os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

# Proper Xauthority detection (needed when launched from a service or cron).
_xauth = _os.environ.get("XAUTHORITY", "")
if not _xauth:
    _uid = _os.getuid()
    for p in ["~/.Xauthority", f"/run/user/{_uid}/.mutter-Xwaylandauth.*"]:
        expanded = _os.path.expanduser(p)
        if "*" in expanded:
            matches = glob.glob(expanded)
            if matches:
                _xauth = matches[0]
                break
        elif _os.access(expanded, _os.R_OK):
            _xauth = expanded
            break
if _xauth:
    _os.environ["XAUTHORITY"] = _xauth

try:
    if _os.environ.get("DISPLAY"):
        import pyautogui

        pyautogui.FAILSAFE = False
        _HAS_PYAUTOGUI = True
except Exception as e:  # no DISPLAY, missing Xlib, headless box …
    logger.debug("pyautogui init failed: %s", e)

FAST_TYPING = (0.008, 0.025)
NORMAL_TYPING = (0.025, 0.060)
SLOW_TYPING = (0.060, 0.120)
SEARCH_TYPING = (0.010, 0.030)

PAUSE_AFTER = {".", "!", "?", ",", ";", ":"}
PAUSE_EXTRA = (0.15, 0.35)

# wtype / xdotool key names for named keys.
_WTYPE_KEYS = {
    "enter": "Return", "return": "Return",
    "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "bksp": "BackSpace",
    "escape": "Escape", "esc": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "delete": "Delete", "del": "Delete",
    "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}

# Linux input event keycodes for ydotool.
_YDOTOOL_KEYS = {
    "enter": "28", "return": "28", "tab": "15", "space": "57",
    "backspace": "14", "escape": "1", "esc": "1",
    "up": "103", "down": "108", "left": "105", "right": "106",
    "delete": "111", "del": "111", "home": "102", "end": "107",
    "pageup": "104", "pagedown": "109",
}
_YDOTOOL_MODS = {"ctrl": "29", "alt": "56", "shift": "42", "super": "125", "win": "125"}
_YDOTOOL_KEYCODES = {
    "enter": "28", "tab": "15", "space": "57", "esc": "1", "escape": "1",
    "a": "30", "b": "48", "c": "46", "d": "32", "e": "18", "f": "33",
    "g": "34", "h": "35", "i": "23", "j": "36", "k": "37", "l": "38",
    "m": "50", "n": "49", "o": "24", "p": "25", "q": "16", "r": "19",
    "s": "31", "t": "20", "u": "22", "v": "47", "w": "17", "x": "45",
    "y": "21", "z": "44",
}

_MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "shift": "shift",
    "win": "win", "windows": "win", "super": "win", "meta": "win",
}

# Anything outside printable ASCII (plus newline/tab) cannot be produced by
# the keycode backends and has to go through the clipboard.
_NEEDS_CLIPBOARD = re.compile(r"[^\x20-\x7e\n\t\r]")

# The clipboard helper needs PyGObject, which lives in the system Python,
# not in the project venv.
_CLIPBOARD_HELPER = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "clipboard_helper.py"
)
_SYSTEM_PYTHON = "/usr/bin/python3"
_CLIPBOARD_OK: bool | None = None

# After this many consecutive failures a backend is skipped for the session.
_SKIP_AFTER = 3
_fail_streak: dict[str, int] = {}


async def _clipboard_ready() -> bool:
    """Check once whether the system Python can drive the clipboard."""
    global _CLIPBOARD_OK
    if _CLIPBOARD_OK is None:
        ok = _os.path.exists(_CLIPBOARD_HELPER) and _os.access(_SYSTEM_PYTHON, _os.X_OK)
        if ok:
            r = await _run(
                [
                    _SYSTEM_PYTHON,
                    "-c",
                    "import gi;gi.require_version('Gtk','3.0');"
                    "from gi.repository import Gtk",
                ],
                20,
            )
            ok = r is not None and r.returncode == 0
        _CLIPBOARD_OK = ok
        if not ok:
            logger.warning("clipboard backend unavailable: %s lacks PyGObject", _SYSTEM_PYTHON)
    return _CLIPBOARD_OK



async def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess | None:
    """Run a command off the event loop; return None on timeout/spawn failure."""
    try:
        return await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
        )
    except Exception as e:
        logger.debug("command failed %s: %s", cmd[0], e)
        return None


class KeyboardTool(BaseTool):
    name = "type"
    description = (
        "Type text at the current cursor with human-like rhythm, or send keys "
        "and hotkeys. Use only when the user explicitly asks you to type, "
        "write, press a key, copy/paste, or save."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Text to type, or a key/combo. Plain text: 'hello world'. "
                    "Named keys: 'enter', 'tab', 'backspace', 'escape', 'up', "
                    "'down', 'left', 'right', 'delete', 'home', 'end', 'f1'-'f12'. "
                    "Hotkeys: 'ctrl+c', 'ctrl+v', 'ctrl+s', 'ctrl+a', 'alt+tab', "
                    "'win+r'."
                ),
            },
            "speed": {
                "type": "string",
                "enum": ["fast", "normal", "slow", "search"],
                "description": "Typing speed. Default 'normal'.",
                "default": "normal",
            },
            "click": {
                "type": "string",
                "description": (
                    "Optional click before typing: 'x,y' in screen pixels, or "
                    "'center'. Leave empty to type at the current cursor."
                ),
                "default": "",
            },
        },
        "required": ["text"],
    }

    # ── entry point ───────────────────────────────────────────────────────────

    async def execute(self, text: str, speed: str = "normal", click: str = "") -> str:
        if not (_HAS_WTYPE or _HAS_YDOTOOL or _HAS_PYAUTOGUI or _HAS_XDOTOOL):
            return (
                "Error: no keyboard backend available. Install one of: "
                "wtype (Wayland), xdotool (X11), ydotool, or pyautogui."
            )

        try:
            if click:
                result = await self._do_click(click)
                if result and result.startswith("Error"):
                    return result
                if result is None:
                    return f"Error: invalid click target {click!r} (use 'x,y' or 'center')."

            if _NEEDS_CLIPBOARD.search(text):
                return await self._paste_text(text)

            if "\n" in text:
                return await self._type_lines(text, speed)

            lowered = text.strip().lower()

            if "+" in lowered:
                result = await self._do_hotkey(lowered)
                if result is not None:
                    return result

            key = self._match_special_key(lowered)
            if key:
                ok = await self._press_key(key)
                return f"Pressed: {key}" if ok else f"Error: failed to press {key}"

            return await self._type_human(text, speed)

        except Exception as e:
            logger.exception("keyboard execute failed")
            return f"Error: {e}"

    # ── text typing ───────────────────────────────────────────────────────────

    async def _type_human(self, text: str, speed: str) -> str:
        if not text:
            return "Error: nothing to type."

        delay_ms = self._delay_ms(speed)
        chars = len(text)
        started = time.monotonic()

        # Session-aware order: on Wayland, pyautogui only reaches XWayland
        # windows, so prefer compositor-level backends.
        for backend in self._backends():
            if backend == "wtype":
                r = await _run(["wtype", "-d", str(delay_ms), "--", text])
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return self._typed_message(chars, started)
                self._note_fail(backend, (r.stderr or "").strip()[:80] if r else "spawn failed")

            elif backend == "ydotool":
                r = await _run(["ydotool", "type", "--key-delay", str(delay_ms), "--", text])
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return self._typed_message(chars, started)
                self._note_fail(backend, (r.stderr or "").strip()[:80] if r else "spawn failed")

            elif backend == "pyautogui":
                try:
                    await self._pyautogui_type(text, speed)
                    self._note_ok(backend)
                    return self._typed_message(chars, started)
                except Exception as e:
                    self._note_fail(backend, str(e)[:80])

            elif backend == "xdotool":
                r = await _run(["xdotool", "type", "--delay", str(delay_ms), "--", text])
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return self._typed_message(chars, started)
                self._note_fail(backend, (r.stderr or "").strip()[:80] if r else "spawn failed")

        return "Error: all keyboard backends failed"

    def _typed_message(self, chars: int, started: float) -> str:
        elapsed = max(time.monotonic() - started, 0.001)
        wpm = max(1, int((chars / 5) / (elapsed / 60)))
        return f"Typed {chars} characters at ~{wpm} WPM"

    async def _paste_text(self, text: str) -> str:
        """Enter arbitrary Unicode: put it on the clipboard, then paste it.

        Keycode backends cannot emit characters that are not in the keyboard
        layout, so Hindi/CJK/emoji/etc. must go through the clipboard.
        """
        if not await _clipboard_ready():
            return (
                "Error: this text contains non-Latin characters, which need "
                "clipboard paste, but no clipboard backend is available. "
                f"Install PyGObject for {_SYSTEM_PYTHON} or the wl-clipboard package."
            )

        proc = await asyncio.create_subprocess_exec(
            _SYSTEM_PYTHON,
            _CLIPBOARD_HELPER,
            "--hold",
            "1.5",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if proc.stdin is None or proc.stdout is None:
                return "Error: could not start the clipboard helper."

            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            ready = False
            deadline = time.monotonic() + 8
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                if line.strip() == b"READY":
                    ready = True
                    break

            if not ready:
                return "Error: could not place the text on the clipboard."

            combo = await self._do_hotkey("ctrl+v")
            if not combo or combo.startswith("Error"):
                return f"Error: clipboard ready, but the paste shortcut failed ({combo})."

            await asyncio.sleep(0.2)
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                pass

            return f"Pasted {len(text)} characters (Unicode via clipboard)"
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass

    async def _pyautogui_type(self, text: str, speed: str) -> bool:
        base_delay = sum(self._speed_range(speed)) / 2

        chunks: list[tuple[str, str]] = []
        buf: list[str] = []
        for char in text:
            if char in PAUSE_AFTER:
                if buf:
                    chunks.append(("text", "".join(buf)))
                    buf = []
                chunks.append(("pause", char))
            else:
                buf.append(char)
        if buf:
            chunks.append(("text", "".join(buf)))

        for kind, value in chunks:
            if kind == "text" and value:
                await asyncio.to_thread(pyautogui.write, value, interval=base_delay)
            elif kind == "pause":
                await asyncio.sleep(random.uniform(*PAUSE_EXTRA))
        return True

    async def _type_lines(self, text: str, speed: str) -> str:
        lines = text.split("\n")
        failures: list[str] = []
        for i, line in enumerate(lines):
            if line:
                result = await self._type_human(line, speed)
                if result.startswith("Error"):
                    failures.append(result)
            if i < len(lines) - 1:
                if not await self._press_key("enter"):
                    failures.append("enter key")
                await asyncio.sleep(random.uniform(0.08, 0.2))
        if failures:
            return (
                f"Typed {len(text)} chars across {len(lines)} lines "
                f"(errors: {'; '.join(failures)})"
            )
        return f"Typed {len(text)} chars across {len(lines)} lines"

    # ── keys and hotkeys ──────────────────────────────────────────────────────

    async def _press_key(self, key: str) -> bool:
        for backend in self._backends():
            if backend == "wtype":
                r = await _run(["wtype", "-k", _WTYPE_KEYS.get(key, key)], 10)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return True
                self._note_fail(backend)
            elif backend == "ydotool":
                code = self._ydotool_code(_YDOTOOL_KEYS, key)
                if code is None:
                    continue
                # Explicit press/release: the bare `ydotool key 28` form can
                # leave the key held down in ydotool 1.0.x.
                r = await _run(["ydotool", "key", f"{code}:1", f"{code}:0"], 10)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return True
                self._note_fail(backend)
            elif backend == "pyautogui":
                try:
                    await asyncio.to_thread(pyautogui.press, key)
                    self._note_ok(backend)
                    return True
                except Exception:
                    self._note_fail(backend)
            elif backend == "xdotool":
                r = await _run(["xdotool", "key", key], 10)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return True
                self._note_fail(backend)
        return False

    async def _do_hotkey(self, text: str) -> str | None:
        parts = [p.strip() for p in text.split("+")]
        mods: list[str] = []
        key: str | None = None

        for part in parts:
            if part in _MOD_ALIASES:
                mods.append(_MOD_ALIASES[part])
            else:
                key = self._match_special_key(part) or part

        if not (mods and key):
            return None

        combo = "+".join(mods + [key])

        for backend in self._backends():
            if backend == "wtype":
                cmd = ["wtype"]
                for mod in mods:
                    cmd += ["-M", mod]
                cmd += ["-k", _WTYPE_KEYS.get(key, key)]
                r = await _run(cmd, 10)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return f"Hotkey: {combo}"
                self._note_fail(backend)
            elif backend == "ydotool":
                final = self._ydotool_code(_YDOTOOL_KEYCODES, key)
                codes = [self._ydotool_code(_YDOTOOL_MODS, m) for m in mods]
                if final is None or any(c is None for c in codes):
                    continue
                # Explicit events, mods released last: `ydotool key 29 47`
                # leaves modifiers stuck on ydotool 1.0.x, which then repeats
                # Ctrl+V (or emits a bare "v") into whatever window has focus.
                events = [f"{c}:1" for c in codes]
                events += [f"{final}:1", f"{final}:0"]
                events += [f"{c}:0" for c in reversed(codes)]
                r = await _run(["ydotool", "key", *events], 10)
                if r is None or r.returncode != 0:
                    # Never leave a modifier held down.
                    await _run(["ydotool", "key", "29:0", "56:0", "42:0", "125:0"], 5)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return f"Hotkey: {combo}"
                self._note_fail(backend)
            elif backend == "pyautogui":
                py_mods = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "win"}
                try:
                    await asyncio.to_thread(
                        pyautogui.hotkey, *[py_mods.get(m, m) for m in mods], key
                    )
                    self._note_ok(backend)
                    return f"Hotkey: {combo}"
                except Exception:
                    self._note_fail(backend)
            elif backend == "xdotool":
                r = await _run(["xdotool", "key", combo], 10)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return f"Hotkey: {combo}"
                self._note_fail(backend)

        return f"Error: hotkey {combo} failed on every backend"

    # ── mouse ─────────────────────────────────────────────────────────────────

    async def _do_click(self, click: str) -> str | None:
        click = click.strip().lower()

        if click == "center":
            coords = await self._screen_center()
            if coords is None:
                return "Error: could not determine screen size for a center click"
        else:
            match = re.match(r"^\s*(\d+)\s*,\s*(\d+)\s*$", click)
            if not match:
                return None
            coords = (int(match.group(1)), int(match.group(2)))

        x, y = coords

        for backend in self._backends():
            if backend == "pyautogui":
                try:
                    await asyncio.to_thread(pyautogui.click, x, y)
                    self._note_ok(backend)
                    return f"Clicked ({x}, {y})"
                except Exception:
                    self._note_fail(backend)
            elif backend == "ydotool":
                r = await _run(
                    ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], 5
                )
                r2 = await _run(["ydotool", "click", "0xC0"], 5)
                if r and r2 and r.returncode == 0 and r2.returncode == 0:
                    self._note_ok(backend)
                    return f"Clicked ({x}, {y})"
                self._note_fail(backend)
            elif backend == "xdotool":
                r = await _run(["xdotool", "mousemove", str(x), str(y), "click", "1"], 5)
                if r is not None and r.returncode == 0:
                    self._note_ok(backend)
                    return f"Clicked ({x}, {y})"
                self._note_fail(backend)

        return f"Error: click ({x}, {y}) failed (no working backend)"

    async def _screen_center(self) -> tuple[int, int] | None:
        if _HAS_PYAUTOGUI:
            try:
                w, h = pyautogui.size()
                return w // 2, h // 2
            except Exception:
                pass
        r = await _run(["xdotool", "getdisplaygeometry"], 5)
        if r is not None and r.returncode == 0:
            try:
                w, h = (int(v) for v in r.stdout.split())
                return w // 2, h // 2
            except Exception:
                pass
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _backends(self) -> list[str]:
        """Available backends in the order that makes sense for this session.

        ydotool comes first on Wayland: it drives the kernel uinput device, so
        it works on GNOME/Mutter, which does not implement the virtual-keyboard
        protocol wtype needs. On such compositors wtype fails *partially* --
        it can emit stray keys before erroring -- so it is only a fallback.
        """
        if _IS_WAYLAND:
            order = ["ydotool", "wtype", "pyautogui", "xdotool"]
        else:
            order = ["pyautogui", "xdotool", "wtype", "ydotool"]
        available = {
            "wtype": _HAS_WTYPE,
            "ydotool": _HAS_YDOTOOL,
            "pyautogui": _HAS_PYAUTOGUI,
            "xdotool": _HAS_XDOTOOL,
        }
        return [name for name in order if available[name] and self._usable(name)]

    @staticmethod
    def _ydotool_code(table: dict[str, str], key: str) -> str | None:
        """Numeric Linux keycode for `key`, or None when there is no mapping."""
        code = table.get(key, key)
        return code if code.isdigit() else None

    def _usable(self, backend: str) -> bool:
        return _fail_streak.get(backend, 0) < _SKIP_AFTER

    def _note_ok(self, backend: str) -> None:
        _fail_streak[backend] = 0

    def _note_fail(self, backend: str, detail: str = "") -> None:
        n = _fail_streak.get(backend, 0) + 1
        _fail_streak[backend] = n
        if n == 1:
            logger.warning(
                "%s failed%s; trying the next backend",
                backend,
                f" ({detail})" if detail else "",
            )
        elif n == _SKIP_AFTER:
            logger.warning(
                "%s failed %d times in a row; skipping it for this session",
                backend,
                n,
            )

    def _speed_range(self, speed: str) -> tuple[float, float]:
        return {
            "fast": FAST_TYPING,
            "normal": NORMAL_TYPING,
            "slow": SLOW_TYPING,
            "search": SEARCH_TYPING,
        }.get(speed, NORMAL_TYPING)

    def _delay_ms(self, speed: str) -> int:
        return max(1, int(sum(self._speed_range(speed)) / 2 * 1000))

    def _match_special_key(self, text: str) -> str | None:
        aliases = {
            "enter": "enter", "return": "enter",
            "tab": "tab", "space": "space",
            "backspace": "backspace", "bksp": "backspace",
            "escape": "esc", "esc": "esc",
            "up": "up", "down": "down", "left": "left", "right": "right",
            "delete": "delete", "del": "delete",
            "home": "home", "end": "end",
            "pageup": "pageup", "pagedown": "pagedown",
        }
        if text in aliases:
            return aliases[text]
        if re.fullmatch(r"f([1-9]|1[0-2])", text):
            return text
        return None


def backend_status() -> dict[str, bool | None]:
    """Diagnostics for `python main.py doctor` style checks."""
    return {
        "wayland": _IS_WAYLAND,
        "wtype": _HAS_WTYPE,
        "ydotool": _HAS_YDOTOOL,
        "pyautogui": _HAS_PYAUTOGUI,
        "xdotool": _HAS_XDOTOOL,
        "clipboard": _CLIPBOARD_OK,
    }
