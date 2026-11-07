"""Thread-safe Rich renderer for one-line terminal animations.

The visualizer is deliberately optional and fail-open: if Rich is missing, the
output is not a TTY, or ``ALEX_TERMVIZ=0``, every ``task()`` becomes a no-op
context manager and the rest of Alex works exactly as before.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from typing import Any, Iterator, List, Optional

from .registry import REGISTRY, Task, resolve  # noqa: F401  (registers default)
from . import animations  # noqa: F401  (import for the side effect: registration)

logger = logging.getLogger("alex.termviz")

DEFAULT_FPS = 15
MAX_NOTE_LINES = 4

try:  # Rich ships with livekit-agents, but Alex must survive without it.
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    _HAVE_RICH = True
except Exception:  # pragma: no cover - exercised only on broken installs
    Console = None  # type: ignore[assignment]
    Live = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]
    _HAVE_RICH = False


def _env_enabled() -> bool:
    raw = os.getenv("ALEX_TERMVIZ", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disable", "disabled"}


def _env_fps() -> int:
    raw = os.getenv("ALEX_TERMVIZ_FPS", "").strip()
    if not raw:
        return DEFAULT_FPS
    try:
        return max(2, min(60, int(raw)))
    except ValueError:
        return DEFAULT_FPS


class Visualizer:
    """Render the current task as a single animated line.

    ``task()`` is safe to nest and safe to call from several threads.  The
    outermost task owns the Rich ``Live`` region; when the last task ends the
    region is erased by the terminal.
    """

    def __init__(
        self,
        console: Any | None = None,
        fps: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        if _HAVE_RICH:
            self.console = console or Console(highlight=False, soft_wrap=True)
        else:
            self.console = console
        self.fps = fps if fps is not None else _env_fps()
        if enabled is None:
            enabled = _HAVE_RICH and _env_enabled() and self._console_is_tty()
        self._enabled = bool(enabled)
        self._lock = threading.RLock()
        self._live_lock = threading.Lock()
        self._stack: List[Task] = []
        self._live: Any | None = None
        self._own_live: Any | None = None
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._writer_paused = threading.Event()
        self._t0 = time.monotonic()

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> Task | None:
        with self._lock:
            return self._stack[-1] if self._stack else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "fps": self.fps,
            "active": self.current.kind if self.current else "",
            "joined_hud": self._external_live_active(),
            "registered": len(REGISTRY),
        }

    def _console_is_tty(self) -> bool:
        console = self.console
        return bool(getattr(console, "is_terminal", False))

    # ── rendering ────────────────────────────────────────────────────────────

    def __rich__(self) -> Any:
        line = self.render_line()
        return line if line is not None else Text("")

    def render_line(self) -> Any | None:
        """Current animation as one ``rich.text.Text`` line, or ``None``.

        ``None`` means "no task to show"`; embedding code (the waveform HUD)
        should then keep its own layout untouched.
        """
        if not _HAVE_RICH or not self._enabled:
            return None
        with self._lock:
            task = self._stack[-1] if self._stack else None
        if task is None:
            return None

        width = getattr(self.console, "width", 80) or 80
        elapsed = time.monotonic() - self._t0
        try:
            line = resolve(task.kind)(task, elapsed, width)
        except Exception:  # a broken animation must never break a task
            logger.debug("animation %r failed", task.kind, exc_info=True)
            line = Text("●", style="red")

        # The file animation draws its own label (typewriter effect).
        if task.label and task.kind.split(".")[0] != "file":
            line.append(f"  {task.label}", style="bold")
        line.append(f"  {task.elapsed:.1f}s", style="dim")
        return line

    def _external_live_active(self) -> bool:
        """True while someone else owns the console's Rich Live region."""
        if not _HAVE_RICH:
            return False
        stack = getattr(self.console, "_live_stack", None)
        if stack:  # newer Rich keeps a stack of Live regions
            return any(live is not self._live for live in stack)
        live = getattr(self.console, "_live", None)
        return bool(live) and live is not self._live

    def adopt_console(self, console: Any) -> None:
        """Switch to the console the external HUD owns.

        LiveKit's console HUD runs its ``Live`` on *its* ``Console`` instance.
        Unless we render tasks and notices on that same console, two Rich
        ``Live`` regions write to the same stdout without knowing about each
        other and garble the terminal (duplicated/parked lines).  Called by
        the integration hooks when an external Live starts or renders.

        Switching is idempotent and stops our own Live region first — which
        is safe here because the external Live has not entered the adopted
        console yet at that point.
        """
        if console is None or console is self.console or not _HAVE_RICH:
            return
        with self._live_lock:
            live = self._live
            self._live = None
        if live is not None:
            # Console is still the old one, so ``owns`` resolves true and the
            # frame is erased from it cleanly before we rebind.
            self._pause_lock()
        with self._lock:
            self.console = console

    def yield_to_hud(self) -> None:
        """Stop our own Live region once an external HUD takes over.

        Rich ``Live`` is not thread-safe: stopping it from the HUD's thread
        while our own refresh emits a frame produces a duplicated line, so
        the single-writer loop is drained first and ``stop()`` only ever runs
        after it has fully exited.
        """
        if not self._enabled:
            return
        if not self._external_live_active():
            return
        with self._live_lock:
            live = self._live
            self._live = None
        if live is None:
            return
        self._pause_lock()

    def _pause_lock(self) -> None:
        """Stop the writer loop, then release the Live region cleanly."""
        self._writer_stop.set()
        self._writer_paused.set()
        writer = self._writer
        self._writer = None
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=1.0)
        live = self._own_live
        self._own_live = None
        if live is None:
            return
        stack = getattr(self.console, "_live_stack", None)
        owns = (
            any(item is live for item in stack or ())
            if stack is not None
            else getattr(self.console, "_live", None) is live
        )
        if not owns:
            # The HUD already owns the console; stopping our Live now would
            # erase the wrong (HUD-owned) region, so leave the object be.
            return
        try:
            live.stop()
        except Exception:
            logger.debug("could not stop terminal animation", exc_info=True)

    def _start_live(self) -> None:
        if not self._enabled or not _HAVE_RICH:
            return
        if self._external_live_active():
            # LiveKit's console HUD already runs a Live region. Two Rich
            # ``Live``s on one console garble each other, so we stop competing
            # and render through the HUD instead (see termviz.integration).
            with self._live_lock:
                self._live = None
            return
        with self._live_lock:
            if self._live is not None:
                return
            try:
                live = Live(
                    self,
                    console=self.console,
                    refresh_per_second=self.fps,
                    transient=True,
                    vertical_overflow="visible",
                    auto_refresh=False,   # our own writer is the single author
                )
                live.start()
                self._own_live = live
                self._live = live
                self._writer_stop.clear()
                self._writer_paused.clear()
                self._writer = threading.Thread(
                    target=self._writer_loop,
                    args=(live, 1.0 / max(2, self.fps)),
                    daemon=True,
                    name="termviz-live-writer",
                )
                self._writer.start()
            except Exception:
                self._live = None
                self._own_live = None
                logger.debug("could not start terminal animation", exc_info=True)

    def _writer_loop(self, live: Any, interval: float) -> None:
        while not self._writer_stop.is_set() and getattr(live, "_started", False):
            time.sleep(interval)
            if self._writer_stop.is_set() or self._writer_paused.is_set():
                continue
            try:
                live.refresh()
            except Exception:
                break

    def _stop_live(self) -> None:
        with self._live_lock:
            live = self._live
            self._live = None
        if live is None:
            return
        self._pause_lock()

    # ── public surface ───────────────────────────────────────────────────────

    @contextlib.contextmanager
    def task(self, kind: str, label: str = "") -> Iterator[Task]:
        """Show an animation for *kind* while the block runs.

        The stack is always maintained (so ``current`` is useful even when
        animations are disabled); only rendering is gated on ``enabled``.
        """
        task = Task(kind, label or "")
        with self._lock:
            self._stack.append(task)
        if self._enabled:
            self._start_live()

        ok = True
        try:
            yield task
        except BaseException:
            ok = False
            raise
        finally:
            with self._lock:
                try:
                    self._stack.remove(task)
                except ValueError:
                    pass
                last = self._enabled and not self._stack
            if last:
                self._finish(task, ok)
                self._stop_live()

    @contextlib.asynccontextmanager
    async def atask(self, kind: str, label: str = ""):
        """Async variant of :meth:`task`, for use inside tool coroutines."""
        with self.task(kind, label):
            yield

    def _finish(self, task: Task, ok: bool) -> None:
        if not self._enabled:
            return
        mark = "✓" if ok else "✗"
        style = "green" if ok else "red"
        try:
            self.console.print(
                f"[{style}]{mark}[/] {task.label or task.kind} "
                f"[dim]{task.elapsed:.1f}s[/]"
            )
        except Exception:
            logger.debug("could not print task result", exc_info=True)

    def note(self, message: str, style: str = "yellow") -> None:
        """Print a permanent one-line notice above the animation.

        Used for warnings and errors, which must not disappear into a log file.
        """
        text = " ".join(str(message).split())
        if len(text) > 400:
            text = text[:397] + "…"
        if not self._enabled or not _HAVE_RICH:
            print(text, file=sys.stderr)
            return
        try:
            self.console.print(Text(text, style=style))
        except Exception:
            print(text, file=sys.stderr)

    def close(self) -> None:
        """Stop the animation region; safe to call more than once."""
        with self._lock:
            self._stack.clear()
        self._stop_live()


_default: Visualizer | None = None
_default_lock = threading.Lock()


def get_visualizer() -> Visualizer:
    """Return the process-wide visualizer shared by tools and logging."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Visualizer()
        return _default


def configure(visualizer: Visualizer | None) -> None:
    """Replace the process-wide visualizer, mainly for tests."""
    global _default
    with _default_lock:
        if _default is not None and _default is not visualizer:
            _default.close()
        _default = visualizer
