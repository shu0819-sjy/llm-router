## Summary

<!-- What does this PR change and why? -->

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] Docs / CI / tooling
- [ ] Breaking change (describe migration)

## Checklist

- [ ] Tests added or updated (`pytest`; coverage gate still passes)
- [ ] Lint clean (`ruff check app tests scripts`)
- [ ] Docs updated when behavior or config changes
- [ ] No secrets, real API keys, or machine-absolute paths in the diff
- [ ] Integration / live-provider tests remain opt-in (`LLM_ROUTER_RUN_INTEGRATION=1`)

## Test plan

<!-- Commands you ran locally -->

```bash
pip install -r requirements-dev.txt
python -m pytest -q --cov=app --cov-report=term-missing
ruff check app tests scripts
```
