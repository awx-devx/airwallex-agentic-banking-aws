"""Fail if a tracked or staged file looks like it carries a credential.

Prints only file:line and the rule name, never the matched text.
Usage: python scripts/secret_scan.py [--staged]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RULES = {
    "airwallex-api-key-assignment": re.compile(r"AWX_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    "api-key-header-literal": re.compile(r"x-api-key['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    "bearer-token-literal": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{40,}"),
    "aws-access-key-id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws-secret-assignment": re.compile(r"aws_secret_access_key\s*[=:]\s*\S{30,}", re.IGNORECASE),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt-literal": re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    ),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".lock", ".drawio"}


def files(staged: bool) -> list[Path]:
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # noqa: S603
    return [Path(p) for p in out.splitlines() if p and Path(p).suffix not in SKIP_SUFFIXES]


def main() -> int:
    hits = 0
    for path in files("--staged" in sys.argv):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, rule in RULES.items():
                if rule.search(line):
                    print(f"{path}:{lineno}: possible secret ({name})")
                    hits += 1
    if hits:
        print(f"secret scan: {hits} finding(s); remove them before committing")
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
