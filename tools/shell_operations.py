"""
tools/shell_operations.py

Exposes the permission-checked shell as a LiveKit function tool.
"""

from __future__ import annotations

import logging

from livekit.agents import llm

from termviz import get_visualizer

from .shell_tool import DEFAULT_TIMEOUT, ShellTool

logger = logging.getLogger("alex.tools.shell_ops")

_shell_tool = ShellTool()


@llm.function_tool
async def shell(
    command: str,
    approval_token: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    authorized: bool = False,
) -> str:
    """Run a terminal command on the user's machine.

    Use when the user asks you to run a command, check the system, inspect a
    repository, run tests, or do something a file tool cannot do. The working
    directory persists between calls, so `cd` in one call affects the next.

    Permission: a direct instruction from the user is full authorization. If
    the user asked for this command (or for the action it performs), set
    `authorized=true` and it runs immediately with no extra confirmation. Do
    not ask the user to confirm something they already asked you to do.

    Policy: routine read-only commands always run. Sensitive commands (rm, mv,
    cp, installs, network access, interpreters, test runners, unknown
    programs) also run when `authorized=true`; without it they return
    "Approval required" so the user can decide. Privileged or destructive
    commands (sudo, shutdown, disk tools, credential files) are always refused
    and cannot be authorized.

    Approval protocol (only when the user has *not* already asked): if the
    tool answers "Approval required", explain the command and its effect and
    wait for a clear yes. Then call the tool again with the same command,
    `authorized=true`, and the returned approval_token. The token is
    single-use, expires quickly, and is bound to that exact command and
    directory.

    Never use the shell to edit protected project files or to get around a
    file-tool denial. Use edit_file for code changes; stage self-modifications
    under staging/.

    Args:
        command: One-line shell command. Pipes, && and || are supported.
        approval_token: Token from a previous "Approval required" reply, used
            only together with an explicit yes.
        timeout: Seconds before the process group is stopped (1-300).
        authorized: True when the user explicitly asked for this command or
            action in their current message. That is enough; no confirmation
            prompt is needed.
    """
    label = (command.strip().split(maxsplit=1) or ["shell"])[0][:40]
    with get_visualizer().task("shell", label):
        result = await _shell_tool.execute(
            command=command,
            approval_token=approval_token,
            timeout=timeout,
            authorized=authorized,
        )
    logger.info("shell %s -> %s", command[:80], result[:80].replace("\n", " "))
    return result


SHELL_TOOLS = [shell]

__all__ = ["shell", "SHELL_TOOLS"]
