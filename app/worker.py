"""Background collector. One worker means SQLite always has a single writer."""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict

from app.runtime import build_services


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def run() -> int:
    parser = argparse.ArgumentParser(description="NewsRSSHub background worker")
    parser.add_argument("--once", action="store_true", help="Run one collection/analysis pass and exit")
    parser.add_argument("--force", action="store_true", help="Fetch every enabled source, ignoring its interval")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between scheduling checks")
    args = parser.parse_args()

    services = build_services()
    configure_logging(services.settings.log_level)
    seeded = services.pipeline.bootstrap()
    if seeded:
        logging.getLogger(__name__).info("Imported %s existing RSS sources.", seeded)

    while True:
        try:
            outcome = services.pipeline.run_once(force=args.force)
            logging.getLogger(__name__).info("Pipeline pass finished: %s", json.dumps(outcome, ensure_ascii=False))
        except Exception:
            logging.getLogger(__name__).exception("Pipeline pass failed")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    raise SystemExit(run())
