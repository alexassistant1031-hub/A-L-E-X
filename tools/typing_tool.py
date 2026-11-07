"""
tools/typing_tool.py

Exposes keyboard control to Alex as a LiveKit function tool.

The model can only reach the keyboard through `type_text`, and every call
passes through TypingGuard first (length limits, destructive-command
blocklist, confirmation policy, audit log).
"""

from __future__ import annotations

import logging
from typing import Literal

from livekit.agents import llm

from controller import ControllerError, get_config
from termviz import get_visualizer

from .keyboard_tool import KeyboardTool
from .safety import TypingGuard

logger = logging.getLogger("alex.tools.typing")

# Informational only: the real log is created by the controller with private
# permissions.  Kept as a module attribute so diagnostics can show the path.
try:
    AUDIT_PATH = str(get_config().audit_root / "typing.jsonl")
except Exception:  # configuration problems are reported when the guard runs
    AUDIT_PATH = "/var/log/alex-controller/typing.jsonl"

_guard = TypingGuard()
_keyboard = KeyboardTool()


@llm.function_tool
async def type_text(
    text: str,
    speed: Literal["fast", "normal", "slow", "search"] = "normal",
    click: str = "",
    confirmed: bool = False,
) -> str:
    """Type text at the cursor, press a key, or send a hotkey on the user's desktop.

    Use ONLY when the user explicitly asks you to type, write, press a key,
    copy/paste, save, or interact with a text field. Never use it for
    conversation, questions, or to "show" an answer.

    Text can be plain words, a named key ('enter', 'tab', 'backspace',
    'escape', 'up', 'down', 'left', 'right', 'delete', 'home', 'end',
    'f1'-'f12'), or a hotkey ('ctrl+c', 'ctrl+v', 'ctrl+s', 'alt+tab',
    'win+r').

    Any language works, including Hindi/Devanagari, Arabic, CJK and emoji.
    Non-Latin text is pasted through the clipboard, so it appears all at once
    rather than character by character.

    Confirmation protocol: if the tool replies that confirmation is required,
    ask the user out loud and wait for a clear yes. Only then call the tool
    again with confirmed=true. When the user directly asked for the typing,
    that request is already approval: pass confirmed=true immediately and do
    not ask twice.

    Meta-requests are conversation, not typing. If the user talks about this
    tool itself ("typing ka fix", "fix the typing", "why did you type that"),
    never type their words: ask what should change, or stage a code change.

    Args:
        text: What to type, or the key/hotkey to press.
        speed: Typing speed; 'normal' unless the user asks otherwise.
        click: Optional click before typing: 'x,y' pixels or 'center'.
        confirmed: Set true when the user explicitly asked for this typing.
    """
    allowed, reason = _guard.validate(text, click)
    if not allowed:
        try:
            _guard.record(text, speed, click, f"blocked: {reason}")
        except (ControllerError, OSError) as exc:
            logger.critical("Typing audit failed while blocking an action: %s", exc)
        return reason

    # Fail closed: no keystroke is emitted unless the secure audit log is
    # usable.  This replaces the old project-local memory/typing_audit.log.
    try:
        _guard.ensure_audit()
    except (ControllerError, OSError) as exc:
        logger.error("Refusing to type: %s", exc)
        return (
            "Error: the secure typing audit log is unavailable, so typing is "
            "disabled. Run `python -m controller init` to repair controller "
            "storage, then retry."
        )

    if _guard.requires_confirmation(text, click) and not confirmed:
        message = _guard.confirmation_message(text)
        logger.info("Typing confirmation requested for %d chars", len(text))
        try:
            _guard.record(text, speed, click, "confirmation requested")
        except (ControllerError, OSError) as exc:
            logger.critical("Typing audit failed during confirmation: %s", exc)
            return (
                "Error: confirmation was requested, but the secure audit log "
                "is unavailable. Refusing to continue."
            )
        return message

    # Record intent before typing.  If this fails we must not emit keys.
    try:
        _guard.record(text, speed, click, "requested")
    except (ControllerError, OSError) as exc:
        logger.error("Refusing to type because audit failed: %s", exc)
        return (
            "Error: the secure typing audit log is unavailable, so typing is "
            "disabled. Run `python -m controller init` to repair controller "
            "storage, then retry."
        )

    with get_visualizer().task("typing", f"{len(text)} chars"):
        result = await _keyboard.execute(text=text, speed=speed, click=click)
    try:
        _guard.record(text, speed, click, result)
    except (ControllerError, OSError) as exc:
        # The keystrokes already happened; surface the audit failure loudly.
        logger.critical("TYPING COMPLETED BUT AUDIT FAILED: %s", exc)
        return (
            f"{result}\n\nWARNING: the typing action completed but the secure "
            "audit log could not be written. Stop and check "
            f"{AUDIT_PATH}."
        )
    return result


TYPING_TOOLS = [type_text]

__all__ = ["type_text", "TYPING_TOOLS", "AUDIT_PATH"]
