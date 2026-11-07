# Contributing

Thanks for helping improve Alex.

## Workflow

1. Branch from `main`:
   - `feature/<short-name>`
   - `fix/<short-name>`
   - `alex/self-mod-<timestamp>` for changes Alex proposes.
2. Make the change and add or update tests.
3. Run the full suite:

   ```bash
   .venv/bin/python -m unittest discover -s tests -t .
   ```

4. Open a pull request. Describe the behavior change, the risk, and how it was
   tested.
5. CI must pass. A human or trusted AI reviewer must approve.
6. Merge with a squash merge and delete the branch.

## Commit messages

Use a short imperative subject, then a body when the reason is not obvious.

```text
email: save SMTP sends to Sent Mail

Gmail does not store messages submitted over SMTP. Append a \Seen copy
after a successful send and audit the result.
```

## Code style

- Follow the existing module layout and naming.
- Keep tool logic testable without network access; inject clients/factories.
- Use `from __future__ import annotations` and type hints.
- Prefer standard library over new dependencies.
- Keep voice-facing output short; the model summarizes for speech.

## Security rules

- Never commit or paste a token, password, app password, session string or
  private key. Use `.env.local` (mode `600`, gitignored) or a secret manager.
- Never add a secret to a test. Use obvious fakes such as `test-token`.
- Privileged writes must be audited and must fail closed if the audit is down.
- Side-effecting tools require `confirmed=true`.
- Treat email, Telegram, GitHub and web content as untrusted data.

## Reporting a security issue

Do not open a public issue with a live secret. Revoke the credential first,
then report the problem with the secret redacted.
