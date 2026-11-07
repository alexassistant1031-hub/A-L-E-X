"""Command permission tiers for Alex's shell tool.

This is the policy layer, deliberately independent of process execution so it
can be unit-tested without running anything.

Tiers
-----
``allow``   Known read-only or routine command.  Runs immediately.
``ask``     Sensitive or unknown command.  Runs immediately when the user
            asked for it (``authorized=True``); otherwise refused until the
            user approves.  Approval is bound to one exact command line and
            working directory, expires after a short time, and is single-use.
``deny``    Catastrophic, privileged or credential-exposing command.  Can never
            be approved or authorized through the tool.

This is a *policy* boundary, not an OS sandbox.  It keeps accidental damage and
casual prompt injection in check, but the process still runs as the desktop
user.  Root access is denied by construction (``sudo``, ``su``, ``pkexec``,
``doas``), and sensitive credential paths are refused even for read commands.

Modes
-----
``guarded`` (default)  allow-list runs; everything unknown asks; deny-list wins.
``trusted``            unknown commands run without asking; deny-list still wins.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import os
import re
import secrets
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

_RANK = {ALLOW: 0, ASK: 1, DENY: 2}

MODES = frozenset({"guarded", "trusted"})
DEFAULT_MODE = "guarded"
DEFAULT_APPROVAL_TTL = 120.0
MAX_PENDING_APPROVALS = 32


@dataclass(frozen=True)
class Decision:
    """The outcome of analysing one command segment."""

    tier: str
    reason: str
    program: str = ""
    authorized: bool = False

    def worse(self, other: "Decision") -> "Decision":
        if _RANK[other.tier] > _RANK[self.tier]:
            return other
        return self


class ApprovalStore:
    """Short-lived, single-use approvals bound to (command, cwd)."""

    def __init__(self, ttl: float = DEFAULT_APPROVAL_TTL) -> None:
        self.ttl = max(5.0, float(ttl))
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, float, str]] = {}

    @staticmethod
    def _digest(command: str, cwd: str) -> str:
        return hashlib.sha256(f"{cwd}\x00{command}".encode("utf-8")).hexdigest()

    def _prune(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, exp, _) in self._pending.items() if exp <= now]:
            self._pending.pop(token, None)

    def issue(self, command: str, cwd: str, reason: str) -> str:
        with self._lock:
            self._prune()
            while len(self._pending) >= MAX_PENDING_APPROVALS:
                self._pending.pop(next(iter(self._pending)))
            token = secrets.token_hex(4)
            self._pending[token] = (
                self._digest(command, cwd),
                time.monotonic() + self.ttl,
                reason[:200],
            )
            return token

    def consume(self, token: str, command: str, cwd: str) -> tuple[bool, str]:
        token = (token or "").strip()
        if not token:
            return False, "no approval token was supplied"
        with self._lock:
            self._prune()
            entry = self._pending.get(token)
            if entry is None:
                return False, "that approval token is unknown or has expired"
            digest, expires, _ = entry
            if expires <= time.monotonic():
                self._pending.pop(token, None)
                return False, "that approval token has expired"
            if not hmac.compare_digest(digest, self._digest(command, cwd)):
                return False, "that approval token belongs to a different command"
            self._pending.pop(token, None)
            return True, "approved"

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


# ── tokenizer ────────────────────────────────────────────────────────────────

_CONTROL = frozenset({";", "&&", "||", "|", "|&", "&", ";;", "(", ")"})
_REDIRECT = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DANGEROUS_ASSIGNMENTS = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "BASH_ENV",
        "ENV",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "NODE_OPTIONS",
        "GIT_SSH_COMMAND",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "PAGER",
        "EDITOR",
        "VISUAL",
        "IFS",
    }
)


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _CONTROL:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _basename(program: str) -> str:
    return os.path.basename(program.rstrip("/")) or program


def parse_segments(command: str) -> list[list[str]]:
    """Split *command* into simple-command segments.

    Public so the shell tool can follow ``cd`` without duplicating the
    tokenizer and its quoting rules.
    """
    return _segments(_tokens(command))


# ── sensitive paths ─────────────────────────────────────────────────────────

_SENSITIVE_DIRS = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".password-store",
        ".local/share/keyrings",
        ".config/gcloud",
        ".config/gh",
        ".config/opencode",
    }
)
_SENSITIVE_FILES = (
    ".env",
    ".env.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "credentials.txt",
    "credentials.csv",
    "credentials.ini",
    "credentials.cfg",
    "secrets",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.txt",
    "secrets.env",
    "*token*.json",
    "*token*.yaml",
    "*token*.yml",
    "*token*.txt",
    "*secret*.json",
    "*secret*.yaml",
    "*secret*.yml",
    "*password*.json",
    "*password*.yaml",
    "*password*.yml",
    "Cookies",
    "Login Data",
)
_METADATA_PROGRAMS = frozenset({"ls", "stat", "file", "du", "df", "wc", "tree", "cd", "realpath", "readlink"})

# `find -exec`, `xargs`, `timeout`, `command`, ... can hide the real program.
_EXEC_MARKERS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "{"})
_WRAPPER_NAMES = frozenset(
    {
        "command", "builtin", "env", "nice", "nohup", "setsid", "timeout",
        "stdbuf", "ionice", "taskset", "watch", "xargs", "parallel", "find",
    }
)

# Environment variable names that must never be expanded through the shell.
_SECRET_VAR = re.compile(
    r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
_SECRET_NAME_WORDS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
    "SESSION", "COOKIE", "PRIVATE",
)
_SENSITIVE_ABS_PREFIXES = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/ssl/private",
)
_PROC_SENSITIVE = frozenset(
    {"/proc/kcore", "/proc/keys", "/proc/self/mem", "/proc/self/environ"}
)


def _is_sensitive_path(raw: str, cwd: Path) -> bool:
    value = raw.strip().strip("'\"")
    if not value or value.startswith("-") or value in {"-", "/dev/null"}:
        return False
    if "://" in value:
        # URLs are handled by the network-command rules, not path rules.
        return False
    if ":" in value:
        # git's <rev>:<path> syntax and similar wrappers.
        if any(
            part and _is_sensitive_path(part, cwd)
            for part in value.split(":")
        ):
            return True
    value = os.path.expanduser(value)
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    # Normalise "." / ".." and follow symlinks before the check; otherwise
    # `cd /etc && cat ./shadow` would look like an innocent relative name.
    try:
        path = path.resolve(strict=False)
    except OSError:
        pass
    text = str(path)
    if any(text == p or text.startswith(p.rstrip("/") + "/") for p in _SENSITIVE_ABS_PREFIXES):
        return True
    if text in _PROC_SENSITIVE or (
        text.startswith("/proc/") and text.endswith("/environ")
    ):
        return True
    parts = [part.lower() for part in path.parts]
    joined = "/".join(parts)
    for marker in _SENSITIVE_DIRS:
        if marker in joined:
            return True
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_FILES)


# ── program tables ──────────────────────────────────────────────────────────

_DENY_PROGRAMS = {
    "sudo": "privilege escalation is never available through the shell",
    "su": "privilege escalation is never available through the shell",
    "doas": "privilege escalation is never available through the shell",
    "pkexec": "privilege escalation is never available through the shell",
    "runuser": "privilege escalation is never available through the shell",
    "chroot": "chroot changes the filesystem root",
    "shutdown": "system power control is not exposed to the shell",
    "reboot": "system power control is not exposed to the shell",
    "poweroff": "system power control is not exposed to the shell",
    "halt": "system power control is not exposed to the shell",
    "init": "system power control is not exposed to the shell",
    "telinit": "system power control is not exposed to the shell",
    "kexec": "system power control is not exposed to the shell",
    "mkfs": "filesystem creation/destruction is blocked",
    "mkswap": "swap creation/destruction is blocked",
    "fdisk": "partition editing is blocked",
    "parted": "partition editing is blocked",
    "wipefs": "filesystem destruction is blocked",
    "mount": "mounting filesystems is not exposed to the shell",
    "umount": "unmounting filesystems is not exposed to the shell",
    "swapoff": "swap control is not exposed to the shell",
    "passwd": "account changes are not exposed to the shell",
    "chpasswd": "account changes are not exposed to the shell",
    "useradd": "account changes are not exposed to the shell",
    "userdel": "account changes are not exposed to the shell",
    "usermod": "account changes are not exposed to the shell",
    "groupadd": "account changes are not exposed to the shell",
    "groupdel": "account changes are not exposed to the shell",
    "visudo": "sudoers editing is blocked",
    "iptables": "firewall changes are not exposed to the shell",
    "nft": "firewall changes are not exposed to the shell",
    "cryptsetup": "disk encryption control is blocked",
    "debugfs": "raw filesystem access is blocked",
    "insmod": "kernel module changes are blocked",
    "rmmod": "kernel module changes are blocked",
    "modprobe": "kernel module changes are blocked",
    "setcap": "capability changes are blocked",
}

_ALLOW_PROGRAMS = {
    "ls", "dir", "vdir", "pwd", "basename", "dirname", "realpath", "readlink",
    "cat", "head", "tail", "grep", "rg", "ag", "ack", "wc", "sort", "uniq",
    "cut", "tr", "nl", "tac", "rev", "column", "comm", "cmp", "diff", "paste",
    "join", "fold", "fmt", "expand", "unexpand", "od", "xxd", "base64",
    "strings", "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum",
    "echo", "printf", "true", "false", "test", "[", "date", "cal", "uptime",
    "whoami", "id", "groups", "uname", "hostname", "arch", "nproc", "free",
    "df", "du", "stat", "file", "tree", "which", "whereis", "type", "command",
    "ps", "pgrep", "lsof", "dmesg", "journalctl", "systemctl", "man", "help",
    "git", "gh", "python", "python3", "pip", "pip3", "pytest", "ruff", "mypy",
    "flake8", "node", "npm", "tar", "jq", "yq", "sqlite3", "make", "cmake",
    "cargo", "go", "java",
    "sed", "awk", "find", "ip", "ss", "netstat",
    "env", "printenv", "export", "unset", "alias", "unalias", "history",
    "sleep", "timeout", "watch", "cd", "dirs",
}


# ── per-program rules ───────────────────────────────────────────────────────

def _git_decision(args: list[str]) -> Decision:
    if not args:
        return Decision(ALLOW, "git with no subcommand", "git")
    if any(a == "--output" or a.startswith("--output=") for a in args):
        return Decision(ASK, "git --output writes a file", "git")
    sub = args[0]
    rest = args[1:]
    if sub == "grep":
        return Decision(
            ASK,
            "git grep reads tracked file contents, which may include credentials",
            "git",
        )
    if sub == "log" and any(f in rest for f in ("-p", "--patch", "-u", "--raw", "--full-diff")):
        return Decision(ASK, "git log -p prints full diffs, which may include secrets", "git")
    if sub == "show" and not any(
        f in rest for f in ("--stat", "--name-only", "--name-status", "-s", "--no-patch")
    ):
        return Decision(ASK, "git show prints a full diff, which may include secrets", "git")
    if sub == "diff" and not any(
        f in rest
        for f in ("--stat", "--numstat", "--shortstat", "--name-only", "--name-status", "--check")
    ):
        return Decision(ASK, "git diff prints file contents, which may include secrets", "git")
    safe = {
        "status", "diff", "log", "show", "rev-parse", "ls-files", "blame",
        "describe", "shortlog", "version", "help",
    }
    if sub in {"push", "pull", "fetch", "clone", "reset", "clean", "checkout",
               "switch", "merge", "rebase", "stash", "commit", "add", "rm",
               "mv", "restore", "cherry-pick", "revert", "init", "submodule",
               "worktree", "gc", "prune", "filter-branch"}:
        return Decision(ASK, f"git {sub} changes a repository", "git")
    if sub == "config":
        if any(flag in args for flag in ("--get", "--get-all", "--list", "-l")):
            return Decision(ALLOW, "read-only git config", "git")
        return Decision(ASK, "git config can modify repository configuration", "git")
    if sub in {"branch", "tag"}:
        if not rest or all(item.startswith("-") for item in rest):
            return Decision(ALLOW, f"lists git {sub}s", "git")
        return Decision(ASK, f"git {sub} can create or delete {sub}es", "git")
    if sub == "remote":
        if not rest or rest[0] in {"-v", "--verbose", "show", "get-url"}:
            return Decision(ALLOW, "read-only git remote", "git")
        return Decision(ASK, "git remote can change repository configuration", "git")
    if sub == "reflog":
        if not rest or rest[0] == "show":
            return Decision(ALLOW, "read-only git reflog", "git")
        return Decision(ASK, "git reflog can expire history", "git")
    if sub in safe:
        return Decision(ALLOW, f"read-only git {sub}", "git")
    return Decision(ASK, f"git {sub!r} is not a known read-only subcommand", "git")


def _pip_decision(args: list[str]) -> Decision:
    if not args:
        return Decision(ALLOW, "pip with no subcommand", "pip")
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in {"list", "show", "freeze", "check", "config"} and sub != "config":
        return Decision(ALLOW, f"read-only pip {sub}", "pip")
    if sub and sub not in {"install", "uninstall", "download", "wheel"}:
        return Decision(ASK, f"pip {sub} is not a known read-only subcommand", "pip")
    return Decision(ASK, "pip can modify the Python environment", "pip")


def _python_decision(args: list[str]) -> Decision:
    if not args:
        return Decision(ASK, "an interactive Python session would block", "python")
    if any(a in {"-c", "-"} for a in args):
        return Decision(ASK, "inline Python code can do anything", "python")
    if "-m" in args:
        index = args.index("-m")
        module = args[index + 1] if index + 1 < len(args) else ""
        if module in {"pytest", "unittest"}:
            return Decision(
                ASK,
                f"python -m {module} executes project code",
                "python",
            )
        if module in {"ruff", "mypy", "flake8"}:
            return Decision(ALLOW, f"python -m {module}", "python")
        if module in {"pip", "ensurepip"}:
            return _pip_decision(args[index + 2 :])
        return Decision(ASK, f"python -m {module or '?'} runs module code", "python")
    return Decision(
        ASK, "runs a Python script, which can modify files outside the guard", "python"
    )


def _shell_decision(args: list[str], program: str) -> Decision:
    if any(a in {"-c", "-i", "-s"} for a in args):
        return Decision(DENY, "nested or inline shells hide the real command", program)
    if not args:
        return Decision(ASK, "an interactive shell would block", program)
    return Decision(ASK, f"{program} runs a script through a shell wrapper", program)


def _find_decision(args: list[str]) -> Decision:
    if any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"} for a in args):
        return Decision(ASK, "find can delete files or run commands", "find")
    return Decision(ALLOW, "read-only find", "find")


def _tar_decision(args: list[str]) -> Decision:
    if any(a.startswith("--list") or (a.startswith("-") and "t" in a[1:] and "x" not in a[1:]) for a in args):
        return Decision(ALLOW, "list archive contents", "tar")
    return Decision(ASK, "tar extraction/creation writes files", "tar")


def _sed_decision(args: list[str]) -> Decision:
    if any(a in {"-i", "--in-place"} or a.startswith("-i") for a in args):
        return Decision(ASK, "sed -i rewrites files", "sed")
    scripts = [a for a in args if not a.startswith("-")]
    for script in scripts:
        # `1e cmd`, `w file`, and the `e`/`w` flags of `s///` execute or write.
        if re.search(r"(^|[;\n])\s*[0-9,$/]*[we](\s|$)", script):
            return Decision(ASK, "sed can write files or execute commands", "sed")
        if re.search(r"(^|[;\n])\s*s(.).*?\2[^\n]*\2[gpi0-9]*[we]", script):
            return Decision(ASK, "sed can write files or execute commands", "sed")
    return Decision(ALLOW, "read-only sed", "sed")


def _awk_decision(args: list[str]) -> Decision:
    if any(a == "inplace" or a == "-i" or a.startswith("-i") for a in args):
        return Decision(ASK, "awk -i can rewrite files or load extensions", "awk")
    if any(token in a for a in args for token in ("system(", "|", "getline", "print >")):
        return Decision(ASK, "awk can run commands or write files", "awk")
    return Decision(ALLOW, "read-only awk", "awk")


def _rm_decision(args: list[str]) -> Decision:
    if "--no-preserve-root" in args:
        return Decision(DENY, "removing the filesystem root is blocked", "rm")
    home = Path.home().resolve()
    protected = {
        Path("/"),
        Path("/home"),
        Path("/usr"),
        Path("/etc"),
        Path("/var"),
        Path("/boot"),
        Path("/opt"),
        Path("/snap"),
        home,
    }
    for target in (a for a in args if not a.startswith("-")):
        raw = target.strip("'\"")
        if "$HOME" in raw or "${HOME}" in raw:
            return Decision(DENY, "refusing to remove the home directory", "rm")
        expanded = os.path.expanduser(raw)
        try:
            resolved = Path(expanded).resolve(strict=False)
        except OSError:
            resolved = Path(expanded)
        if resolved in protected:
            return Decision(DENY, f"refusing to remove {raw}", "rm")
        if str(resolved) in {"/", "/home", "/usr", "/etc", "/var", "/boot", "/opt", "/snap"}:
            return Decision(DENY, f"refusing to remove {raw}", "rm")
        if expanded.startswith("/*") or expanded.startswith("/home/*"):
            return Decision(DENY, f"refusing to remove {raw}", "rm")
    return Decision(ASK, "rm permanently removes files (outside the trash)", "rm")


def _chmod_decision(args: list[str], program: str) -> Decision:
    home = Path.home().resolve()
    protected = {
        Path("/"),
        Path("/home"),
        Path("/usr"),
        Path("/etc"),
        Path("/bin"),
        Path("/sbin"),
        Path("/lib"),
        Path("/boot"),
        Path("/var"),
        home,
    }
    for target in args:
        if target.startswith("-"):
            continue
        value = target.strip("'\"")
        if "$HOME" in value or "${HOME}" in value:
            return Decision(DENY, f"refusing to {program} the home directory", program)
        expanded = os.path.expanduser(value)
        try:
            resolved = Path(expanded).resolve(strict=False)
        except OSError:
            resolved = Path(expanded)
        if resolved in protected:
            return Decision(DENY, f"refusing to {program} {value}", program)
    return Decision(ASK, f"{program} changes permissions or ownership", program)


def _kill_decision(args: list[str], program: str) -> Decision:
    if any(a == "1" for a in args if not a.startswith("-")):
        return Decision(DENY, "refusing to signal PID 1", program)
    return Decision(ASK, f"{program} signals running processes", program)


def _systemctl_decision(args: list[str]) -> Decision:
    sub = args[0] if args else ""
    if sub in {"status", "is-active", "is-enabled", "is-failed", "list-units",
               "list-unit-files", "list-timers", "show", "cat", "help"}:
        return Decision(ALLOW, f"read-only systemctl {sub}", "systemctl")
    if sub in {"poweroff", "reboot", "halt", "suspend", "hibernate"}:
        return Decision(DENY, "system power control is blocked", "systemctl")
    return Decision(ASK, f"systemctl {sub} changes system state", "systemctl")


def _ip_decision(args: list[str]) -> Decision:
    if any(a in {"set", "add", "del", "delete", "change", "replace", "flush", "up", "down"} for a in args):
        return Decision(ASK, "network configuration changes", "ip")
    return Decision(ALLOW, "read-only ip", "ip")


def _dd_decision(args: list[str]) -> Decision:
    for arg in args:
        if arg.startswith("of="):
            target = arg[3:]
            if target.startswith("/dev/") or target == "/":
                return Decision(DENY, "writing raw disk devices is blocked", "dd")
    return Decision(ASK, "dd can overwrite data", "dd")


def _env_decision(args: list[str]) -> Decision:
    # `env` with no command dumps the environment; with a command it is a wrapper.
    if not args:
        return Decision(DENY, "dumping the whole environment can expose credentials", "env")
    return Decision(ASK, "env wraps another command", "env")


def _printenv_decision(args: list[str]) -> Decision:
    if not args:
        return Decision(DENY, "dumping the whole environment can expose credentials", "printenv")
    for name in args:
        if not name.startswith("-") and _looks_secret_name(name):
            return Decision(DENY, f"refusing to print secret variable {name}", "printenv")
    return Decision(ALLOW, "prints named environment variables", "printenv")


def _looks_secret_name(value: str) -> bool:
    upper = value.upper()
    return any(word in upper for word in _SECRET_NAME_WORDS)


_SAFE_VARS = frozenset(
    {
        "HOME", "PWD", "OLDPWD", "USER", "LOGNAME", "SHELL", "PATH", "TERM",
        "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "TMPDIR", "VIRTUAL_ENV",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "HOSTNAME",
        "UID", "EUID", "RANDOM",
    }
)


def _expansion_decision(command: str) -> Decision | None:
    """Judge variable expansion in *command*.

    Secret-looking names are denied.  Any other name outside a small set of
    harmless environment variables requires approval, because an argument such
    as ``$FILE`` can expand to a credential path that the static path check
    cannot see.
    """
    for match in _SECRET_VAR.finditer(command):
        name = match.group(1)
        if _looks_secret_name(name):
            return Decision(
                DENY, f"expanding ${name} could expose a credential"
            )
        if name not in _SAFE_VARS:
            return Decision(
                ASK, f"variable ${name} is expanded at run time and cannot be checked"
            )
    return None


def _command_decision(args: list[str], cwd: Path) -> Decision:
    """`command` runs another program, so judge what it actually runs."""
    rest = list(args)
    while rest and rest[0] in {"-p", "--"}:
        rest.pop(0)
    if rest and rest[0] in {"-v", "-V"}:
        return Decision(ALLOW, "command lookup only", "command")
    if not rest:
        return Decision(ALLOW, "command with no command", "command")
    return _analyze_segment(rest, cwd)


def _embedded_deny(segment: list[str]) -> Decision | None:
    """Deny-looking programs hidden behind wrappers must stay denied.

    ``command sudo``, ``timeout 5 sudo``, ``xargs sudo`` and
    ``find . -exec sudo`` would otherwise only be judged by the wrapper.
    """
    for index, token in enumerate(segment[1:], start=1):
        name = _basename(token)
        if name not in _DENY_PROGRAMS:
            continue
        lookback = segment[max(0, index - 4) : index]
        # `command -v sudo` only prints a path.
        if "command" in lookback and any(flag in lookback for flag in ("-v", "-V")):
            continue
        if any(
            item in _EXEC_MARKERS or _basename(item) in _WRAPPER_NAMES
            for item in lookback
        ):
            return Decision(
                DENY,
                f"{name} cannot be reached through a wrapper",
                name,
            )
    return None


def _gh_decision(args: list[str]) -> Decision:
    if not args:
        return Decision(ASK, "gh needs a subcommand", "gh")
    sub = args[0]
    if sub in {"pr", "issue", "repo", "release", "run", "workflow"}:
        verb = args[1] if len(args) > 1 else ""
        if verb in {"view", "list", "status", "checks", "diff"}:
            return Decision(ALLOW, f"read-only gh {sub} {verb}", "gh")
        return Decision(ASK, f"gh {sub} {verb} may change remote state", "gh")
    if sub in {"auth", "config"}:
        return Decision(DENY, "gh auth/config can expose or change credentials", "gh")
    return Decision(ASK, f"gh {sub} is not a known read-only subcommand", "gh")


def _interpreter_decision(program: str) -> Decision:
    return Decision(ASK, f"{program} runs arbitrary code", program)


# ── analyzer ────────────────────────────────────────────────────────────────

def analyze(
    command: str,
    *,
    cwd: str | Path | None = None,
    mode: str = DEFAULT_MODE,
    authorized: bool = False,
) -> Decision:
    """Return the strongest decision across every segment of *command*.

    ``authorized`` means the user explicitly asked for this exact action in
    their current message.  That is full authorization for the ``ask`` tier:
    it runs without a second confirmation prompt.  The ``deny`` tier is an
    absolute block and is never overridden by user authorization.
    """
    mode = mode if mode in MODES else DEFAULT_MODE
    command = (command or "").strip()
    if not command:
        return Decision(DENY, "empty command")
    if "\x00" in command or "\n" in command or "\r" in command:
        return Decision(DENY, "multi-line and control characters are not accepted")
    if any(marker in command for marker in ("`", "$(", "<(", ">(")):
        return Decision(DENY, "command/process substitution hides what actually runs")
    if "/dev/tcp/" in command or "/dev/udp/" in command:
        return Decision(DENY, "bash network pseudo-devices are blocked")
    expansion = _expansion_decision(command)

    try:
        tokens = _tokens(command)
    except ValueError as exc:
        return Decision(DENY, f"cannot parse command: {exc}")
    segments = _segments(tokens)
    if not segments:
        return Decision(DENY, "no command found")

    # A pipeline that ends in a shell/interpreter is a download-and-run pattern.
    for index, segment in enumerate(segments):
        if index == 0 or not segment:
            continue
        name = _basename(segment[0])
        if name in {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "perl", "ruby", "node"}:
            return Decision(DENY, "piping data into an interpreter is not allowed")

    base = Path(cwd).expanduser() if cwd else Path.cwd()
    decision = Decision(ALLOW, "safe", "")
    for segment in segments:
        decision = decision.worse(_analyze_segment(segment, base))
    if expansion is not None:
        decision = decision.worse(expansion)

    if mode == "trusted" and decision.tier == ASK:
        return Decision(ALLOW, f"{decision.reason} (trusted mode)", decision.program)
    if authorized and decision.tier == ASK:
        return Decision(
            ALLOW,
            f"{decision.reason} (authorized by the user's request)",
            decision.program,
            True,
        )
    return decision


def _analyze_segment(segment: list[str], cwd: Path) -> Decision:
    # Leading VAR=value assignments: some of them change how the next program runs.
    index = 0
    while index < len(segment) and _ASSIGNMENT.match(segment[index]):
        name = segment[index].split("=", 1)[0]
        if name in _DANGEROUS_ASSIGNMENTS:
            return Decision(DENY, f"setting {name} can inject code into later commands")
        index += 1
    segment = segment[index:]
    if not segment:
        return Decision(ASK, "environment assignment with no command")

    # Redirections need explicit approval unless they only discard output.
    redirect = _redirect_decision(segment)
    if redirect is not None:
        return redirect

    program = segment[0]
    args = segment[1:]
    name = _basename(program)

    # A program name must be literal: `$CMD ...` could expand to anything,
    # including a denied program, after the policy check.
    if "$" in program:
        return Decision(DENY, "variable expansion is not allowed in the program name", name)

    if program.startswith(".") or "/" in program:
        resolved = Path(program if Path(program).is_absolute() else cwd / program)
        if not str(resolved.parent) in {
            "/bin", "/usr/bin", "/usr/local/bin", "/usr/sbin", "/sbin", "/snap/bin"
        }:
            return Decision(ASK, f"executes {resolved} instead of a system command", name)

    if name in {"eval", "exec", "source", "."}:
        return Decision(DENY, f"{name} executes hidden or indirect code", name)
    if name.startswith("mkfs."):
        return Decision(DENY, "filesystem creation/destruction is blocked", name)
    if name in _DENY_PROGRAMS:
        return Decision(DENY, _DENY_PROGRAMS[name], name)

    # A denied program hidden behind a wrapper is still denied.
    embedded = _embedded_deny(segment)
    if embedded is not None:
        return embedded

    # Credential material is off-limits even to read-only commands, except for
    # pure metadata listings.
    if name not in _METADATA_PROGRAMS:
        for arg in args:
            if _is_sensitive_path(arg, cwd):
                return Decision(DENY, f"refuses to access credential path {arg}", name)

    if name == "command":
        return _command_decision(args, cwd)
    if name == "printenv":
        return _printenv_decision(args)
    if name == "export" and not args:
        return Decision(DENY, "dumping the environment can expose credentials", name)
    if name in {"alias", "unalias"} and args:
        return Decision(ASK, f"{name} can hide another command behind a name", name)
    if name in {"sh", "bash", "zsh", "dash", "ksh"}:
        return _shell_decision(args, name)
    if name in {"python", "python3"}:
        return _python_decision(args)
    if name in {"perl", "ruby", "node", "php"}:
        return _interpreter_decision(name)
    if name == "git":
        return _git_decision(args)
    if name == "pip" or name == "pip3":
        return _pip_decision(args)
    if name == "find":
        return _find_decision(args)
    if name == "tar":
        return _tar_decision(args)
    if name == "sed":
        return _sed_decision(args)
    if name == "awk":
        return _awk_decision(args)
    if name == "rm":
        return _rm_decision(args)
    if name in {"chmod", "chown", "chgrp", "setfacl"}:
        return _chmod_decision(args, name)
    if name in {"kill", "pkill", "killall"}:
        return _kill_decision(args, name)
    if name == "systemctl":
        return _systemctl_decision(args)
    if name == "ip":
        return _ip_decision(args)
    if name == "dd":
        return _dd_decision(args)
    if name == "env":
        return _env_decision(args)
    if name == "gh":
        return _gh_decision(args)
    if name == "date":
        if any(a.startswith("-s") or a.startswith("--set") for a in args):
            return Decision(ASK, "date can change the system clock", name)
        return Decision(ALLOW, "read-only date", name)
    if name == "hostname":
        if args:
            return Decision(ASK, "hostname can change the system name", name)
        return Decision(ALLOW, "prints the hostname", name)
    if name in {"ifconfig", "route"}:
        return Decision(ASK, f"{name} can change network configuration", name)
    if name in {"cargo", "go", "java"}:
        return Decision(ASK, f"{name} can compile or run arbitrary code", name)
    if name in {"less", "more", "vi", "vim", "nano"}:
        return Decision(ASK, f"{name} is an interactive program with shell escapes", name)
    if name == "curl" or name == "wget":
        return Decision(ASK, "network download can write files or leak data", name)
    if name in {"ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "socat", "telnet"}:
        return Decision(ASK, "remote/network access needs approval", name)
    if name in {"xargs", "parallel", "watch", "timeout", "nice", "nohup", "setsid"}:
        return Decision(ASK, f"{name} wraps or backgrounds other commands", name)
    if name in {"cp", "mv", "ln", "touch", "mkdir", "rmdir", "truncate", "shred", "tee"}:
        return Decision(ASK, f"{name} changes files on disk (use edit_file for edits)", name)
    if name in {"black", "ruff", "yq"} and any(
        a in {"--fix", "--unsafe-fixes", "-i", "--inplace", "--in-place"}
        for a in args
    ):
        return Decision(ASK, f"{name} rewrites files", name)
    if name in {"black", "ruff", "yq"} and any(a in {"--check", "check"} for a in args):
        return Decision(ALLOW, f"read-only {name} check", name)
    if name in {"black", "ruff", "yq"}:
        return Decision(ASK, f"{name} rewrites files", name)
    if name in {"npm", "pnpm", "yarn"}:
        if any(a in {"--version", "-v"} for a in args):
            return Decision(ALLOW, f"{name} version", name)
        return Decision(ASK, f"{name} can install or run package code", name)
    if name in {"apt", "apt-get", "dnf", "yum", "pacman", "zypper", "snap", "flatpak"}:
        return Decision(ASK, f"{name} changes installed software", name)
    if name in {"docker", "podman", "kubectl", "helm", "systemd-run", "crontab", "at"}:
        return Decision(ASK, f"{name} can change system or container state", name)
    if name in {"make", "cmake", "sqlite3"}:
        return Decision(ASK, f"{name} can execute project-defined actions", name)
    if name == "pytest":
        return Decision(ASK, "pytest executes project code", name)
    if name in {"gcc", "g++", "cc", "c++", "clang", "clang++", "rustc", "javac"}:
        return Decision(ASK, f"{name} writes compiled output", name)
    if name == "sort":
        if any(
            a in {"-o", "--output"}
            or a.startswith("--output=")
            or (a.startswith("-o") and not a.startswith("--"))
            for a in args
        ):
            return Decision(ASK, "sort can write an output file", name)
        return Decision(ALLOW, "read-only sort", name)
    if name == "uniq":
        if len([a for a in args if not a.startswith("-")]) >= 2:
            return Decision(ASK, "uniq can write an output file", name)
        return Decision(ALLOW, "read-only uniq", name)
    if name == "xxd":
        if any(a in {"-r", "-revert"} or a.startswith("-r") for a in args):
            return Decision(ASK, "xxd -r writes a binary file", name)
        return Decision(ALLOW, "read-only xxd", name)
    if name == "journalctl":
        if any(
            a.startswith(("--vacuum", "--rotate", "--flush", "--sync"))
            for a in args
        ):
            return Decision(ASK, "journalctl can delete or rewrite logs", name)
        return Decision(ALLOW, "read-only journalctl", name)
    if name == "dmesg":
        if any(a in {"-c", "--clear"} for a in args):
            return Decision(ASK, "dmesg can clear the kernel log", name)
        return Decision(ALLOW, "read-only dmesg", name)
    if name == "ss":
        if any(a in {"-K", "--kill"} for a in args):
            return Decision(ASK, "ss can kill sockets", name)
        return Decision(ALLOW, "read-only ss", name)
    if name == "ps":
        if any(a == "e" or (not a.startswith("-") and "e" in a) for a in args):
            return Decision(ASK, "ps can expose process environments", name)
        return Decision(ALLOW, "read-only ps", name)
    if name in _ALLOW_PROGRAMS:
        return Decision(ALLOW, f"routine {name}", name)
    return Decision(ASK, f"{name!r} is not on the safe command list", name)


def _redirect_decision(segment: list[str]) -> Decision | None:
    for index, token in enumerate(segment):
        if token not in _REDIRECT:
            continue
        target = segment[index + 1] if index + 1 < len(segment) else ""
        if token in {"<", "<<", "<<<"}:
            return Decision(ASK, "input redirection or here-documents need approval")
        if target in {"1", "2", "0", "&1", "&2", "/dev/null"}:
            continue
        if target.startswith("/dev/fd/"):
            continue
        return Decision(ASK, "shell redirection writes outside the reply stream")
    return None


def describe_decision(decision: Decision) -> str:
    """Short, model-facing explanation."""
    if decision.tier == DENY:
        return f"blocked ({decision.reason})"
    if decision.tier == ASK:
        return f"needs approval ({decision.reason})"
    return f"allowed ({decision.reason})"
