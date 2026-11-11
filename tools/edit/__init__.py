"""Exact edits: replace precise text in a file with a verified snapshot."""

from .operations import EDIT_TOOLS, edit_file
from .tool import EditTool

__all__ = ["EDIT_TOOLS", "edit_file", "EditTool"]
