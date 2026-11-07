## Summary

What changed, and why?

## Risk

- [ ] No secrets or credentials added
- [ ] No new network access in tests
- [ ] Privileged actions are audited
- [ ] Side effects require `confirmed=true`
- [ ] External content is treated as untrusted

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Paste the result, or describe the manual verification.

## Rollback

How can this be reverted if it breaks the worker?
