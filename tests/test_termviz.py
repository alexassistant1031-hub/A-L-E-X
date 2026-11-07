"""Tests for the termviz terminal animation package."""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from termviz import (
    REGISTRY,
    Visualizer,
    animation,
    get_visualizer,
    install_logging,
    resolve,
)
from termviz.logs import TermvizLogHandler


def plain_console(enable: bool = False) -> Console:
    return Console(
        file=io.StringIO(),
        force_terminal=enable,
        width=100,
        highlight=False,
        soft_wrap=True,
    )


class RegistryTests(unittest.TestCase):
    def test_specific_kind_falls_back_to_parent(self) -> None:
        self.assertIs(resolve("search.google.images"), REGISTRY["search"])
        self.assertIs(resolve("file.write"), REGISTRY["file"])

    def test_unknown_kind_falls_back_to_default(self) -> None:
        self.assertIs(resolve("totally.unknown.kind"), REGISTRY["default"])

    def test_custom_animation_registers_and_resolves(self) -> None:
        @animation("build.compile")
        def watcher(task, t, width):
            return "compiling"

        try:
            self.assertIs(resolve("build.compile.debug"), watcher)
            self.assertIs(resolve("build.compile"), watcher)
        finally:
            REGISTRY.pop("build.compile", None)

    def test_bulk_registration(self) -> None:
        @animation("a.first", "a.second")
        def shared(task, t, width):
            return "ok"

        try:
            self.assertIs(resolve("a.first"), shared)
            self.assertIs(resolve("a.second"), shared)
        finally:
            for kind in ("a.first", "a.second"):
                REGISTRY.pop(kind, None)


class VisualizerTests(unittest.TestCase):
    def test_disabled_task_is_strictly_bookkeeping(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        with viz.task("search.web", "prices") as task:
            self.assertEqual(task.kind, "search.web")
            self.assertIsNotNone(viz.current)
        self.assertIsNone(viz.current)
        self.assertEqual(viz.status()["registered"], len(REGISTRY))

    def test_nested_tasks_stack_and_drain(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        with viz.task("model", "outer"):
            with viz.task("search.web", "inner") as current:
                self.assertEqual(viz.current, current)
        self.assertIsNone(viz.current)

    def test_exception_pops_the_task(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        with self.assertRaises(ZeroDivisionError):
            with viz.task("file.write", "broken"):
                1 / 0
        self.assertIsNone(viz.current)
        self.assertFalse(viz._stack)

    def test_pops_only_its_own_task(self) -> None:
        list_tasks = ["a", "b"]
        viz = Visualizer(console=plain_console(), enabled=False)
        with viz.task("model", list_tasks[0]):
            with viz.task("shell", list_tasks[1]):
                # simulate an outer task draining first (misuse defence)
                viz._stack.remove(viz._stack[0])
        self.assertFalse(viz._stack)

    def test_animation_failure_never_breaks_the_task(self) -> None:
        viz = Visualizer(console=plain_console(enable=True), enabled=True)
        with mock.patch.object(viz, "fps", 60):
            @animation("temp.bad")
            def broken(task, t, width):
                raise RuntimeError("boom")

            try:
                with viz.task("temp.bad", "crash test"):
                    pass
            finally:
                REGISTRY.pop("temp.bad", None)
        self.assertIsNone(viz.current)

    def test_note_when_disabled_goes_to_stderr(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            viz.note("careful there", style="yellow")
        self.assertEqual(err.getvalue().strip(), "careful there")

    def test_enabled_console_prints_completion_lines(self) -> None:
        console = plain_console(enable=True)
        viz = Visualizer(console=console, fps=60, enabled=True)
        with viz.task("search.web", "prices"):
            pass
        viz.close()
        text = console.file.getvalue()
        clean = text.replace("\x1b[32m", "").replace("\x1b[0m", "").replace("\x1b[2m", "")
        self.assertIn("✓", clean)
        self.assertIn("prices", clean)
        self.assertIn("0.", clean)


class LogBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = logging.getLogger()
        self._handlers = list(self._root.handlers)
        self._level = self._root.level
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _restore_root(self) -> None:
        self._root.handlers[:] = self._handlers
        self._root.setLevel(self._level)

    def test_records_writing_to_file(self) -> None:
        log_path = Path(self.temp.name) / "activity.log"
        handler = TermvizLogHandler(Visualizer(enabled=False), log_path)
        self._root.addHandler(handler)
        self.addCleanup(self._root.removeHandler, handler)
        self.addCleanup(handler.close)
        test = logging.getLogger("alex.termviz.test")
        test.setLevel(logging.INFO)
        test.propagate = True
        test.info("normal %s", "message")
        test.warning("loud message")
        handler._file.flush()
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("normal message", text)
        self.assertIn("loud message", text)

    def test_warnings_surface_through_the_visualizer(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        log_path = Path(self.temp.name) / "activity.log"
        handler = TermvizLogHandler(viz, log_path)
        self._root.addHandler(handler)
        self.addCleanup(self._root.removeHandler, handler)
        self.addCleanup(handler.close)

        notes: list[tuple[str, str]] = []
        viz_note = viz.note
        viz.note = lambda message, style="yellow": notes.append((message, style))
        self.addCleanup(setattr, viz, "note", viz_note)

        test = logging.getLogger("alex.termviz.test2")
        test.setLevel(logging.INFO)
        test.info("quiet")
        test.error("boom")
        self.assertEqual([message for message, _ in notes], ["Error: boom"])
        self.assertEqual([style for _, style in notes], ["red"])

    def test_install_removes_livekit_job_log_queue(self) -> None:
        class LogQueueHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                pass

        root = logging.getLogger()
        queue_handler = LogQueueHandler()
        root.addHandler(queue_handler)
        log_path = Path(self.temp.name) / "queue.log"
        try:
            handler = install_logging(path=log_path)
            self.assertNotIn(queue_handler, root.handlers)
        finally:
            root.removeHandler(handler)
            handler.close()

    def test_install_keeps_file_handlers(self) -> None:
        root = logging.getLogger()
        file_handler = logging.FileHandler(Path(self.temp.name) / "keep.log")
        root.addHandler(file_handler)
        log_path = Path(self.temp.name) / "keep-install.log"
        try:
            handler = install_logging(path=log_path)
            self.assertIn(file_handler, root.handlers)
        finally:
            root.removeHandler(handler)
            handler.close()
            root.removeHandler(file_handler)
            file_handler.close()

    def test_guard_wraps_livekit_logging_setup(self) -> None:
        import livekit.agents.cli._legacy as legacy
        import livekit.agents.cli.cli as cli_module
        import livekit.agents.cli.log as log_module

        from termviz import guard_livekit_logging

        saved = {
            cli_module: ("setup_logging", getattr(cli_module, "setup_logging")),
            log_module: ("setup_logging", getattr(log_module, "setup_logging")),
            legacy: ("_configure_logger", getattr(legacy, "_configure_logger")),
        }
        waveform = legacy.FrequencyVisualizer
        try:
            self.assertTrue(guard_livekit_logging())
            self.assertTrue(
                getattr(log_module.setup_logging, "_termviz_guard", False)
            )
            self.assertTrue(
                getattr(legacy._configure_logger, "_termviz_guard", False)
            )
            self.assertFalse(guard_livekit_logging())  # already wrapped
        finally:
            for module, (attribute, original) in saved.items():
                if original is not None:
                    setattr(module, attribute, original)
            legacy.FrequencyVisualizer = waveform
            delattr(legacy, "_termviz_waveform_patch")

    def test_install_is_idempotent_and_removes_console_handlers(self) -> None:
        root = logging.getLogger()
        console_handler = logging.StreamHandler(sys.stdout)
        root.addHandler(console_handler)
        log_path = Path(self.temp.name) / "install.log"
        try:
            first = install_logging(path=log_path)
            second = install_logging(path=log_path)
            self.assertIs(first, second)
            self.assertNotIn(console_handler, root.handlers)
            self.assertEqual(
                [h for h in root.handlers if getattr(h, "_alex_termviz", False)],
                [second],
            )
        finally:
            root.removeHandler(second)
            second.close()


class GlobalVisualizerTests(unittest.TestCase):
    def test_get_visualizer_returns_one_process_wide_instance(self) -> None:
        self.assertIs(get_visualizer(), get_visualizer())


if __name__ == "__main__":
    unittest.main()




class ExternalLiveTests(unittest.TestCase):
    def test_render_line_is_none_when_idle_or_disabled(self) -> None:
        viz = Visualizer(console=plain_console(), enabled=False)
        self.assertIsNone(viz.render_line())

        viz = Visualizer(console=plain_console(enable=True), enabled=True)
        self.assertIsNone(viz.render_line())
        with viz.task("search.web", "prices"):
            line = viz.render_line()
        self.assertIsNotNone(line)
        self.assertIn("prices", line.plain)
        self.assertIn("s", line.plain)

    def test_defers_to_an_external_live_region(self) -> None:
        from rich.live import Live
        from rich.text import Text

        console = plain_console(enable=True)
        viz = Visualizer(console=console, fps=30, enabled=True)
        external = Live(Text("hud"), console=console, transient=True)
        external.start()
        try:
            with viz.task("search.web", "prices"):
                self.assertTrue(viz._external_live_active())
                self.assertIsNone(viz._live)
                self.assertTrue(viz.status()["joined_hud"])
            self.assertIsNone(viz._live)
        finally:
            external.stop()
            viz.close()

    def test_adopt_console_stops_own_live_and_rebinds(self) -> None:
        """Adopting the HUD's console tears down our old-console region."""
        console_a = plain_console(enable=True)
        console_b = plain_console(enable=True)
        viz = Visualizer(console=console_a, fps=30, enabled=True)
        try:
            with viz.task("search.web", "prices"):
                self.assertIsNotNone(viz._live)
                viz.adopt_console(console_b)
                self.assertIsNone(viz._live)      # old-console region stopped
                self.assertIsNone(viz._own_live)
                self.assertIs(viz.console, console_b)
                self.assertIsNotNone(viz.render_line())   # task still animates

            viz.adopt_console(None)               # no-op
            viz.adopt_console(console_b)          # idempotent
        finally:
            viz.close()

    def test_hud_takes_ownership_mid_task(self) -> None:
        """An HUD started *during* a task stops our internal Live."""
        import livekit.agents.cli._legacy as legacy
        from rich.console import Group
        from rich.live import Live
        from rich.text import Text

        from termviz import patch_console_waveform

        console = plain_console(enable=True)
        viz = Visualizer(console=console, fps=30, enabled=True)

        original = legacy.FrequencyVisualizer
        original_live = legacy.Live
        flag = getattr(legacy, "_termviz_waveform_patch", False)
        try:
            patch_console_waveform(visualizer=viz)
            hud_class = legacy.FrequencyVisualizer
            hud = object.__new__(hud_class)          # no AgentsConsole needed

            with viz.task("search.web", "prices"):
                self.assertIsNotNone(viz._live)      # internal Live is ours
                external = Live(Text("hud"), console=console, transient=True)
                external.start()
                try:
                    out = hud.__rich__()             # first HUD frame
                    self.assertIsNone(viz._live)     # ours handed over
                    self.assertIsInstance(out, Group)
                    self.assertIn("prices", str(out.renderables[0].plain))
                finally:
                    external.stop()
        finally:
            legacy.FrequencyVisualizer = original
            legacy.Live = original_live
            if flag:
                setattr(legacy, "_termviz_waveform_patch", True)
            elif getattr(legacy, "_termviz_waveform_patch", False):
                delattr(legacy, "_termviz_waveform_patch")
            viz.close()


class LiveKitHUDIntegrationTests(unittest.TestCase):
    def test_patch_is_idempotent(self) -> None:
        import livekit.agents.cli._legacy as legacy
        from termviz import patch_console_waveform

        original = legacy.FrequencyVisualizer
        original_live = legacy.Live
        flag = getattr(legacy, "_termviz_waveform_patch", False)
        try:
            self.assertTrue(patch_console_waveform())
            patched = legacy.FrequencyVisualizer
            self.assertFalse(patch_console_waveform())
            self.assertIs(legacy.FrequencyVisualizer, patched)
        finally:
            legacy.FrequencyVisualizer = original
            legacy.Live = original_live
            if flag:
                setattr(legacy, "_termviz_waveform_patch", True)
            elif getattr(legacy, "_termviz_waveform_patch", False):
                delattr(legacy, "_termviz_waveform_patch")

    def test_patched_hud_wraps_the_task_line(self) -> None:
        import livekit.agents.cli._legacy as legacy
        from rich.text import Text as RichText
        from termviz import patch_console_waveform

        original = legacy.FrequencyVisualizer
        original_live = legacy.Live
        flag = getattr(legacy, "_termviz_waveform_patch", False)
        try:
            self.assertTrue(patch_console_waveform())
            hud_class = legacy.FrequencyVisualizer
            hud = object.__new__(hud_class)          # no AgentsConsole needed

            # With no task, the HUD keeps its own layout (\u58caless fallback).
            self.assertIsInstance(hud.__rich__(), RichText)

            console = plain_console(enable=True)
            viz = Visualizer(
                console=console,
                fps=30,
                enabled=True,
            )

            with viz.task("reach.web_read", "reply text"):
                self.assertIn("reply text", viz.render_line().plain)
        finally:
            legacy.FrequencyVisualizer = original
            legacy.Live = original_live
            if flag:
                setattr(legacy, "_termviz_waveform_patch", True)
            elif getattr(legacy, "_termviz_waveform_patch", False):
                delattr(legacy, "_termviz_waveform_patch")
