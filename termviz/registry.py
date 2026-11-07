"""Animation registry for the terminal visualizer.

A *kind* is a dotted name such as ``search.google`` or ``file.write``.  A
*frame function* turns a task plus a clock into one line of ``rich.text.Text``.
Kinds resolve from most to least specific: ``search.google.images`` ->
``search.google`` -> ``search`` -> ``default``.

Adding an animation never requires touching the visualizer:

    from termviz import animation

    @animation("build.compile")
    def compile_spinner(task, t, width):
        ...
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

# A frame function must be pure: same (task, t, width) -> same Text.
Frame = Callable[["Task", float, int], object]

REGISTRY: Dict[str, Frame] = {}


@dataclass
class Task:
    """One unit of work shown in the terminal."""

    kind: str
    label: str = ""
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


def animation(*kinds: str):
    """Register a frame function for one or more kinds.

    The decorator returns the function unchanged, so animations remain plain,
    testable functions.
    """

    def register(fn: Frame) -> Frame:
        for kind in kinds:
            REGISTRY[kind.strip().lower()] = fn
        return fn

    return register


def _fallback(task: Task, t: float, width: int):
    from rich.text import Text

    return Text("●", style="cyan")


def resolve(kind: str) -> Frame:
    """Return the most specific frame function for *kind*."""
    parts = (kind or "default").strip().lower().split(".")
    while parts:
        key = ".".join(parts)
        if key in REGISTRY:
            return REGISTRY[key]
        parts.pop()
    return REGISTRY.get("default", _fallback)


def registered() -> List[str]:
    """Sorted list of registered kinds, useful for diagnostics and tests."""
    return sorted(REGISTRY)
