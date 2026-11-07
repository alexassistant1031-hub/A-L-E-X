"""
memory/transcript_hook.py

Captures both sides of a Gemini Live conversation for Alex's memory.

WHY THIS EXISTS:
- `session.on("user_input_transcribed")` fires when Gemini returns the user's
  speech transcription. Requires `input_audio_transcription` on RealtimeModel.
- `session.on("conversation_item_added")` fires for the assistant role with
  Gemini Live (LiveKit issues #2098, #3649 mean user items may not fire here).

APPROACH:
1. Enable both transcription configs on RealtimeModel explicitly.
2. Listen to BOTH `user_input_transcribed` (user) and
   `conversation_item_added` (assistant).
3. Guard `user_input_transcribed` with `is_final=True`.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.agents import AgentSession

    from memory.memory_manager import MemoryManager

logger = logging.getLogger("alex.memory.hook")


class TranscriptHook:
    """
    Attaches to an AgentSession and pipes both sides of the conversation
    into MemoryManager.

    Must be attached BEFORE session.start() is called.
    """

    def __init__(self, session: "AgentSession", memory: "MemoryManager") -> None:
        self._session = session
        self._memory = memory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attached = False

    def attach(self) -> None:
        """Register event listeners on the session. Call before session.start()."""
        if self._attached:
            return
        self._attached = True

        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            logger.error("No event loop — TranscriptHook cannot attach")
            return

        # ── USER SPEECH ────────────────────────────────────────────────────
        @self._session.on("user_input_transcribed")
        def _on_user(ev) -> None:
            try:
                if not getattr(ev, "is_final", True):
                    return
                transcript = (getattr(ev, "transcript", "") or "").strip()
                if transcript:
                    logger.debug("USER: %s", transcript[:80])
                    self._memory.schedule_capture("user", transcript)
            except Exception as e:
                logger.warning("_on_user error: %s", e)

        # ── AGENT SPEECH ───────────────────────────────────────────────────
        @self._session.on("conversation_item_added")
        def _on_item(ev) -> None:
            try:
                item = getattr(ev, "item", None)
                if item is None:
                    return
                if getattr(item, "role", "") != "assistant":
                    return

                text = ""
                if hasattr(item, "text_content"):
                    text = item.text_content or ""
                elif hasattr(item, "content"):
                    content = item.content
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = []
                        for c in content:
                            if isinstance(c, str):
                                parts.append(c)
                            elif hasattr(c, "text") and c.text:
                                parts.append(c.text)
                        text = " ".join(parts)

                text = text.strip()
                if text:
                    logger.debug("ALEX: %s", text[:80])
                    self._memory.schedule_capture("alex", text)
            except Exception as e:
                logger.warning("_on_item error: %s", e)

        logger.info("TranscriptHook attached to session")
