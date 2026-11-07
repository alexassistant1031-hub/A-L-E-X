---
name: Bug report
about: Report a defect without exposing secrets
title: "[bug] "
labels: bug
---

> **Do not paste tokens, passwords, app passwords, session strings or private
> keys.** Revoke any credential that may have leaked, then describe it as
> `<redacted>`.

## What happened

## What you expected

## Steps to reproduce

1.
2.
3.

## Environment

- OS:
- Python:
- Worker command (`main.py dev` / `start`):

## Logs

Paste only redacted log lines. Remove email addresses, tokens and message
contents that are private.

## Checks

- [ ] I removed all secrets from this report.
- [ ] I ran `.venv/bin/python -m unittest discover -s tests -t .`.
