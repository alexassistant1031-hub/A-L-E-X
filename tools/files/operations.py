"""
tools/files/operations.py

Exposes filesystem access to Alex as a LiveKit function tool.

Policy reminder for the model: reading is unrestricted, but writing is limited
to the project's ``staging/`` directory and the user's working folders.
Application code, tools, memory, backups and audit logs are read-only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from livekit.agents import llm

from termviz import get_visualizer

from .tool import FileTool

from recovery import SafetyTag, execute_tool

logger = logging.getLogger("alex.tools.file_ops")

_file_tool = FileTool()


@llm.function_tool
async def file_operation(
    action: Literal[
        "read", "write", "append", "delete", "list",
        "mkdir", "copy", "move", "exists", "info",
    ],
    path: str,
    content: str = "",
    destination: str = "",
) -> str:
    """Read, create, edit, delete, list, copy, move or inspect files and folders.

    Use when the user asks about files on disk: read a file, save something,
    create notes, edit a document, list a folder, check whether a file
    exists, copy/move files, make a directory, or delete something.

    Writing is restricted to ``staging/`` inside the project and the user's
    configured working folders (Documents, Desktop, Downloads, Projects,
    Notes, Work). Attempts to write application code, tools, memory, backups or
    audit logs are refused. Credential files are never writable.

    Actions:
        read       - return a file's text contents
        write      - create a file or overwrite it with `content`
        append     - add `content` to the end of a file
        delete     - move a file/folder to the trash (recoverable)
        list       - list a directory
        mkdir      - create a directory
        copy       - copy `path` to `destination`
        move       - move `path` to `destination`
        exists     - check whether a path exists
        info       - size, dates and permissions

    `path` may be absolute, start with ~, or be relative to the project
    folder. To write, use ``staging/<path>`` (relative to the project) or an
    absolute path inside a working folder. For copy/move, `destination` is
    required.

    Args:
        action: One of the actions above.
        path: File or directory path.
        content: Text for write/append.
        destination: Target path for copy/move.
    """
    label = Path(path).name or path
    _tag = SafetyTag.READ if action == "read" else SafetyTag.WRITE_IRREVERSIBLE
    with get_visualizer().task(f"file.{action}", label):
        result = await execute_tool(
            tool="file_operation", operation=action,
            safety_tag=_tag,
            func=_file_tool.execute,
            error_prefixes=(),
            action=action, path=path, content=content,
            destination=destination,
        )
    logger.info("file_operation %s %s -> %s", action, path, result[:80])
    return result


FILE_TOOLS = [file_operation]

__all__ = ["file_operation", "FILE_TOOLS"]
