# Alex self-modification controller — Phase 0 + Phase 1

Safety rails for autonomous self-modification **without Git**. The assistant
can read anywhere, but it can write only to `staging/` and the user's working
directories. Every project mutation takes a fresh, verified snapshot first,
and every attempt is recorded in a controller-owned, hash-chained audit log.

## Layout

```
controller/
  config.py        paths, allow-list, exclusions, size limits, fallbacks
  storage.py       0700 directories, 0600 files, atomic writes
  audit.py         hash-chained JSONL logs + verification
  guard.py         write-path policy (deny wins)
  backup.py        full project + single-path snapshots, manifests, restore
  modification.py  backup-first mutation pipeline
  status.py        side-effect-free readiness/policy report
  cli.py           status / init / backup / list / verify / prune / restore
  setup.sh         one-time privileged directory setup
staging/           the only writable place inside the project
tests/             stdlib unittest suite (temp dirs only)
```

Private state lives **outside** the project:

```
/var/backups/alex/            full project snapshots
/var/backups/alex/paths/      snapshots of individual working-directory files
/var/log/alex-controller/     audit logs (file.jsonl, typing.jsonl, ...)
```

If `/var` is unavailable, the controller falls back to the private XDG state
directory (`~/.local/state/alex/...`) instead of writing inside the project.
That fallback is still outside the project and still 0700. Set
`ALEX_STORAGE_FALLBACK=0` to fail closed instead.

## One-time setup

```bash
sudo bash controller/setup.sh
.venv/bin/python -m controller status
```

`setup.sh` creates `/var/backups/alex`, `/var/log/alex-controller` and
`staging/` with mode 0700, owned by the account that runs Alex. `status`
reports readiness without creating anything.

## What a mutation does

For every `write`, `append`, `mkdir`, `delete`, `copy` or `move`:

1. **Policy check** — `WritePathGuard` resolves symlinks and decides whether
   the target is writable. Denials happen before any backup work.
2. **Safety snapshot** — project paths get a fresh full `tar.gz` snapshot;
   external working-directory paths get a snapshot of that file or directory.
   The archive is written to a temporary name, hashed, verified, and only then
   published. If the snapshot fails, the mutation does not happen.
3. **Audit intent** — `file_change_requested` and `safety_snapshot` are
   appended to the controller audit log. If the log is unavailable, the
   mutation is refused.
4. **Atomic change** — writes use a temporary file plus `os.replace`; deletes
   move the item to `~/.alex_trash`.
5. **Audit outcome** — success or failure, with the snapshot name.

SQLite databases are captured through the SQLite backup API, so a snapshot is
consistent even while Alex is running. A `.db` file that is not actually
SQLite is copied byte-for-byte.

## Policy

| Action | Agent (`file_operation`) | Operator |
|---|---|---|
| Read project files | yes | yes |
| Write `staging/**` | yes | yes |
| Write `~/Documents`, `~/Desktop`, ... | yes | yes |
| Write core project files | **no** | manual, outside this tool |
| Write `tools/`, `memory/`, `controller/` | **no** | manual, outside this tool |
| Write backups / audit logs / trash | **no** | no |
| Write `.env`, keys, `.ssh` | **no** | no |
| Delete an allowed root | **no** | no |

Symlinks are resolved before the decision, so a link in `staging/` cannot be
used to reach core code or `/etc`. Writes use `O_NOFOLLOW` and re-check the
opened descriptor through `/proc/self/fd`. Credential material is never
writable, even inside an allowed working directory.

`file_operation` never promotes a staged change into live code. Promotion is
intentionally outside the model-facing surface and is left to the operator.

## Shell and exact-string edits

Two additional model-facing tools sit on the same policy layer:

* `edit_file` replaces an exact string in a writable text file. An ambiguous
  match is refused unless `replace_all=true`, hard-linked files are refused,
  and every edit takes a fresh snapshot and writes a `file` audit record.
* `shell` runs one-line commands as the desktop user with a persistent working
  directory. It is **not** an OS sandbox and never provides root access.

Shell commands are classified by `tools/shell/permissions.py`:

| Tier | Behaviour |
|---|---|
| `allow` | routine/read-only command runs immediately |
| `ask` | sensitive or unknown command: runs immediately when the user asked for it (`authorized=true`); otherwise refused until the user approves |
| `deny` | privileged, destructive or credential-exposing command never runs, even if authorized |

A direct instruction from the user is full authorization: the model sets
`authorized=true` on the call and the `ask` tier runs without a second
confirmation prompt. For work the model initiates on its own, the `ask` refusal
returns a short-lived, single-use token bound to the exact command and
directory; the model explains the command, waits for the user's clear yes, and
retries with that token. Privilege escalation, filesystem/disk destruction,
credential paths, command substitution, heredocs, and download-piped-to-shell
are denied outright and cannot be authorized.

Operator controls:

| Variable | Default |
|---|---|
| `ALEX_SHELL_MODE` | `guarded` (`trusted` allows unknown commands; deny still wins) |
| `ALEX_SHELL_APPROVAL_TTL` | `120` seconds |
| `ALEX_SHELL_TIMEOUT` | `60` seconds (per call, max 300) |
| `ALEX_SHELL_MAX_OUTPUT` | `20000` characters |

Shell output is capped and ANSI-stripped; the whole process group is killed on
timeout. Commands are audited in `shell.jsonl` before execution, with secret
assignments redacted and output never recorded. If the audit log is
unavailable, the command is not run.

Because `ask` is a conversational gate rather than an OS permission prompt, a
model that ignores its instructions could approve its own request. Treat the
agent account as semi-trusted and keep backups/audit storage outside it.

## Audit logs

Each stream is append-only JSON Lines with a SHA-256 hash chain:

```bash
.venv/bin/python -m controller audit-verify --stream file
.venv/bin/python -m controller audit-tail --stream file --limit 20
```

The chain is tamper-**evident**, not tamper-proof. The 0700 directory and
controller ownership are the primary protection.

## Backups

```bash
.venv/bin/python -m controller backup --reason "before experiment"
.venv/bin/python -m controller list
.venv/bin/python -m controller verify              # verify every snapshot
.venv/bin/python -m controller prune --keep 10
.venv/bin/python -m controller restore <name> /tmp/alex-restored --confirm RESTORE
.venv/bin/python -m controller restore-path <name> /tmp/doc-restored --confirm RESTORE
```

Restore is conservative: the destination must be empty or absent unless
`--overwrite` is given. Stop Alex before restoring a live project.

## Environment overrides

| Variable | Default |
|---|---|
| `ALEX_PROJECT_ROOT` | directory containing `controller/` |
| `ALEX_STAGING_ROOT` | `<project>/staging` |
| `ALEX_BACKUP_ROOT` | `/var/backups/alex` |
| `ALEX_AUDIT_ROOT` | `/var/log/alex-controller` |
| `ALEX_WRITE_ROOTS` | `~/Documents:~/Desktop:~/Downloads:~/Projects:~/Notes:~/Work` |
| `ALEX_TRASH_ROOT` | `~/.alex_trash` |
| `ALEX_BACKUP_RETENTION` | `20` |
| `ALEX_BACKUP_EXCLUDES` | `.venv`, `.git`, caches, `*.db-wal`, `*.db-shm`, ... |
| `ALEX_BACKUP_COALESCE_SECONDS` | `30` (agent tool forces fresh snapshots) |
| `ALEX_MAX_WRITE_BYTES` | `10485760` |
| `ALEX_MAX_PATH_SNAPSHOT_BYTES` | `536870912` |
| `ALEX_STORAGE_FALLBACK` | `1` (set `0` to fail closed if `/var` is unusable) |

`ALEX_BACKUP_EXCLUDES` replaces the default list; an empty value includes
everything. Snapshots always capture live SQLite files through the SQLite
backup API, so `-wal`/`-shm` files are not archived raw.

## Tests

The suite uses only temporary directories and never touches the real
`/var/backups/alex` or `/var/log/alex-controller`:

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## Scope and limitations

* **Implemented:** private storage, full/path snapshots, write-path guard,
  secure audit logs, backup-first writes, CLI, tests.
* **Not implemented:** autonomous proposal generation, automated evaluation of
  staged changes, automatic rollback, and unattended promotion.
* The path guard is a policy layer, not an OS sandbox. A process that can run
  arbitrary code can bypass it; run Alex as a dedicated account for stronger
  isolation.
* No Git repository, worktree, subprocess, or network call is used anywhere in
  the controller.
