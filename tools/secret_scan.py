"""Scan repository source files for common committed-secret signatures."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg", ".conf", ".env", ".ini", ".json", ".md", ".py", ".sh",
    ".toml", ".txt", ".yaml", ".yml",
}
EXCLUDED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".venv", "__pycache__"}
PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "assigned credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password)"
            r"\s*[:=]\s*['\"](?!\s*(?:example|placeholder|changeme|test|dummy)\b)"
            r"[^'\"\s]{16,}['\"]"
        ),
    ),
)


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / value.decode() for value in result.stdout.split(b"\0") if value]


def eligible(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return (
        path.is_file()
        and path.stat().st_size <= 1_000_000
        and not EXCLUDED_PARTS.intersection(relative.parts)
        and (path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env"))
    )


def scan(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    findings: list[str] = []
    for line_number, line in enumerate(lines, 1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: possible {label}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    candidates = [path.resolve() for path in args.paths] if args.paths else repository_files()
    files = [path for path in candidates if eligible(path)]
    findings = [finding for path in files for finding in scan(path)]
    if findings:
        print("Secret scan failed:\n" + "\n".join(findings), file=sys.stderr)
        return 1
    print(f"Secret scan passed for {len(files)} repository files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
