"""termviz: modular, context-aware terminal animations for Alex.

The terminal shows one animated line per active task instead of scrolling log
output.  Nothing else about the agent changes: tool code keeps returning the
same strings, and log records are written to ``activity.log``.

Quick start
-----------
    from termviz import get_visualizer

    with get_visualizer().task("search.web", "looking up prices"):
        ...do the work...

Async tools can use ``atask`` or the same ``task`` context manager around an
``await``:

    async with get_visualizer().atask("file.read", path.name):
        result = await tool.execute(...)

Adding an animation
-------------------
    from termviz import animation

    @animation("build.compile")
    def compile_spinner(task, t, width):
        from rich.text import Text
        return Text("⚙", style="yellow")

Kinds resolve from most to least specific, so ``build.compile.debug`` uses the
``build.compile`` animation.  A ``default`` animation is always present.

Environment
-----------
ALEX_TERMVIZ=0        Disable animations (plain stderr warnings only).
ALEX_TERMVIZ_FPS=15   Animation refresh rate, 2-60.
ALEX_TERMVIZ_LOG=...  Override the activity log path.
"""

from __future__ import annotations

from .registry import REGISTRY, Frame, Task, animation, registered, resolve
from .visualizer import (
    Visualizer,
    configure,
    get_visualizer,
)
from .logs import (
    TermvizLogHandler,
    default_log_path,
    guard_livekit_logging,
    install as install_logging,
)
from .integration import patch_console_waveform

__all__ = [
    "REGISTRY",
    "Frame",
    "Task",
    "animation",
    "registered",
    "resolve",
    "Visualizer",
    "configure",
    "get_visualizer",
    "TermvizLogHandler",
    "default_log_path",
    "guard_livekit_logging",
    "install_logging",
    "patch_console_waveform",
]
