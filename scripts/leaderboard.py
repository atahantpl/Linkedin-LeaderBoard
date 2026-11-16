"""
Print and export LinkedIn Games connection leaderboards.

Usage:
    python leaderboard.py                         # today's tables
    python leaderboard.py --date 2026-07-14       # specific day
    python leaderboard.py --game zip              # one game only
    python leaderboard.py --export-csv out.csv    # CSV export
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from leaderboard_format import (
    format_day_message,
    format_game_lines,
    linkedin_today,
    read_leaderboard_data,
)
from sheet_layout import load_layout

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def print_game_leaderboard(
    game_key: str,
    game_data: dict,
    *,
    show_source: bool = False,
    my_name: str | None = None,
) -> None:
    lines = format_game_lines(game_key, game_data, my_name=my_name, for_slack=False)
    if not lines:
        return
    if show_source:
        source = game_data.get("source")
        if source and lines:
            lines[1] = f"{lines[1]} [{source}]"
    for line in lines:
        print(line)


def print_day(
    data: dict,
    day_key: str,
    *,
    game_filter: str | None = None,
    show_source: bool = False,
    my_name: str | None = None,
) -> None:
    rec = data.get(day_key)
    if not rec:
        logger.error("No leaderboard data for %s", day_key)
        return

    games = rec.get("games") or {}
    if game_filter:
        if game_filter not in games:
            logger.error("No leaderboard data for game %r on %s", game_filter, day_key)
            return
        print_game_leaderboard(game_filter, games[game_filter], show_source=show_source, my_name=my_name)
        return

    try:
        layout = load_layout()
        print(format_day_message(data, day_key, layout, for_slack=False))
    except Exception:
        for game_key in games:
            print_game_leaderboard(game_key, games[game_key], show_source=show_source, my_name=my_name)


def export_leaderboard_csv(data: dict, csv_path: Path) -> int:
    rows: list[list[str]] = []
    for day_key in sorted(data):
        rec = data[day_key] or {}
        games = rec.get("games") or {}
        for game_key, game_data in games.items():
            for entry in game_data.get("entries") or []:
                rows.append([
                    day_key,
                    rec.get("day_of_week", ""),
                    game_key,
                    str(entry.get("rank", "")),
                    entry.get("name", ""),
                    "yes" if entry.get("is_me") else "no",
                    entry.get("score", ""),
                    game_data.get("source", ""),
                ])

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Day of Week", "Game", "Rank", "Player", "Is Me", "Score", "Source",
        ])
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    try:
        layout = load_layout()
        lb_path = layout.output_leaderboard or (Path.cwd() / "leaderboard.json")
        my_name = layout.my_name
    except Exception as exc:
        logger.warning("Could not read config (%s); using ./leaderboard.json", exc)
        lb_path = Path.cwd() / "leaderboard.json"
        my_name = None

    parser = argparse.ArgumentParser(description="Display LinkedIn Games connection leaderboards")
    parser.add_argument("--input", type=Path, default=None, help="Leaderboard JSON path")
    parser.add_argument("--date", metavar="YYYY-MM-DD", default=None, help="Day to display")
    parser.add_argument("--game", metavar="KEY", default=None, help="Single game key (e.g. zip)")
    parser.add_argument("--export-csv", metavar="FILE", default=None, help="Export all rows to CSV")
    parser.add_argument("--show-source", action="store_true", help="Show api/html/merged source tag")
    args = parser.parse_args()

    lb_path = (args.input or lb_path).expanduser()
    data = read_leaderboard_data(lb_path)
    if not data:
        logger.error("Leaderboard store not found or empty: %s", lb_path)
        logger.error("Run collector.py with track_connections enabled after LinkedIn login.")
        return 1

    if args.export_csv:
        n = export_leaderboard_csv(data, Path(args.export_csv).expanduser())
        logger.info("Exported %d row(s) to %s", n, args.export_csv)
        return 0

    day_key = args.date or linkedin_today().isoformat()
    if args.game:
        print_day(data, day_key, game_filter=args.game, show_source=args.show_source, my_name=my_name)
    else:
        try:
            layout = load_layout()
            print(format_day_message(data, day_key, layout, for_slack=False))
        except Exception as exc:
            logger.warning("Falling back to simple print (%s)", exc)
            print_day(data, day_key, show_source=args.show_source, my_name=my_name)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
