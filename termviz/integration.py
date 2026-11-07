"""Make termviz co-exist with LiveKit's console interface.

LiveKit's console mode owns the terminal: it runs its own Rich ``Live`` region
(the microphone waveform HUD) and installs its own log handler.  Starting a
second ``Live`` on the same console makes the two garble each other, so the
visualizer must *join* the HUD instead of replacing it.

``patch_console_waveform`` swaps LiveKit's ``FrequencyVisualizer`` for a thin
subclass whose ``__rich__`` prepends the current termviz animation line.  The
class is resolved at call time from the module namespace, so patching before
the audio loop starts is enough.  Every Alex task then animates inside the
existing HUD: the microphone bars stay put, the activity line renders above
them, and both disappear together when the HUD closes.
"""

from __future__ import annotations

import logging

from .visualizer import Visualizer, get_visualizer

logger = logging.getLogger("alex.termviz")

_PATCH_FLAG = "_termviz_waveform_patch"


def patch_console_waveform(visualizer: Visualizer | None = None) -> bool:
    """Join LiveKit's terminal interface instead of competing with it.

    Two things are patched:

    1. :class:`Rich Live` inside ``livekit.agents.cli._legacy`` — every external
       Live region (the waveform HUD, the text prompt, status spinners) calls
       :meth:`Visualizer.yield_to_hud` *before* taking the console, while our
       own region still owns it and can be erased cleanly.
    2. :class:`FrequencyVisualizer` — the microphone HUD prepends the current
       animation line to its render, which is where tasks show from then on.

    Returns ``True`` when the LiveKit console HUD was patched.  Safe to call
    more than once; later callers return ``False`` (already patched).
    """
    try:
        from livekit.agents.cli import _legacy
    except Exception:  # livekit not installed / different layout
        return False

    if getattr(_legacy, _PATCH_FLAG, False):
        return False
    if not _patch_live_handover(_legacy):
        return False

    base = getattr(_legacy, "FrequencyVisualizer", None)
    if base is None:
        return False

    def _current() -> Visualizer:
        """The instance passed to this patch, or the process-wide one."""
        return visualizer if visualizer is not None else get_visualizer()

    class WaveWithViz(base):  # type: ignore[misc, valid-type]
        """Waveform HUD with the termviz task line rendered above it."""

        def __rich__(self):  # type: ignore[override]
            from rich.console import Group
            from rich.text import Text

            try:
                hud = base.__rich__(self)
            except Exception:
                logger.debug("LiveKit waveform HUD failed", exc_info=True)
                hud = Text("")

            try:
                _current().adopt_console(self.c.console)
            except Exception:  # headless/test HUD without an AgentsConsole
                pass
            try:
                _current().yield_to_hud()
                line = _current().render_line()
            except Exception:
                logger.debug("termviz render failed", exc_info=True)
                line = None
            return Group(line, hud) if line is not None else hud

    _legacy.FrequencyVisualizer = WaveWithViz
    setattr(_legacy, _PATCH_FLAG, True)
    return True


def _patch_live_handover(_legacy) -> bool:
    original = getattr(_legacy, "Live", None)
    if original is None or getattr(original, "_termviz_handover", False):
        return False

    class HandoverLive(original):  # type: ignore[misc, valid-type]
        """Yield our animation region before the external one starts."""

        def __enter__(self):  # type: ignore[override]
            try:
                get_visualizer().adopt_console(self.console)
                get_visualizer().yield_to_hud()
            except Exception:
                logger.debug("hand-over failed", exc_info=True)
            return super().__enter__()

        def __exit__(self, *args):  # type: ignore[override]
            return super().__exit__(*args)

    HandoverLive._termviz_handover = True  # type: ignore[attr-defined]
    _legacy.Live = HandoverLive
    return True
