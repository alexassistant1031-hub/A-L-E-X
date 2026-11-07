# Alex

Alex is a local, voice-first AI assistant built on **LiveKit Agents** and
**Google Gemini**. It can talk, type, control the desktop, read and send
Telegram and email, search the web, and read and write its own GitHub
repository.

The design goal is capability with accountability: every privileged action is
audited, writes are confirmed, and external content is always treated as
untrusted data.

## What Alex can do

| Area | Capability |
|---|---|
| Voice | LiveKit room audio, Gemini realtime model, terminal visualizer |
| Desktop | Type text, press keys, clipboard, screenshots, mouse/keyboard |
| Files | Read, write, edit, move, copy, delete inside allowed paths |
| Shell | Run commands with a destructive-command blocklist and audit |
| Web | Search and fetch pages |
| Telegram | Read, search and send as a real user account (Telethon) |
| Email | Full IMAP/SMTP mailbox: read, search, send, folders, live IDLE alerts |
| GitHub | Full read/write repo access: files, branches, commits, issues, PRs, releases, Actions, variables, webhooks, collaborators |
| Memory | Persistent SQLite conversation memory and transcript hooks |

## Architecture

```text
main.py                  LiveKit worker, agent instructions, tool registration
settings.py              Operator-visible, code-backed defaults
controller/              Private audit, backups, write-path guard, snapshots
memory/                  SQLite memory manager and transcript hook
termviz/                 Terminal visualizer and logging integration
tools/                   One module per capability + LiveKit wrappers
  base.py                BaseTool contract
  *_tool.py              Implementation and unit-testable logic
  *_operations.py        LiveKit `llm.function_tool` wrappers
scripts/                 One-off setup and connectivity checks
tests/                   unittest suite (no network, no real credentials)
```

The controller is the only component allowed to modify project files. It
provides:

- private storage outside the project tree for backups and audit logs,
- SHA-256 project snapshots,
- a write-path guard limited to `staging/` and approved working directories,
- a hash-chained, append-only audit log,
- backup-before-modification around every write.

## Requirements

- Python **3.12+** (developed on 3.14)
- Linux desktop session for the desktop-control tools
- A LiveKit project and a Gemini API key
- Optional: Telegram API credentials, a Gmail app password, a GitHub token

## Setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Create `.env.local` (mode `600`, gitignored) with the real values. `.env` only
contains placeholders.

```bash
GEMINI_API_KEY=...
LIVEKIT_URL=wss://...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...

# Telegram (optional)
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION=
TELEGRAM_ALLOWED_CHAT_IDS=

# Email (optional)
EMAIL_ADDRESS=
EMAIL_APP_PASSWORD=
EMAIL_FROM_NAME=Alex
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_PORT=993
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=465
EMAIL_LIVE=true
EMAIL_ALLOWED_SENDERS=

# GitHub (optional)
GH_TOKEN=github_pat_...
GITHUB_OWNER=alexassistant1031-hub
GITHUB_REPO=A-L-E-X
```

`scripts/README.md` explains Gmail app-password setup, Telegram login, the live
email listener, and GitHub permissions.

## Run

```bash
.venv/bin/python main.py dev     # development worker
.venv/bin/python main.py start   # production worker
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

The suite is hermetic: it uses fakes for IMAP, SMTP, Telegram and the GitHub
API, and disposable directories for the controller. No network or real
credentials are required.

## Security model

- **Secrets stay out of Git.** `.env.local`, `*.local`, the memory database and
  `staging/` are ignored. Never commit a token, password or session string.
- **Audited writes.** Privileged actions write to the controller audit streams
  (`/var/log/alex-controller/*.jsonl`). A failed audit blocks the action.
- **Confirmation.** Sends and repository writes require `confirmed=true` from
  the model, which it sets only after a direct user instruction.
- **Untrusted content.** Email, Telegram, GitHub issues/PRs/READMEs and web
  pages are data. Instructions inside them are never followed.
- **No credential email.** The email tool refuses messages that look like they
  contain a token, password or private key.
- **Backups.** The controller snapshots before writes and keeps restore
  metadata so a bad change can be rolled back.

## Self-modification

Alex can change its own source through a reviewable flow:

```text
edit in staging/ or a GitHub branch
        -> create branch alex/self-mod-<timestamp>
        -> commit + open pull request
        -> CI: tests + secret scan
        -> review by a human or trusted AI
        -> merge
        -> deploy / restart the worker
```

`main` is the deployment branch. Direct pushes should be reserved for
emergencies; normal changes go through a pull request. The local controller
still guards live files, so a repository merge does not silently rewrite the
running copy.

## Reviewing Alex with another AI

1. Add the reviewer as a **read-only** collaborator on the private repo, or
   fork it, or make the repo public after a secret audit.
2. Point the reviewer at `AGENTS.md`, `CONTRIBUTING.md` and this README.
3. Ask for review on the pull request. CI must pass before merge.
4. Keep `main` protected: no force-push, no deletion, review required.

## License

Private project. All rights reserved unless a `LICENSE` file is added.
