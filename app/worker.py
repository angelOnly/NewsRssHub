"""后台任务入口：抓取调度与大模型处理可独立运行。"""

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
    parser = argparse.ArgumentParser(description="NewsRSSHub 后台任务")
    parser.add_argument("--once", action="store_true", help="运行一轮后退出")
    parser.add_argument("--force", action="store_true", help="忽略排期并抓取全部启用来源")
    parser.add_argument(
        "--role",
        choices=("collector", "processor", "topics", "all"),
        default="all",
        help="collector 仅抓取；processor 仅处理内容；topics 仅归并今日热点",
    )
    parser.add_argument("--interval", type=int, default=30, help="两轮检查之间的秒数")
    args = parser.parse_args()

    services = build_services()
    configure_logging(services.settings.log_level)
    if args.role in {"collector", "all"}:
        seeded = services.pipeline.bootstrap()
        if seeded:
            logging.getLogger(__name__).info("Imported %s existing RSS sources.", seeded)

    while True:
        try:
            if args.role == "collector":
                outcome = services.pipeline.collect_once(force=args.force)
            elif args.role == "processor":
                outcome = services.pipeline.process_once()
            elif args.role == "topics":
                outcome = services.pipeline.refresh_daily_topics_once()
            else:
                outcome = services.pipeline.run_once(force=args.force)
            logging.getLogger(__name__).info(
                "%s pass finished: %s", args.role, json.dumps(outcome, ensure_ascii=False)
            )
        except Exception:
            logging.getLogger(__name__).exception("Pipeline pass failed")
            if args.once:
                return 1
        if args.once:
            return 0
        if args.role == "topics":
            # 热点任务使用网页中的持久化设置；启动后先执行一轮，再按该间隔等待。
            interval_seconds = services.repository.get_daily_topic_refresh_interval_minutes() * 60
        else:
            interval_seconds = max(15, args.interval)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    raise SystemExit(run())
