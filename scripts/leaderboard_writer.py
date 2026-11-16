"""
JSON writer for LinkedIn Games connection leaderboards.

Stores per-day, per-game connection rankings alongside (but separate from) the
personal score store in json_writer.py. Writes are atomic.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

from leaderboard_parser import ConnectionScore, resolve_player_name
from linkedin_scraper import GameResult
from sheet_layout import Layout, load_layout
from config import GAMES

logger = logging.getLogger(__name__)


def _date_key(today: date) -> str:
    return today.isoformat()


def _read_data(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object keyed by date.")
    return data


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _entry_to_dict(entry: ConnectionScore, my_name: Optional[str] = None) -> dict:
    data = asdict(entry)
    if my_name:
        data["name"] = resolve_player_name(
            data.get("name", ""),
            is_me=bool(data.get("is_me")),
            my_name=my_name,
        )
    # Drop empty optional fields for cleaner JSON.
    if not data.get("profile_url"):
        data.pop("profile_url", None)
    if data.get("rank") is None:
        data.pop("rank", None)
    return data


def write_leaderboard(
    results: list[GameResult],
    today: date,
    path: Path,
    layout: Optional[Layout] = None,
) -> int:
    """
    Upsert today's connection leaderboard entries.

    Returns the number of games written with at least one connection entry.
    """
    layout = layout or load_layout()
    data = _read_data(path)
    key = _date_key(today)

    rec = data.get(key) or {}
    rec["day_of_week"] = today.strftime("%A")
    games_out: dict = rec.get("games") or {}

    name_to_key = {g["name"]: g["key"] for g in GAMES}
    written = 0

    for result in results:
        if not result.connections:
            continue
        game_key = name_to_key.get(result.name)
        if not game_key:
            continue
        if game_key not in layout.included_game_keys():
            continue

        games_out[game_key] = {
            "source": result.leaderboard_source or "none",
            "entries": [_entry_to_dict(e, layout.my_name) for e in result.connections],
        }
        written += 1

    if written == 0:
        return 0

    rec["games"] = games_out
    data[key] = rec
    _atomic_write(path, data)
    logger.info("Leaderboard JSON saved: %s (%d game(s))", path, written)
    return written


def read_leaderboard_day(path: Path, today: date) -> dict:
    data = _read_data(path)
    return data.get(_date_key(today), {})
