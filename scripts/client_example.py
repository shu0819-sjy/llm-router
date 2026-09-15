#!/usr/bin/env python3
"""Minimal OpenAI-compatible client example against a local llm-router.

Usage (from repo root, with the gateway running on :8000):

    python scripts/client_example.py
    python scripts/client_example.py --base-url http://127.0.0.1:8000 --api-key sk-demo-key

Uses only the Python standard library so it works without extra client SDKs.
Replace placeholders; never commit real keys.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Call llm-router /v1/chat/completions")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="sk-demo-key", help="Gateway API key (placeholder OK)")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--prompt", default="Say hello in one short sentence.")
    args = parser.parse_args()

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {args.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, indent=2))
    try:
        content = body["choices"][0]["message"]["content"]
        print("\nAssistant:", content)
    except (KeyError, IndexError, TypeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
