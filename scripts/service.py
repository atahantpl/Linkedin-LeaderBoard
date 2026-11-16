#!/usr/bin/env python3
"""
Local service: play today's LinkedIn Games, collect the connection leaderboards
and post them to Slack (or print them when no webhook is configured).

Usage:
    python service.py                 # play + collect + Slack
    python service.py --no-play       # collect + Slack, games already played by hand
    python service.py --dry-run       # play + collect, print message, no Slack
    python service.py --slack-only    # post existing leaderboard.json only
    python service.py --message-only  # print formatted message only

Setup:
    1. Ensure setup_auth.py has been run once
    2. track_connections: true in config.json
    3. Optional: copy .env.example → .env and set SLACK_WEBHOOK_URL

Schedule (optional, macOS launchd example):
    # Run daily at 09:00 local time
    # 0 9 * * * cd /path/to/scripts && .venv/bin/python service.py
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from config import SCRIPTS_DIR
from dotenv import load_dotenv
from leaderboard_format import format_day_message, linkedin_today, read_leaderboard_data
from sheet_layout import load_layout
from slack_notify import SlackNotifyError, post_message, slack_webhook_url

load_dotenv(SCRIPTS_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_player(*, duration: float | None = None) -> int:
    cmd = [sys.executable, str(SCRIPTS_DIR / "play.py"), "--game", "all"]
    if duration is not None:
        cmd += ["--duration", str(duration)]
    logger.info("Playing today's games …")
    result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR))
    return result.returncode


def run_collector(*, update: bool = True) -> int:
    cmd = [sys.executable, str(SCRIPTS_DIR / "collector.py")]
    if update:
        cmd.append("--update")
    logger.info("Running collector …")
    result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR))
    return result.returncode


def build_message(layout, day_key: str | None = None) -> str:
    day_key = day_key or linkedin_today().isoformat()
    lb_path = layout.output_leaderboard or (SCRIPTS_DIR / "leaderboard.json")
    data = read_leaderboard_data(lb_path)
    if not data:
        raise RuntimeError(
            f"Leaderboard store not found or empty: {lb_path}\n"
            "Run with collector first (track_connections must be enabled)."
        )
    return format_day_message(data, day_key, layout, for_slack=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect LinkedIn Games leaderboards and post to Slack"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and print the Slack message without posting",
    )
    parser.add_argument(
        "--slack-only",
        action="store_true",
        help="Skip collection; post the current leaderboard.json",
    )
    parser.add_argument(
        "--message-only",
        action="store_true",
        help="Print the formatted message and exit (no collect, no Slack)",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Leaderboard date to format (default: today Pacific)",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Run collector without --update (smart mode)",
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        help="Skip playing the games before collecting",
    )
    parser.add_argument(
        "--play-duration",
        type=float,
        metavar="SECONDS",
        default=None,
        help="Target seconds per game for the player (default: play.py default)",
    )
    args = parser.parse_args()

    try:
        layout = load_layout()
    except Exception as exc:
        logger.error("Could not read config.json: %s", exc)
        return 1

    if not layout.track_connections:
        logger.error("track_connections is disabled in config.json — enable it for Slack leaderboards.")
        return 1

    if not args.slack_only and not args.message_only:
        if not args.no_play:
            code = run_player(duration=args.play_duration)
            if code != 0:
                logger.warning("Some games were not completed (exit %d) — their leaderboards may be empty.", code)
        code = run_collector(update=not args.no_update)
        if code != 0:
            logger.error("Collector failed (exit %d) — Slack message not sent.", code)
            return code

    try:
        message = build_message(layout, day_key=args.date)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    if args.message_only or args.dry_run:
        print(message)
        if args.dry_run:
            logger.info("Dry run — message not sent to Slack.")
        return 0

    if not slack_webhook_url():
        print(message)
        logger.info("SLACK_WEBHOOK_URL is not set — message printed instead of posted.")
        return 0

    try:
        post_message(message)
    except SlackNotifyError as exc:
        logger.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
