#!/usr/bin/env python3
"""Prepare or verify the Go toolchain used by TSBS automation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parents[3] / "lib"))

from tsbs_environment import (  # noqa: E402
    TsbsEnvironmentError,
    resolve_go,
    verify_go,
)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--install-root", type=Path)
        child.add_argument("--json", action="store_true")
        child.add_argument("--result-file", type=Path)
    return parser


def emit(result: dict[str, Any], args: argparse.Namespace) -> None:
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.result_file:
        path = args.result_file.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")
    if args.json:
        print(serialized, end="")
    else:
        print(f"Go source: {result['source']}")
        print(f"Go version: {result['version']}")
        print(f"Go platform: {result['platform']}")
        print(f"Go binary: {result['binary']}")
        print(f"Go binary SHA-256: {result['binary_sha256']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        result = resolve_go(args.install_root) if args.command == "prepare" else verify_go(args.install_root)
        emit(result, args)
        return 0
    except (TsbsEnvironmentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
