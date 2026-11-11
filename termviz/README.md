# termviz — context-aware terminal animations

Alex's terminal replaces scrolling log output with a single animated line per
task.  Every raw log record is preserved in a rotating `activity.log`, and
warnings still appear as permanent one-line notices.

```
⌕ ▓▒█▒▓···········  querying prices  2.4s
✓ prices 2.4s
```

## Layout

| File | Purpose |
|---|---|
| `registry.py` | `Task`, `Frame`, `REGISTRY`, `@animation`, dotted-kind `resolve` |
| `animations.py` | the built-in frame functions (registered on import) |
| `visualizer.py` | thread-safe `Visualizer`, Rich `Live` region, global instance |
| `logs.py` | `TermvizLogHandler`, `install()` bridge, `guard_livekit_logging()` |
| `integration.py` | `patch_console_waveform()` — joins LiveKit's console HUD |

## Usage in tool code

Synchronous:

```python
from termviz import get_visualizer

with get_visualizer().task("file.write", "notes.txt"):
    write_the_file()
```

Inside an async tool coroutine (the pattern used in `tools/*/operations.py`):

```python
with get_visualizer().task(f"file.{action}", Path(path).name):
    result = await tool.execute(...)
```

or the async variant:

```python
async with get_visualizer().atask("memory", "loading memory"):
    await memory.initialize()
```

Kinds are dotted and match from most to least specific, so `file.write`
resolves to the `file` animation and `search.google.images` resolves to
`search`.  Unknown kinds fall back to `default` (the braille spinner), so a
new tool is decorated the moment it is written — no wiring required.

## Adding an animation

```python
# termviz/animations.py
from rich.text import Text
from .registry import Task, animation

@animation("build.compile")
def compile_spinner(task: Task, t: float, width: int) -> Text:
    tick = "⚙" if int(t * 8) % 2 else "⚙"
    return Text(tick, style="bright_yellow")
```

That is the whole change: the function must be pure (same `(task, t, width)`
in, one `Text` out) and never raise.  The visualizer also catches exceptions
during rendering as a second line of defence, so a broken animation can never
break a tool call.

## Behaviour and safety

* Animations run only when stdout is a terminal and `ALEX_TERMVIZ` is not `0`.
  Otherwise `task()` is pure bookkeeping and `note()` falls back to stderr —
  safe for pipes, systemd and CI.
* Errors and warnings always surface as a permanent single line; INFO and
  below are logged to file only.
* Internal errors of the visualizer are degraded to a plain line and never
  propagate into tool results.
* `install()` is idempotent: calling it again removes any console handlers
  that were added after the first call (LiveKit's CLI re-adds one during
  worker startup, so `entrypoint()` re-installs the bridge).

## Living beside LiveKit's console

LiveKit's console mode owns the terminal in two ways, and both are handled:

* **Logging.** `guard_livekit_logging()` (called at import in `main.py`)
  wraps the two functions LiveKit uses to install console handlers —
  `livekit.agents.cli.log.setup_logging` (worker/dev path, plain
  `StreamHandler`) and `livekit.agents.cli._legacy._configure_logger`
  (console path, `RichLoggingHandler`).  After each of them runs, `install()`
  re-strips the console, so styled log blocks never scroll again.
* **The microphone waveform HUD.** LiveKit runs its own Rich `Live` region
  (the `▁▂▃…` mic bars).  Two `Live` regions on one console garble each
  other, so `patch_console_waveform()` swaps LiveKit's `FrequencyVisualizer`
  for a subclass that prepends the termviz task line inside the HUD:

  ```
  ‖ ❯ ░░▒█▒░░… $  xdg-open  2.1s      ← Alex's task, animated
     default   ▁ ▁ ▁ ▃ ▆ █ …          ← LiveKit's mic bars, untouched
  ```

  The patched HUD and Live also call `Visualizer.adopt_console()`: tasks,
  notices and completion lines render on LiveKit's console from then on.
  That matters because LiveKit's HUD uses its *own* `Console` instance —
  without adoption the two Live regions write to the same terminal without
  knowing about each other, which garbles the output.
  If the HUD comes up *while* a task is animating, the internal region is
  stopped on the first HUD frame; in text mode or anywhere without the HUD,
  the visualizer runs its own region as before.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `ALEX_TERMVIZ` | `1` | Set `0` to disable animations entirely |
| `ALEX_TERMVIZ_FPS` | `15` | Refresh rate of the animated line (2-60) |
| `ALEX_TERMVIZ_LOG` | `~/.local/state/alex/termviz/activity.log` | Where raw log records go |

The activity log is rotating (1 MB × 3, mode 0700) and is separate from the
controller's hash-chained audit streams, which are never touched here.
