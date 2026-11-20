"""
Format LinkedIn Games leaderboard data for terminal or Slack.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import GAMES, SCRIPTS_DIR
from leaderboard_parser import resolve_player_name, score_to_sortable
from sheet_layout import Layout, load_layout

_LINKEDIN_TZ = ZoneInfo("America/Los_Angeles")
TOP_RANK = 3
MONKEY_EMOJI = "🐒"
BOT_PLAYED_PATH = SCRIPTS_DIR / "bot_played.json"


def read_bot_played(day_key: str, path: Path = BOT_PLAYED_PATH) -> set[str]:
    data = read_leaderboard_data(path)
    return set(data.get(day_key) or [])


def record_bot_played(game_key: str, day_key: str | None = None, path: Path = BOT_PLAYED_PATH) -> None:
    day_key = day_key or linkedin_today().isoformat()
    data = read_leaderboard_data(path)
    games = set(data.get(day_key) or [])
    games.add(game_key)
    data[day_key] = sorted(games)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_leaderboard_data(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def linkedin_today() -> date:
    return datetime.now(tz=ZoneInfo("UTC")).astimezone(_LINKEDIN_TZ).date()


def game_display_name(game_key: str) -> str:
    for g in GAMES:
        if g["key"] == game_key:
            return g["name"]
    return game_key


def is_time_game(game_key: str) -> bool:
    for g in GAMES:
        if g["key"] == game_key:
            return bool(g["is_time"])
    return True


def entry_rank(entry: dict, position: int) -> int:
    rank = entry.get("rank")
    if rank is None:
        return position
    try:
        return int(rank)
    except (TypeError, ValueError):
        return position


def format_player_name(
    entry: dict,
    position: int,
    *,
    my_name: str | None = None,
    marker: str = "←",
) -> str:
    name = resolve_player_name(
        entry.get("name", "?"),
        is_me=bool(entry.get("is_me")),
        my_name=my_name,
    )
    if entry_rank(entry, position) > TOP_RANK:
        name = f"{name} {MONKEY_EMOJI}"
    if entry.get("is_me") and marker:
        name = f"{name} {marker}"
    return name


def sorted_entries(game_key: str, game_data: dict) -> list[dict]:
    entries = game_data.get("entries") or []
    is_time = is_time_game(game_key)
    return sorted(
        entries,
        key=lambda e: (
            e.get("rank") if e.get("rank") is not None else 999,
            score_to_sortable(str(e.get("score", "")), is_time),
        ),
    )


def drop_bot_entry(entries: list[dict]) -> list[dict]:
    mine = [e for e in entries if e.get("is_me")]
    if not mine:
        return entries
    removed_rank = min(
        (entry_rank(e, i) for i, e in enumerate(entries, start=1) if e.get("is_me")),
        default=None,
    )
    kept: list[dict] = []
    for position, entry in enumerate(entries, start=1):
        if entry.get("is_me"):
            continue
        rank = entry_rank(entry, position)
        if removed_rank is not None and rank > removed_rank:
            entry = {**entry, "rank": rank - 1}
        kept.append(entry)
    return kept


def format_game_lines(
    game_key: str,
    game_data: dict,
    *,
    my_name: str | None = None,
    for_slack: bool = False,
    bot_played: bool = False,
) -> list[str]:
    entries = sorted_entries(game_key, game_data)
    if bot_played:
        entries = drop_bot_entry(entries)
    if not entries:
        return []

    title = game_display_name(game_key)
    marker = "" if for_slack else "←"
    lines: list[str] = []

    if for_slack:
        lines.append(f"*{title}*")
    else:
        lines.extend(["", f"=== {title} ===", f"{'Rank':<6} {'Player':<32} {'Score':<10}", "-" * 50])

    for position, entry in enumerate(entries, start=1):
        rank = entry_rank(entry, position)
        name = format_player_name(entry, position, my_name=my_name, marker=marker)
        score = entry.get("score", "—")
        if for_slack:
            me_tag = " _(you)_" if entry.get("is_me") else ""
            lines.append(f"{rank}. {name} — `{score}`{me_tag}")
        else:
            lines.append(f"{str(rank):<6} {name:<32} {score:<10}")

    return lines


def format_day_message(
    data: dict,
    day_key: str,
    layout: Layout,
    *,
    for_slack: bool = False,
) -> str:
    rec = data.get(day_key) or {}
    games = rec.get("games") or {}
    dow = rec.get("day_of_week", "")
    my_name = layout.my_name

    if for_slack:
        header = f"*LinkedIn Games Leaderboard*\n_{day_key}"
        if dow:
            header += f" — {dow}"
        header += "_"
        parts = [header]
    else:
        parts = [f"Leaderboard — {day_key}" + (f" ({dow})" if dow else "")]

    game_keys = [k for k in layout.included_game_keys() if k in games]
    bot_games = read_bot_played(day_key)

    for game_key in game_keys:
        section = format_game_lines(
            game_key,
            games[game_key],
            my_name=my_name,
            for_slack=for_slack,
            bot_played=game_key in bot_games,
        )
        if section:
            if for_slack and len(parts) > 1:
                parts.append("")
            parts.extend(section)

    if len(parts) == 1:
        return parts[0] + ("\n\nNo leaderboard data for today." if for_slack else "\n(no data)")

    return "\n".join(parts)
