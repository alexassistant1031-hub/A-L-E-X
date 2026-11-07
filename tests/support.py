"""Shared fixtures for controller and file-tool tests.

Every test gets its own temporary project, working directory, backup root and
audit root.  Nothing here ever touches the real ``/var/backups/alex`` or
``/var/log/alex-controller`` locations.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from controller import (
    ControllerConfig,
    reset_audits,
    reset_backup_manager,
    reset_config,
    reset_modification_controller,
)


def reset_controller_caches() -> None:
    reset_audits()
    reset_backup_manager()
    reset_modification_controller()
    reset_config()


def build_env(base: Path) -> ControllerConfig:
    """Create a disposable project and return a config pointing at it."""
    project = base / "project"
    project.mkdir()
    (project / "staging").mkdir()
    (project / "tools").mkdir()
    (project / "core.py").write_text("ORIGINAL\n", encoding="utf-8")
    (project / "tools" / "helper.py").write_text("HELPER\n", encoding="utf-8")

    work = base / "work"
    work.mkdir()

    return ControllerConfig(
        project_root=project,
        staging_root=project / "staging",
        backup_root=base / "backups",
        audit_root=base / "audit",
        allowed_write_roots=(work,),
        trash_root=base / "trash",
        backup_retention=5,
        backup_excludes=(),  # include everything in the tiny test project
        backup_coalesce_seconds=0,  # snapshot every mutation
    )


class EnvMixin:
    """Set up a disposable project for a test case."""

    def setUp(self) -> None:
        reset_controller_caches()
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-test-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(reset_controller_caches)
        self.base = Path(self._tmp.name)
        self.config = build_env(self.base)

    # convenience accessors
    @property
    def project(self) -> Path:
        return self.config.project_root

    @property
    def staging(self) -> Path:
        return self.config.staging_root

    @property
    def work(self) -> Path:
        return self.config.allowed_write_roots[0]

    @property
    def backup_root(self) -> Path:
        return self.config.backup_root

    @property
    def audit_root(self) -> Path:
        return self.config.audit_root


class TempEnvTestCase(EnvMixin, unittest.TestCase):
    """Synchronous base class for controller tests."""


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
