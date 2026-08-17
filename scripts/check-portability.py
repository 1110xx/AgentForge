#!/usr/bin/env python3
"""Fail closed on host imports, escaping paths and high-confidence secrets."""
from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRECTORY_NAMES = frozenset({
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
TYPESCRIPT_SUFFIXES = frozenset({".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"})
TEXT_SUFFIXES = frozenset({
    ".cfg",
    ".conf",
    ".dockerfile",
    ".env",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
})
FORBIDDEN_MODULE_ROOTS = frozenset({"frontend_v2", "lib", "src", "triage"})

STATIC_IMPORT = re.compile(
    r"(?:^|\n)\s*(?:import|export)\s+(?:type\s+)?"
    r"(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
DYNAMIC_IMPORT = re.compile(r"\b(?:import|require)\s*\(\s*['\"]([^'\"]+)['\"]")
HOST_PATH = re.compile(
    r"(?:^|[^\w])(?:/Users/|/home/|" + r"C:\\Users\\|C:\\Windows\\)",
    re.IGNORECASE,
)
INTERNAL_URL = re.compile(
    r"https?://[^\s/]*(?:gitlab-master\.nvidia\.com|corp\.|internal\.)",
    re.IGNORECASE,
)
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN(?:[A-Z0-9]+)?PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("PAYMENT_SECRET", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
)


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    path: str
    line: int
    code: str
    message: str


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _module_is_forbidden(module: str) -> bool:
    normalized = module.replace("/", "/").lstrip("@").lower()
    root = normalized.split("/", 1)[0]
    return root in FORBIDDEN_MODULE_ROOTS


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _python_findings(path: Path, root: Path, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as error:
        return [
            Finding(
                str(path.relative_to(root)),
                error.lineno or 1,
                "PYTHON_PARSE_ERROR",
                error.msg,
            )
        ]
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_is_forbidden(alias.name):
                    findings.append(
                        Finding(
                            str(path.relative_to(root)),
                            node.lineno,
                            "FORBIDDEN_PYTHON_IMPORT",
                            alias.name,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if node.level == 0 and module is not None and _module_is_forbidden(module):
                findings.append(
                    Finding(
                        str(path.relative_to(root)),
                        node.lineno,
                        "FORBIDDEN_PYTHON_IMPORT",
                        module,
                    )
                )
            if node.level > 0:
                target = path.parent
                for _ in range(node.level):
                    target = target.parent
                if not _is_within(target, root):
                    findings.append(
                        Finding(
                            str(path.relative_to(root)),
                            node.lineno,
                            "ESCAPING_PYTHON_IMPORT",
                            f"relative import level {node.level}",
                        )
                    )
    return findings


def _typescript_findings(path: Path, root: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in (STATIC_IMPORT, DYNAMIC_IMPORT):
        for match in pattern.finditer(text):
            specifier = match.group(1)
            if specifier.startswith("."):
                if not _is_within(path.parent / specifier, root):
                    findings.append(
                        Finding(
                            str(path.relative_to(root)),
                            _line_number(text, match.start(1)),
                            "ESCAPING_TYPESCRIPT_IMPORT",
                            specifier,
                        )
                    )
            elif _module_is_forbidden(specifier):
                findings.append(
                    Finding(
                        str(path.relative_to(root)),
                        _line_number(text, match.start(1)),
                        "FORBIDDEN_TYPESCRIPT_IMPORT",
                        specifier,
                    )
                )
    return findings


def _text_findings(path: Path, root: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for code, pattern in (
        ("HOST_ABSOLUTE_PATH", HOST_PATH),
        ("INTERNAL_URL", INTERNAL_URL),
        *SECRET_PATTERNS,
    ):
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    str(path.relative_to(root)),
                    _line_number(text, match.start()),
                    code,
                    "high-confidence forbidden host or secret material",
                )
            )
    return findings


def _iter_authored_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRECTORY_NAMES for part in relative_parts):
            continue
        if path.name == "check-portability.py":
            continue  # self-bootstrapping check: never scan its own patterns
        if path.is_symlink():
            files.append(path)
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def check(root: Path) -> tuple[list[Finding], int]:
    resolved_root = root.resolve()
    findings: list[Finding] = []
    files = _iter_authored_files(resolved_root)
    for path in files:
        if path.is_symlink():
            if not _is_within(path, resolved_root):
                findings.append(
                    Finding(
                        str(path.relative_to(resolved_root)),
                        1,
                        "ESCAPING_SYMLINK",
                        str(path.readlink()),
                    )
                )
            continue
        if path.name.lower() == "dockerfile" or path.suffix.lower() in TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        else:
            continue
        suffix = path.suffix.lower()
        if suffix in PYTHON_SUFFIXES:
            findings.extend(_python_findings(path, resolved_root, text))
        if suffix in TYPESCRIPT_SUFFIXES:
            findings.extend(_typescript_findings(path, resolved_root, text))
        findings.extend(_text_findings(path, resolved_root, text))
    return sorted(set(findings)), len(files)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that enterprise-agent-platform has no host import or secret dependency"
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"root is not a directory: {args.root}")
    findings, scanned = check(args.root)
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}:{finding.code}:{finding.message}",
            flush=True,
        )
    if findings:
        print(f"portability check failed: {len(findings)} finding(s)", flush=True)
        return 1
    print(f"portability check passed: {scanned} authored file(s) scanned", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
