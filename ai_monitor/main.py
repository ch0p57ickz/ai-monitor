"""Command-line entry point for the ai-monitor usage service."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from ai_monitor import __version__
from ai_monitor.config import Config, ConfigError, load_config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.sources.registry import default_registry
from ai_monitor.state import UsageWorker


LOG = logging.getLogger("ai_monitor")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-monitor",
        description="Loopback usage service for Codex, Antigravity, and OpenRouter.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to a JSON config file. Defaults to embedded defaults.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Override the database path from config.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOG.error("config error: %s", exc)
        return 2

    db_path = args.db or str(Path(config.data_dir) / "usage.db")
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    registry = default_registry()
    worker = UsageWorker(config=config, db=db, registry=registry, version=__version__)
    worker.start()
    server = serve_http(config, worker, version=__version__)

    # Run the HTTP server on a daemon thread so the main thread can wait for signals.
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    LOG.info("serving on %s", server.url)

    stop_requested = threading.Event()

    def _handle_stop(signum: int, _frame: object) -> None:
        LOG.info("received signal %s, shutting down", signum)
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while not stop_requested.is_set():
            stop_requested.wait(timeout=1.0)
    finally:
        server.shutdown()
        worker.stop()
        db.close()
        LOG.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
