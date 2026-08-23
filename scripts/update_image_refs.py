#!/usr/bin/env python3
"""Rewrite the top-level `images:` block of a Helm values file in place.

Phase 4.1 (G1): every image publish bakes the real content-addressed digests
into the golden production values file (deploy/prod/values.yaml) so the git
tree always carries a deployable image set. Only the six repository/digest
lines inside `images:` are touched — comments and every other section are
preserved byte-for-byte, keeping the file reviewable.

Usage:
    python scripts/update_image_refs.py --values deploy/prod/values.yaml \
        --refs deploy/prod/image-refs.json

refs JSON shape:
    {"controlPlane": {"repository": "...", "digest": "sha256:..."},
     "runtime": {...}, "frontend": {...}}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_IMAGE_KEYS = ("controlPlane", "runtime", "frontend")
# Each pattern keeps the original line terminator (\r\n on Windows,\n on POSIX)
# by capturing it in the suffix group — replacing only the value span.
_REPOSITORY_RE = re.compile(r'^([ \t]{4}repository: )[^\r\n]*([\r\n]?)$')
_DIGEST_RE = re.compile(r'^([ \t]{4}digest: ")[^"]*(")([\r\n]?)$')


def _swap(line: str, pattern: re.Pattern, replacement: str) -> str | None:
    match = pattern.match(line)
    if not match:
        return None
    prefix, *suffix = match.groups()
    return f"{prefix}{replacement}{''.join(suffix)}"


def rewrite(values_text: str, refs: dict) -> str:
    lines = values_text.splitlines(keepends=True)
    in_images = False
    current_key: str | None = None
    changed = False
    for index, line in enumerate(lines):
        if not in_images:
            if line.rstrip("\r\n") == "images:":
                in_images = True
            continue
        # End of the images block: next line with zero indent (section key).
        if re.match(r"^[A-Za-z]", line):
            break
        key_match = re.match(r"^  ([A-Za-z]+):(?:[\r\n]?)$", line)
        if key_match:
            current_key = key_match.group(1)
            if current_key not in _IMAGE_KEYS:
                current_key = None
            continue
        if current_key is None:
            continue
        ref = refs.get(current_key)
        if ref is None:
            continue
        if line.lstrip().startswith("repository:"):
            swapped = _swap(line, _REPOSITORY_RE, ref["repository"])
        elif line.lstrip().startswith("digest:"):
            swapped = _swap(line, _DIGEST_RE, ref["digest"])
        else:
            continue
        if swapped is not None and swapped != line:
            lines[index] = swapped
            changed = True
    return "".join(lines), changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--values", required=True, help="path to values YAML")
    parser.add_argument("--refs", required=True, help="path to refs JSON")
    args = parser.parse_args()

    values_path = Path(args.values)
    refs = json.loads(Path(args.refs).read_text(encoding="utf-8"))
    missing = [key for key in _IMAGE_KEYS if key not in refs]
    if missing:
        print(f"refs JSON missing image keys: {missing}", file=sys.stderr)
        return 2

    rewritten, changed = rewrite(values_path.read_text(encoding="utf-8"), refs)
    if not changed:
        print("image refs unchanged (already current)")
        return 0
    values_path.write_text(rewritten, encoding="utf-8")
    print(f"updated image refs in {values_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())