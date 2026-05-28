#!/usr/bin/env python3
"""Tkinter UI entrypoint for the Music client.

The main qqmusic_client.py now starts the Flet UI by default. This file keeps
the original Tkinter interface available without changing the shared API,
auth, playlist, and playback code.
"""

from __future__ import annotations

import sys

from qqmusic_client import QQMusicError, build_parser, run_cli, run_gui


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command:
            return run_cli(args)
        return run_gui(args.api_base, args.timeout, args.player)
    except QQMusicError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
