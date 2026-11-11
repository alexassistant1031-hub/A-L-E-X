"""
tools/edit/operations.py

Exposes exact-string editing as a LiveKit function tool.
"""

from __future__ import annotations

import logging
from pathlib import Path

from livekit.agents import llm

from termviz import get_visualizer

from .tool import EditTool

from recovery import SafetyTag, execute_tool

logger = logging.getLogger("alex.tools.edit_ops")

_edit_tool = EditTool()


@llm.function_tool
async def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact string inside a text file.

    Prefer this over rewriting a whole file: it changes only the matched text
    and leaves the rest untouched. Read the file first, then copy the exact
    text you want to replace, including whitespace and line endings.

    The match must be unique. If old_string appears more than once the edit is
    refused; add surrounding context or set replace_all=true. Writes are
    limited to staging/ and the user's working folders, exactly like the file
    tool. Every edit creates a verified safety backup before the file changes.

    Args:
        path: File to edit (absolute, ~, or relative to the project).
        old_string: Exact existing text. Must not be empty.
        new_string: Replacement text; empty deletes the matched text.
        replace_all: Replace every occurrence instead of requiring one match.
    """
    label = Path(path).name or path
    with get_visualizer().task("file.edit", label):
        result = await execute_tool(
            tool="edit", operation="edit_file",
            safety_tag=SafetyTag.WRITE_IRREVERSIBLE,
            func=_edit_tool.execute,
            error_prefixes=(),
            path=path, old_string=old_string, new_string=new_string,
            replace_all=replace_all,
        )
    logger.info("edit_file %s -> %s", path, result[:80].replace("\n", " "))
    return result


EDIT_TOOLS = [edit_file]

__all__ = ["edit_file", "EDIT_TOOLS"]
