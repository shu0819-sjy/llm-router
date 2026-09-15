# GitHub publish — v0.1.0 recovery

Local git is initialized on `D:\deepseek workspace\llm-router`:

- Branch: `main`
- Commit: `Release v0.1.0: OpenAI-compatible LLM gateway`
- Annotated tag: `v0.1.0`
- **No secrets** in the tree (`.env` gitignored; only `.env.example`)

## Why push did not complete

`gh` is **not authenticated** (token missing/invalid for `shu0819-sjy`):

```text
gh auth status
# → You are not logged into any GitHub hosts.
# or: The token in default is invalid.
```

`gh repo create … --push` exited without creating `origin`.

## Exact recovery (run locally)

```powershell
# 1) Re-authenticate (interactive — browser or paste a PAT with repo scope)
gh auth login -h github.com

# 2) Confirm
gh auth status
# Expect: Logged in to github.com account shu0819-sjy

# 3) From the project root
cd "D:\deepseek workspace\llm-router"

# 4) Create remote + push main
gh repo create shu0819-sjy/llm-router --public --source=. --remote=origin --push

# If the empty repo already exists:
#   git remote add origin https://github.com/shu0819-sjy/llm-router.git
#   git push -u origin main

# 5) Push tag + GitHub Release
git push origin v0.1.0
gh release create v0.1.0 -F RELEASE_NOTES_v0.1.md --title "llm-router v0.1.0"
```

Alternative without `gh` (after creating the empty repo in the UI):

```powershell
git remote add origin https://github.com/shu0819-sjy/llm-router.git
git push -u origin main
git push origin v0.1.0
```

Never store PATs in the repository or in committed `.env` files.
