"""
tools/shell_tool.py

Persistent, permission-checked shell access for Alex.

Design
------
* One :class:`ShellTool` instance keeps the working directory across calls, so
  ``cd ~/Documents`` in one turn and ``ls`` in the next behave as the user
  expects.  Each command still runs in a fresh non-interactive shell, which
  keeps the process table clean and makes timeouts reliable.
* Every command is classified by :mod:`tools.permissions` before anything runs:

  ``allow``  routine, read-only or explicitly safe command -> executed
  ``ask``    sensitive or unknown -> executed when the call is authorized;
             the operator's standing authorization covers it
  ``deny``   catastrophic, privileged or credential-exposing -> never executed

* The operator has granted standing authorization for this machine, so the
  execution boundary marks calls ``authorized=True``.  An ``ask``-tier command
  then runs immediately without a confirmation prompt.  The ``deny`` tier
  cannot be authorized and always refuses.

* With ``ALEX_AUTO_CONFIRM=0`` the old flow returns: self-initiated work needs
  a short-lived, single-use token bound to the exact command and working
  directory.  Tokens cannot be reused, cannot be moved to another command and
  expire after ``ALEX_SHELL_APPROVAL_TTL`` seconds (default 120).
* The command is written to the controller-owned ``shell`` audit stream before
  it runs.  If that audit write fails, the command is not executed (fail
  closed).  Audit fields are redacted and never contain output.
* Output is capped, ANSI codes are stripped, and the whole process group is
  killed on timeout so a runaway command cannot linger.

This is a policy boundary, not an OS sandbox.  Commands run as the desktop
user; there is no privilege escalation path through this tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from controller import (
    ControllerConfig,
    ControllerError,
    SecureAuditLog,
    get_audit,
    get_config,
)

from .base import BaseTool
from .permissions import (
    ALLOW,
    ASK,
    DEFAULT_APPROVAL_TTL,
    DEFAULT_MODE,
    DENY,
    MODES,
    ApprovalStore,
    Decision,
    analyze,
    describe_decision,
    parse_segments,
)

logger = logging.getLogger("alex.tools.shell")

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
# Graphical launchers (especially xdg-open) can block forever with no browser
# or display.  Cap them so one failed attempt cannot stall a voice turn.
LAUNCHER_TIMEOUT = 10
LAUNCHER_PROGRAMS = frozenset(
    {
        "xdg-open",
        "xdg-email",
        "open",
        "sensible-browser",
        "x-www-browser",
        "www-browser",
        "gnome-www-browser",
        "kde-open5",
        "firefox",
        "firefox-esr",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "microsoft-edge",
        "epiphany",
    }
)
DEFAULT_MAX_OUTPUT = 20_000
MAX_OUTPUT_BYTES = 1024 * 1024  # hard ceiling while draining a pipe


def _launches_gui(command: str) -> bool:
    """True when any command segment is a graphical launcher."""
    try:
        segments = parse_segments(command)
    except Exception:
        return False
    for segment in segments:
        program = Path(segment[0]).name if segment else ""
        # ``gio`` is multi-purpose (copy, trash, ...); only its open/launch
        # subcommands are GUI launchers.
        if program == "gio":
            if len(segment) > 1 and segment[1] in {"open", "launch"}:
                return True
            continue
        if program in LAUNCHER_PROGRAMS:
            return True
    return False

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_]*)"
    r"\s*=\s*([^\s]+)"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--(?:token|password|passwd|secret|api-key|apikey)\s+)(\S+)"
)
_AUTH_HEADER = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)(\S+)"
)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", name, raw)
        return default
    return max(minimum, value)


def _redact(command: str) -> str:
    """Remove obvious secrets before writing a command to the log."""
    text = _SECRET_ASSIGNMENT.sub(r"\1=[redacted]", command)
    text = _SECRET_FLAG.sub(r"\1[redacted]", text)
    text = _AUTH_HEADER.sub(r"\1[redacted]", text)
    return text


def _clean_output(text: str) -> str:
    return _ANSI.sub("", text).replace("\x00", "")


class ShellTool(BaseTool):
    """Run shell commands under an allow/ask/deny policy with persistent cwd."""

    name = "shell"
    description = (
        "SHELL TOOL — run a terminal command on the user's machine. Routine "
        "commands run immediately; sensitive commands need explicit user "
        "approval; destructive and privileged commands are always refused. "
        "The working directory persists between calls."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to run. One line; pipes, && and || are "
                    "supported. Command substitution, heredocs and nested "
                    "shells are refused."
                ),
            },
            "approval_token": {
                "type": "string",
                "description": (
                    "Only set this after the user explicitly approved the exact "
                    "command. Use the token returned by the previous refusal."
                ),
                "default": "",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds before the command is stopped (1-300).",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        *,
        config: ControllerConfig | None = None,
        audit: SecureAuditLog | None = None,
        approvals: ApprovalStore | None = None,
        mode: str | None = None,
        default_cwd: str | Path | None = None,
    ) -> None:
        self.config = config or get_config()
        self._audit_sink = audit
        requested_mode = (mode or os.getenv("ALEX_SHELL_MODE", DEFAULT_MODE)).strip().lower()
        if requested_mode not in MODES:
            logger.warning("Unknown ALEX_SHELL_MODE=%r; using %s", requested_mode, DEFAULT_MODE)
            requested_mode = DEFAULT_MODE
        self.mode = requested_mode

        ttl = _env_float("ALEX_SHELL_APPROVAL_TTL", DEFAULT_APPROVAL_TTL, minimum=5.0)
        self.approvals = approvals or ApprovalStore(ttl)
        self.approval_ttl = self.approvals.ttl

        self.default_timeout = _env_int(
            "ALEX_SHELL_TIMEOUT", DEFAULT_TIMEOUT, minimum=1, maximum=MAX_TIMEOUT
        )
        self.max_output_chars = _env_int(
            "ALEX_SHELL_MAX_OUTPUT", DEFAULT_MAX_OUTPUT, minimum=500, maximum=MAX_OUTPUT_BYTES
        )

        start = Path(default_cwd).expanduser() if default_cwd else self.config.project_root
        try:
            start = start.resolve(strict=False)
        except OSError:
            start = self.config.project_root
        if not start.is_dir():
            start = self.config.project_root
        self._cwd = start
        self._oldcwd = start
        self._lock = threading.Lock()

    # ── public state ──────────────────────────────────────────────────────────

    @property
    def cwd(self) -> Path:
        return self._cwd

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cwd": str(self._cwd),
            "approval_ttl": int(self.approval_ttl),
            "timeout": self.default_timeout,
            "max_output_chars": self.max_output_chars,
        }

    # ── audit ─────────────────────────────────────────────────────────────────

    def _audit_log(self) -> SecureAuditLog:
        return self._audit_sink or get_audit("shell", self.config)

    def _record(self, event: str, **fields: Any) -> bool:
        try:
            self._audit_log().write(event, **fields)
            return True
        except (ControllerError, OSError) as exc:
            logger.critical("Secure shell audit write failed (%s): %s", event, exc)
            return False
        except Exception as exc:  # audit must never crash the tool
            logger.critical("Secure shell audit write failed (%s): %s", event, exc)
            return False

    # ── entry point ───────────────────────────────────────────────────────────

    async def execute(
        self,
        command: str,
        approval_token: str = "",
        timeout: int | None = None,
        authorized: bool = False,
    ) -> str:
        # A single lock keeps cwd updates and approval consumption consistent
        # if the model ever issues two shell calls at once.
        return await asyncio.to_thread(
            self._run_locked, command, approval_token, timeout, authorized
        )

    def _run_locked(
        self,
        command: str,
        approval_token: str,
        timeout: int | None,
        authorized: bool = False,
    ) -> str:
        with self._lock:
            return self._run(command, approval_token, timeout, authorized)

    def _run(
        self,
        command: str,
        approval_token: str,
        timeout: int | None,
        authorized: bool = False,
    ) -> str:
        command = (command or "").strip()
        if not command:
            return "Refused: the command is empty."

        seconds = self._effective_timeout(command, timeout)
        decision = analyze(
            command, cwd=self._cwd, mode=self.mode, authorized=authorized
        )
        preview = _redact(command)[:500]

        if decision.tier == DENY:
            self._record(
                "shell_command_denied",
                command=preview,
                cwd=str(self._cwd),
                reason=decision.reason,
            )
            return (
                f"Refused: {decision.reason}. This action is never available "
                "through the shell; do not try another command or tool to "
                "achieve it."
            )

        if decision.tier == ASK:
            if not (approval_token or "").strip():
                token = self.approvals.issue(command, str(self._cwd), decision.reason)
                self._record(
                    "shell_approval_requested",
                    command=preview,
                    cwd=str(self._cwd),
                    reason=decision.reason,
                )
                return (
                    f"Approval required: {decision.reason}.\n"
                    "Explain this to the user and wait for a clear yes. Only "
                    "then call shell again with the exact same command and "
                    f'approval_token="{token}". The token expires in '
                    f"{int(self.approval_ttl)} seconds and works once."
                )

            ok, why = self.approvals.consume(
                approval_token, command, str(self._cwd)
            )
            if not ok:
                self._record(
                    "shell_approval_rejected",
                    command=preview,
                    cwd=str(self._cwd),
                    reason=why,
                )
                return (
                    f"Refused: {why}. Ask the user again if they still want "
                    "this command; a new request will issue a new token."
                )

        # Fail closed: no command runs unless the secure audit log accepted
        # the intent record.
        if not self._record(
            "shell_command_requested",
            command=preview,
            command_sha256=_sha256(command),
            cwd=str(self._cwd),
            tier=decision.tier,
            reason=decision.reason,
            authorized=decision.authorized,
            timeout=seconds,
            mode=self.mode,
        ):
            return (
                "Error: the secure shell audit log is unavailable, so the "
                "command was not run. Check the controller storage and try "
                "again."
            )

        started = time.monotonic()
        result = self._execute(command, seconds)
        duration_ms = int((time.monotonic() - started) * 1000)

        # Reflect the character cap in the audit record as well as the reply.
        if len(str(result["output"])) > self.max_output_chars:
            result["truncated"] = True

        audited = self._record(
            "shell_command_completed",
            command=preview,
            command_sha256=_sha256(command),
            cwd=str(result["start_cwd"]),
            cwd_after=str(self._cwd),
            exit_code=result["exit_code"],
            timed_out=result["timed_out"],
            duration_ms=duration_ms,
            output_bytes=result["output_bytes"],
            output_truncated=result["truncated"],
        )
        formatted = self._format_result(command, result, seconds)
        if not audited:
            formatted += (
                "\n\nWARNING: the command ran, but the secure shell audit log "
                "could not be updated. Stop and check the controller audit "
                "storage."
            )
        return formatted

    def _clamp_timeout(self, timeout: int | None) -> int:
        if timeout is None:
            return self.default_timeout
        try:
            value = int(timeout)
        except (TypeError, ValueError):
            return self.default_timeout
        return max(1, min(MAX_TIMEOUT, value))

    def _effective_timeout(self, command: str, timeout: int | None) -> int:
        """Timeout for this command, with a short leash on GUI launchers.

        An explicit caller timeout is honoured; only the default is capped,
        so opening a browser fails fast instead of blocking for a full minute.
        """
        seconds = self._clamp_timeout(timeout)
        if timeout is None and _launches_gui(command):
            return min(seconds, LAUNCHER_TIMEOUT)
        return seconds

    # ── execution ─────────────────────────────────────────────────────────────

    def _execute(self, command: str, timeout: int) -> dict[str, Any]:
        start_cwd = self._cwd if self._cwd.is_dir() else self.config.project_root
        nonce = secrets.token_hex(8)
        marker = f"__ALEX_CWD_{nonce}__"
        rc = f"_alex_rc_{nonce}"
        # Preserve the command's exit status, then report the final directory.
        wrapped = (
            f"{command}\n{rc}=$?; "
            f"printf '\\n{marker}%s\\n' \"$PWD\"; "
            f"exit ${rc}"
        )

        env = os.environ.copy()
        # Keep commands non-interactive: no pagers, credential prompts or
        # colour escape sequences.
        env.update(
            {
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "TERM": "dumb",
                "NO_COLOR": "1",
                "PIP_NO_INPUT": "1",
                "DEBIAN_FRONTEND": "noninteractive",
            }
        )

        try:
            proc = subprocess.Popen(
                wrapped,
                shell=True,
                cwd=str(start_cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                text=False,
            )
        except OSError as exc:
            return {
                "start_cwd": start_cwd,
                "exit_code": 126,
                "timed_out": False,
                "output": f"Could not start the command: {exc}",
                "output_bytes": 0,
                "truncated": False,
            }

        state: dict[str, Any] = {"data": b"", "truncated": False}

        def drain() -> None:
            stream = proc.stdout
            if stream is None:
                return
            chunks = bytearray()
            truncated = False
            while True:
                try:
                    chunk = stream.read1(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                if len(chunks) < MAX_OUTPUT_BYTES:
                    room = MAX_OUTPUT_BYTES - len(chunks)
                    chunks.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
                # Publish incrementally: a background child may keep the pipe
                # open after the direct child exits.
                state["data"] = bytes(chunks)
                state["truncated"] = truncated
            state["data"] = bytes(chunks)
            state["truncated"] = truncated

        reader = threading.Thread(target=drain, name="alex-shell-output", daemon=True)
        reader.start()

        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = -signal.SIGKILL

        reader.join(timeout=5)
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass

        raw = state["data"]
        decoded = raw.decode("utf-8", errors="replace")
        text = _clean_output(decoded)
        text, reported_cwd = self._consume_marker(text, marker)

        if reported_cwd is not None:
            self._set_cwd(reported_cwd)
        else:
            # The shell may have exited before the trailer ran (set -e, exit,
            # timeout).  Fall back to following a leading cd.
            self._follow_cd(command)

        return {
            "start_cwd": start_cwd,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output": text,
            "output_bytes": len(raw),
            "truncated": state["truncated"],
        }

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ── cwd tracking ──────────────────────────────────────────────────────────

    def _consume_marker(self, text: str, marker: str) -> tuple[str, Path | None]:
        index = text.rfind(marker)
        if index == -1:
            return text, None
        tail = text[index + len(marker) :]
        value = tail.split("\n", 1)[0].strip()
        cleaned = text[:index].rstrip("\n")
        if not value:
            return cleaned, None
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            return cleaned, candidate
        return cleaned, None

    def _set_cwd(self, new: Path) -> None:
        if new == self._cwd:
            return
        self._oldcwd = self._cwd
        self._cwd = new

    def _follow_cd(self, command: str) -> None:
        """Best-effort cwd update when the shell did not report $PWD."""
        try:
            segments = parse_segments(command)
        except ValueError:
            return
        for segment in segments:
            if not segment or segment[0] != "cd":
                continue
            args = segment[1:]
            target = args[0] if args else "~"
            if target == "-":
                candidate = self._oldcwd
            else:
                candidate = Path(target).expanduser()
                if not candidate.is_absolute():
                    candidate = self._cwd / candidate
                candidate = candidate.resolve(strict=False)
            if candidate.is_dir():
                self._set_cwd(candidate)

    # ── result formatting ─────────────────────────────────────────────────────

    def _format_result(
        self, command: str, result: dict[str, Any], timeout: int
    ) -> str:
        safe = _redact(command)
        shown = safe if len(safe) <= 500 else safe[:500] + "…"
        output = _clean_output(str(result["output"])).strip()
        if len(output) > self.max_output_chars:
            output = output[: self.max_output_chars]
            result["truncated"] = True

        lines = [f"$ {shown}", f"(cwd: {result['start_cwd']})"]
        if result["timed_out"]:
            lines.append(f"Timed out after {timeout}s; the process group was killed.")
        lines.append(f"Exit code: {result['exit_code']}")
        lines.append(output if output else "(no output)")
        if result["truncated"]:
            lines.append(
                f"[output truncated; showing at most {self.max_output_chars} characters]"
            )
        if self._cwd != result["start_cwd"]:
            lines.append(f"Working directory is now {self._cwd}")
        return "\n".join(lines)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def backend_status(config: ControllerConfig | None = None) -> dict[str, Any]:
    """Return the shell policy that the running process will use."""
    tool = ShellTool(config=config)
    return tool.status()


__all__ = [
    "ShellTool",
    "backend_status",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "LAUNCHER_TIMEOUT",
    "MODES",
    "ALLOW",
    "ASK",
    "DENY",
    "Decision",
    "describe_decision",
]
