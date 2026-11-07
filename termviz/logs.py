"""Logging bridge: raw logs to a file, warnings to the animation line.

Alex produces a lot of informational chatter (tool results, memory status,
controller checks).  None of that belongs in a clean terminal, so records at
INFO and below go to ``activity.log`` only.  Warnings and errors are also
written there, but stay visible as one permanent line above the animation.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .visualizer import Visualizer, get_visualizer

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_HANDLER_FLAG = "_alex_termviz"
_MAX_BYTES = 1_000_000
_BACKUPS = 3


def default_log_path() -> Path:
    raw = os.getenv("ALEX_TERMVIZ_LOG", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "state" / "alex" / "termviz" / "activity.log"


class TermvizLogHandler(logging.Handler):
    """Write every record to a rotating file; surface warnings and errors."""

    def __init__(
        self,
        visualizer: Visualizer | None = None,
        path: str | Path | None = None,
        *,
        max_bytes: int = _MAX_BYTES,
        backups: int = _BACKUPS,
    ) -> None:
        super().__init__()
        self.visualizer = visualizer or get_visualizer()
        self.path = Path(path).expanduser() if path else default_log_path()
        self.setFormatter(logging.Formatter(LOG_FORMAT))
        self._file: logging.Handler | None = self._open(self.path, max_bytes, backups)

    @staticmethod
    def _open(path: Path, max_bytes: int, backups: int) -> logging.Handler | None:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            # ``emit`` below delegates to this inner handler, so the formatter
            # must be attached here.  A formatter on TermvizLogHandler itself
            # is never consulted and the activity log would lose its
            # timestamp/logger/level prefix.
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            return handler
        except OSError:
            return None

    def emit(self, record: logging.LogRecord) -> None:
        if self._file is not None:
            try:
                self._file.emit(record)
            except Exception:
                self.handleError(record)

        if record.levelno < logging.WARNING:
            return

        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                message = f"{message} ({type(exc).__name__}: {exc})"
            style = "red" if record.levelno >= logging.ERROR else "yellow"
            self.visualizer.note(f"{record.levelname.title()}: {message}", style)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        finally:
            self._file = None
            super().close()


def _is_console_handler(handler: logging.Handler) -> bool:
    """True for handlers that ultimately print to the terminal.

    LiveKit runs the entrypoint in a child process and ships records to the
    console through a socket-based ``LogQueueHandler``, so checking only for
    ``StreamHandler(sys.stdout)`` is not enough.
    """
    if isinstance(handler, logging.FileHandler):
        return False
    if handler.__class__.__name__ == "LogQueueHandler":
        return True
    module = type(handler).__module__
    if module.startswith("rich.logging") or module.endswith(".log_queue"):
        return True
    # LiveKit HUD handlers (RichLoggingHandler / LogQueueHandler / CLI setup)
    # all live under livekit.agents.*; none of them are Alex's file logs.
    if module.startswith("livekit.agents"):
        return True
    if isinstance(handler, logging.StreamHandler):
        stream = getattr(handler, "stream", None)
        if stream is None:
            return False
        if stream in (sys.stdout, sys.stderr):
            return True
        isatty = getattr(stream, "isatty", None)
        return bool(callable(isatty) and isatty())
    return False


def install(
    visualizer: Visualizer | None = None,
    path: str | Path | None = None,
    *,
    level: int = logging.INFO,
) -> TermvizLogHandler:
    """Install the terminal log bridge on the root logger.

    Console handlers are removed — stdout/stderr streams, terminal writers and
    LiveKit's socket-based job log forwarder — so terminal output comes from
    the visualizer instead of scrolling log lines.  Calling this again (for
    example after LiveKit configures logging) is safe and re-establishes the
    bridge.

    File and audit handlers are left untouched.
    """
    viz = visualizer or get_visualizer()
    root = logging.getLogger()

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            handler.visualizer = viz
            if path is not None and getattr(handler, "path", None) != Path(path).expanduser():
                root.removeHandler(handler)
                handler.close()
                continue
            continue
        if _is_console_handler(handler):
            root.removeHandler(handler)

    existing = [
        handler
        for handler in root.handlers
        if getattr(handler, _HANDLER_FLAG, False)
    ]
    if existing:
        handler = existing[0]
        handler.visualizer = viz
    else:
        handler = TermvizLogHandler(viz, path)
        setattr(handler, _HANDLER_FLAG, True)
        root.addHandler(handler)

    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return handler


def guard_livekit_logging() -> bool:
    """Re-install the bridge whenever LiveKit (re)configures logging.

    Console mode installs *two* different handlers depending on the path:

    * ``livekit.agents.cli.log.setup_logging`` — worker/dev path, stdout
      StreamHandler.
    * ``livekit.agents.cli._legacy._configure_logger`` — rich console mode,
      ``RichLoggingHandler`` (the timestamped, pretty multi-line log block).

    Wrapping both keeps the terminal animation-only from the very first
    startup line.  Returns ``True`` when at least one entry point was wrapped.
    """
    import importlib

    patched = _wrap_function("livekit.agents.cli.log", "setup_logging")
    patched |= _wrap_function("livekit.agents.cli.cli", "setup_logging")
    patched |= _wrap_function("livekit.agents.cli._legacy", "_configure_logger")

    from .integration import patch_console_waveform

    patched |= patch_console_waveform()
    return patched


def _wrap_function(module_name: str, attribute: str) -> bool:
    import importlib

    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    original = getattr(module, attribute, None)
    if original is None or getattr(original, "_termviz_guard", False):
        return False

    def wrapper(*args, _original=original, **kwargs):
        result = _original(*args, **kwargs)
        install()
        return result

    wrapper._termviz_guard = True  # type: ignore[attr-defined]
    setattr(module, attribute, wrapper)
    return True
