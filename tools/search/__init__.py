"""Web search: pluggable search backends behind one tool."""

from .operations import SEARCH_TOOLS, web_search
from .tool import SearchTool

__all__ = ["SEARCH_TOOLS", "web_search", "SearchTool"]
