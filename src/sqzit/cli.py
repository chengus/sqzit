from __future__ import annotations

import argparse

from .app import run_app
from .profiles import PROFILES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compress images and videos safely in a terminal UI.")
    parser.add_argument("directory", nargs="?", default=".", help="directory to scan (default: current directory)")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="balanced")
    parser.add_argument("--mode", choices=("replace", "copy"), default="replace")
    parser.add_argument("--no-recursive", action="store_true", help="scan only the selected directory")
    parser.add_argument("--jobs", type=int, default=None, help="number of parallel FFmpeg jobs, 1-8")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run_app(
        directory=args.directory,
        profile=args.profile,
        recursive=not args.no_recursive,
        mode=args.mode,
        jobs=args.jobs,
    )
