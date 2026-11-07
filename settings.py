"""Persistent, operator-visible settings for Alex.

These values are intentionally code-backed rather than session state: they must
survive restarts, memory consolidation and new conversations.  Every setting can
still be overridden for a single launch with an environment variable.

OpenCode reply file
-------------------
OpenCode writes its reply for the user into one known file.  Alex reads that
file by default when the user says "read", "read the reply", "what did OpenCode
say", and does not name a different file.

ALEX_REPLY_FILE=/some/other/file.txt   Override the primary reply file.
                                       A directory is also accepted; Alex then
                                       reads ``reply.txt`` inside it.

The old ``~/Desktop/reply.txt`` location is retired.  It is kept here only so
the prompt can tell the model never to fall back to it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_REPLY_FILE = "~/Desktop/OpenCode Reply Text/reply.txt"
REPLY_ENV_VAR = "ALEX_REPLY_FILE"


def primary_reply_file() -> Path:
    """Return the absolute path Alex should read for "read the reply".

    Resolution rules:
      * ``ALEX_REPLY_FILE`` wins when it is set and non-empty.
      * ``~`` is expanded.
      * A directory (such as ``~/Desktop/OpenCode Reply Text``) resolves to
        ``reply.txt`` inside it, because that is how the user names the folder.
    """
    raw = os.getenv(REPLY_ENV_VAR, "").strip()
    path = Path(raw or DEFAULT_REPLY_FILE).expanduser()
    if path.is_dir():
        path = path / "reply.txt"
    return path


def obsolete_reply_file() -> Path:
    """The retired path that must never be used as the reply source."""
    return Path("~/Desktop/reply.txt").expanduser()


def retired_reply_redirect(path: str | Path) -> Path | None:
    """Return the replacement when *path* is the retired reply file.

    The file tool calls this so a model that remembers the old path is
    corrected instead of silently failing (or, worse, reading a stale file if
    one is ever recreated).
    """
    candidate = Path(path)
    try:
        candidate = candidate.resolve()
        retired = obsolete_reply_file().resolve()
    except OSError:
        return None
    if candidate != retired:
        return None
    return primary_reply_file()

