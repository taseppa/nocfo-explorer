"""``nocfo-explorer`` entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import ExplorerApp
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="nocfo-explorer",
        description="Yazi-inspired terminal explorer for NoCFO files and folders.",
    )
    parser.add_argument(
        "--config", metavar="PATH", help="path to config.toml (see config.example.toml)"
    )
    parser.add_argument(
        "--business",
        metavar="SLUG",
        help="business slug to explore (saved as the default)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    if args.business:
        cfg.business_slug = args.business
    ExplorerApp(cfg).run()
