#!/usr/bin/env python3
"""Deterministic public-surface hygiene audit for llm-router.

Scans public-facing text for:
  - machine-absolute paths (Windows C:/D:/Users, Unix /home /Users)
  - AgentTeams / internal process identifiers
  - internal planning phrasing (working plan, downstream implementation tasks, etc.)
  - high-confidence secrets
  - stale version claims that conflict with the current release line (v0.1-only
    limitation wording such as \"token counts as 0\")

Usage (repo root):
  python scripts/audit_public_release.py
  python scripts/audit_public_release.py --json

Exit codes: 0 = clean, 1 = findings.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INCLUDE_GLOBS = [
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "RELEASE_NOTES_v0.3.md",
    "RELEASE_NOTES_v0.2.md",
    "RELEASE_NOTES_v0.1.md",
    "AUDIT_PLAN.md",
    "verification-report-v0.3.md",
    ".env.example",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".github/**/*.md",
    ".github/**/*.yml",
    ".github/**/*.yaml",
    "scripts/*.md",
    "scripts/*.sh",
    "scripts/client_example.py",
    "scripts/release_checklist.sh",
    "scripts/audit_public_release.py",
]

SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("windows_abs_path", re.compile(r"[A-Za-z]:\\(?:Users|home)\\", re.I)),
    ("windows_drive_path", re.compile(r"\b[CD]:\\(?!\\)", re.I)),
    ("unix_home_path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    (
        "agent_teams",
        re.compile(
            r"AgentTeams|\.agent-teams\b|attempt_id|qa-devops-release|"
            r"protocol-platform|security-reliability|open-source-reviewer",
            re.I,
        ),
    ),
    (
        "internal_planning",
        re.compile(
            r"\b(?:internal working plan|working contract|non-binding ownership|"
            r"downstream implementation|verification tasks?|review tasks?|"
            r"captain acts only|multi-agent team)\b",
            re.I,
        ),
    ),
    ("long_sk_secret", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "assignment_secret",
        re.compile(
            r"(?i)\b(?:OPENAI|ANTHROPIC|DEEPSEEK|QWEN)_API_KEY\s*=\s*"
            r"(?!$|[\"']?\s*$)([^\s#]+)"
        ),
    ),
    (
        "stale_stream_zero_claim",
        re.compile(r"token counts as 0|record token counts as 0", re.I),
    ),
    (
        "stale_v01_limitations_header",
        re.compile(r"^##\s+Limitations\s*\(v0\.1\)\s*$", re.I),
    ),
]

ALLOW_LINE_SUBSTR = (
    "sk-demo",
    "sk-test",
    "sk-forced",
    "sk-strict",
    "change-me",
    "placeholder",
)

SELF_RULE_ALLOW = {
    "agent_teams",
    "internal_planning",
    "windows_abs_path",
    "windows_drive_path",
    "unix_home_path",
    "long_sk_secret",
    "stale_stream_zero_claim",
    "stale_v01_limitations_header",
}


def iter_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in INCLUDE_GLOBS:
        files.update(ROOT.glob(pattern))
    out: list[Path] = []
    for path in sorted(files):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        out.append(path)
    return out


def line_allowed(line: str) -> bool:
    lower = line.lower()
    return any(s.lower() in lower for s in ALLOW_LINE_SUBSTR)


def audit() -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(
                {
                    "id": f"encoding:{rel}",
                    "severity": "medium",
                    "file": rel,
                    "line": 0,
                    "rule": "encoding",
                    "snippet": "non-utf8 file",
                }
            )
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in PATTERNS:
                if not pattern.search(line):
                    continue
                if rel.endswith("audit_public_release.py") and rule in SELF_RULE_ALLOW:
                    continue
                if rule == "assignment_secret":
                    val = line.split("=", 1)[-1].strip().strip("\"'")
                    if val == "":
                        continue
                    if line.lstrip().startswith("-e") and "API_KEY=" in line:
                        continue
                if line_allowed(line) and rule in {"long_sk_secret", "assignment_secret"}:
                    continue
                findings.append(
                    {
                        "id": f"{rule}:{rel}:{lineno}",
                        "severity": (
                            "high"
                            if rule
                            in {
                                "long_sk_secret",
                                "assignment_secret",
                                "agent_teams",
                                "stale_stream_zero_claim",
                            }
                            else "medium"
                        ),
                        "file": rel,
                        "line": lineno,
                        "rule": rule,
                        "snippet": line.strip()[:200],
                    }
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON findings")
    args = parser.parse_args()
    findings = audit()
    if args.json:
        print(json.dumps({"root": str(ROOT), "findings": findings}, indent=2))
    else:
        print(f"Scanned public-facing files under {ROOT}")
        if not findings:
            print("OK: no hygiene findings")
        else:
            print(f"FINDINGS: {len(findings)}")
            for item in findings:
                print(
                    f"- [{item['severity']}] {item['file']}:{item['line']} "
                    f"({item['rule']}) {item['snippet']}"
                )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
