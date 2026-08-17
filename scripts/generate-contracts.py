#!/usr/bin/env python3
"""Regenerate deterministic JSON Schema and OpenAPI public contracts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from enterprise_agent_platform.contracts.schema_export import export_contracts
from enterprise_agent_platform.reference.local_stack import create_app


def generate(output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    export_contracts(output_root / "schemas")
    canonical_openapi = (
        json.dumps(
            create_app().openapi(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    (output_root / "openapi.json").write_text(canonical_openapi, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    generate(args.output_root)
    print(f"generated public contracts in {args.output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
