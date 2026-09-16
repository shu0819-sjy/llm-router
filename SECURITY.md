# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes |
| < 0.3   | No (retired; use 0.3.x) |

Security fixes are applied to the latest release line on `main`.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately via [GitHub Security Advisories](https://github.com/shu0819-sjy/llm-router/security/advisories/new) for this repository.

Include:

- Affected version or commit
- Description of the issue and impact
- Steps to reproduce (with **placeholder** credentials only)
- Any suggested fix if you have one

We aim to acknowledge reports within 7 days and to publish a fix or mitigation timeline after triage.

## Operational guidance

- Set a strong, unique `LLM_ROUTER_ADMIN_TOKEN` in every non-development deployment. Do not use values from `.env.example` in production.
- Use `LLM_ROUTER_ENV=development` only for local work; production defaults reject insecure admin tokens.
- Treat gateway API keys and upstream provider keys as secrets. Never commit `.env` or paste real keys into issues or PRs.
- Prefer rotating keys after suspected exposure.
- Keep dependencies pinned (`requirements.txt`) and refresh pins deliberately after review.
- This release is a **single-node** gateway: rate limits and circuit breakers are process-local. Do not run multiple replicas against one SQLite file expecting shared limiter state.
