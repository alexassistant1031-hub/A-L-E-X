"""Alex tool registry.

Each capability lives in its own package under ``tools/``:

    tools/<feature>/tool.py         implementation (BaseTool subclass)
    tools/<feature>/operations.py   LiveKit function-tool wrapper

``tools/base.py`` holds the shared :class:`BaseTool`, and ``tools/shell`` owns
the command permission policy.  This module re-exports the public API so the
worker and the tests import tools from one place.
"""

from .edit import EDIT_TOOLS, edit_file
from .email import EMAIL_TOOLS, email
from .files import FILE_TOOLS, file_operation
from .github import GITHUB_TOOLS, github
from .reach import REACH_TOOLS, reach
from .search import SEARCH_TOOLS, web_search
from .shell import SHELL_TOOLS, shell
from .store import STORE_TOOLS, store_operation
from .telegram import TELEGRAM_TOOLS, telegram
from .typing import TYPING_TOOLS, KeyboardTool, backend_status, type_text

__all__ = [
    "KeyboardTool",
    "backend_status",
    "type_text",
    "TYPING_TOOLS",
    "file_operation",
    "FILE_TOOLS",
    "github",
    "GITHUB_TOOLS",
    "edit_file",
    "EDIT_TOOLS",
    "shell",
    "SHELL_TOOLS",
    "web_search",
    "SEARCH_TOOLS",
    "reach",
    "REACH_TOOLS",
    "telegram",
    "TELEGRAM_TOOLS",
    "email",
    "EMAIL_TOOLS",
    "store_operation",
    "STORE_TOOLS",
]
