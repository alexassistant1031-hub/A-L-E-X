"""Agent Reach: sandboxed read-only access to external platforms.

The LiveKit wrapper lives in ``tool.py`` alongside the implementation because
the adapter is a single self-contained unit.
"""

from .tool import (
    REACH_TOOLS,
    ReachConfig,
    ReachInputError,
    ReachTool,
    ReachToolError,
    reach,
)

__all__ = [
    "REACH_TOOLS",
    "reach",
    "ReachTool",
    "ReachConfig",
    "ReachToolError",
    "ReachInputError",
]
