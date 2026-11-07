"""Built-in terminal animations.

Every function is pure and defensive: a bad width or label must never be able
to break the agent, so the visualizer catches exceptions as a second line of
defence.  Styles use Rich markup names, which degrade to plain characters when
colour is unavailable.
"""

from __future__ import annotations

import math

from rich.text import Text

from .registry import Task, animation

BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BARS = "▁▂▃▄▅▆▇█"


@animation("default", "think")
def think(task: Task, t: float, width: int) -> Text:
    """Braille spinner plus breathing dots."""
    line = Text(BRAILLE[int(t * 12) % len(BRAILLE)], style="cyan")
    for i in range(5):
        v = (math.sin(t * 3 - i * 0.6) + 1) / 2
        line.append(
            " ●",
            style=(
                f"rgb({int(40 + v * 60)},"
                f"{int(80 + v * 140)},"
                f"{int(120 + v * 135)})"
            ),
        )
    return line


@animation("file")
def file_create(task: Task, t: float, width: int) -> Text:
    """Typewriter: the filename types itself out, then blinks a cursor."""
    name = task.label or "untitled"
    n = int(t * 14) % (len(name) + 12)
    typed = name[:n]
    cursor = "▍" if int(t * 4) % 2 == 0 else " "
    line = Text("▤ ", style="green")
    line.append(typed, style="bright_green")
    line.append(cursor, style="white")
    line.append(" " * (len(name) - len(typed)))
    return line


@animation("search")
def search(task: Task, t: float, width: int) -> Text:
    """Radar/cylon sweep with a fading trail."""
    span = 24
    pos = (math.sin(t * 3) + 1) / 2 * (span - 1)
    line = Text("⌕ ", style="magenta")
    for i in range(span):
        d = abs(i - pos)
        ch = "█" if d < 0.7 else "▓" if d < 1.5 else "▒" if d < 2.5 else "·"
        line.append(ch, style="magenta" if d < 2.5 else "grey37")
    return line


@animation("analyze")
def analyze(task: Task, t: float, width: int) -> Text:
    """Live-looking waveform."""
    line = Text("∿ ", style="yellow")
    for i in range(28):
        v = (
            math.sin(t * 4 + i * 0.5) * math.sin(t * 1.3 + i * 0.2) + 1
        ) / 2
        line.append(BARS[int(v * 7)], style="yellow")
    return line


@animation("browser")
def browser(task: Task, t: float, width: int) -> Text:
    """A window frame that expands, then shows traffic-light dots."""
    p = (t * 1.2) % 1.8
    if p < 1:
        return Text("┌" + "─" * int(p * 18) + "┐", style="blue")
    line = Text("┌─", style="blue")
    for colour in ("red", "yellow", "green"):
        line.append("●", style=colour)
    line.append("──────────┐", style="blue")
    return line


@animation("net", "web", "download", "install", "reach")
def net(task: Task, t: float, width: int) -> Text:
    """Packets travelling between two endpoints."""
    span = 22
    line = Text("◉ ", style="green")
    for i in range(span):
        on = any(int(t * 10 + k * 7) % span == i for k in range(3))
        line.append("•" if on else "─", style="bright_green" if on else "grey37")
    line.append(" ◎", style="green")
    return line


@animation("shell", "exec", "run")
def shell(task: Task, t: float, width: int) -> Text:
    """A prompt with a block cursor and a low equaliser."""
    line = Text("❯ ", style="bright_cyan")
    pos = int(t * 10) % 14
    for i in range(14):
        if i == pos:
            line.append("█", style="bright_cyan")
        elif abs(i - pos) == 1:
            line.append("▒", style="cyan")
        else:
            line.append("░", style="grey37")
    line.append(" $", style="bright_black")
    return line


@animation("typing", "desktop", "keyboard")
def typing(task: Task, t: float, width: int) -> Text:
    """A keyboard row with the active key highlighted."""
    keys = "qwertyuiop"
    pos = int(t * 8) % len(keys)
    line = Text("⌨ ", style="green")
    for i, key in enumerate(keys):
        line.append(key, style="bright_green" if i == pos else "grey37")
    return line


@animation("memory", "remember", "recall")
def memory(task: Task, t: float, width: int) -> Text:
    """Stacks of memory cells filling in sequence."""
    line = Text("◍ ", style="magenta")
    filled = int(t * 4) % 7
    for i in range(6):
        line.append(
            "▰" if i < filled else "▱",
            style="magenta" if i < filled else "grey37",
        )
    return line


@animation("model", "ai", "plan")
def model(task: Task, t: float, width: int) -> Text:
    """Orbiting particles around a calm centre."""
    line = Text("◌ ", style="bright_blue")
    for i in range(9):
        v = (math.sin(t * 3 + i * 0.7) + 1) / 2
        line.append(
            "•" if v > 0.55 else "·",
            style="bright_blue" if v > 0.75 else "blue",
        )
    return line
