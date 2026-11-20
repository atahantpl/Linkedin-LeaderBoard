"""
Play today's LinkedIn games on the saved account so their connection
leaderboards become readable.

LinkedIn ships each puzzle together with its solution in the game page itself
(voyager GraphQL JSON for the older games, the server-rendered component
payload for the newer ones). This script reads it from there and enters the
moves slowly so the account lands near the bottom of each leaderboard.

Usage:
    python play.py --game all                        # every game not yet played today
    python play.py --game queens --duration 60       # one game, target total seconds
    python play.py --game mini_sudoku --guest        # logged-out test run
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import Frame, Page, Response, sync_playwright

from auth_store import get_linkedin_state
from config import SCRIPTS_DIR
from leaderboard_format import record_bot_played

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
START_BUTTON_SELECTOR = "#launch-footer-start-button"
GAMES_QUERY_MARKER = "queryId=voyagerIdentityDashGames."
SDUI_GAME_KEY = '"game":{"$type":"proto.com.linkedin.games.api.Game"'
GUEST_MEMBER_MARKER = "ACoAAAAAAAAB"
FINISHED_RE = re.compile(r"Solved in|Çözüldü|Today.s avg|Bugünün ortalaması", re.IGNORECASE)
CELL = "[data-cell-idx='{}']"
WRONG_PINPOINT_GUESSES = [
    "animals", "colors", "countries", "sports", "music", "movies",
    "cities", "tools", "jobs", "brands", "planets", "insects",
]


class GameData:
    def __init__(self, puzzle: dict, *, guest: bool, finished: bool, puzzle_id: object) -> None:
        self.puzzle = puzzle
        self.guest = guest
        self.finished = finished
        self.puzzle_id = puzzle_id


class PageCapture:
    def __init__(self, slug: str, type_id: Optional[int]) -> None:
        self.slug = slug
        self.type_id = type_id
        self.voyager_item: Optional[dict] = None
        self.document: Optional[str] = None

    def on_response(self, response: Response) -> None:
        url = response.url
        try:
            if response.request.resource_type == "document" and re.search(rf"/games/{re.escape(self.slug)}/?$", url.split("?")[0]):
                if response.status == 200:
                    self.document = response.text()
                return
            if self.type_id is None or self.voyager_item is not None:
                return
            if GAMES_QUERY_MARKER in url and f"gameTypeId:{self.type_id})" in url:
                self.voyager_item = _voyager_item(response.json(), self.type_id)
        except Exception:
            return


def _voyager_item(data: dict, type_id: int) -> Optional[dict]:
    for item in data.get("included", []):
        if item.get("gameTypeId") == type_id and item.get("gamePuzzle"):
            return item
    return None


def _voyager_from_html(html: str, type_id: int) -> Optional[dict]:
    for code in BeautifulSoup(html, "html.parser").find_all("code"):
        text = code.get_text()
        if '"gamePuzzle"' not in text:
            continue
        try:
            item = _voyager_item(json.loads(text), type_id)
        except ValueError:
            continue
        if item:
            return item
    return None


def _sdui_game(document: str) -> Optional[dict]:
    text = document.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
    start = text.find(SDUI_GAME_KEY)
    if start < 0:
        return None
    start += len('"game":')
    depth, in_str, escaped = 0, False, False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:pos + 1])
                except ValueError:
                    return None
    return None


SDUI_DEFAULT_COMPLETION_KEYS = {"$type", "isMistakeFree", "isHintFree"}


def _sdui_finished(game: dict) -> bool:
    attrs = (game.get("gameState") or {}).get("completionAttributes") or {}
    return bool(set(attrs) - SDUI_DEFAULT_COMPLETION_KEYS)


def _load_game_data(page: Page, capture: PageCapture, spec: dict) -> Optional[GameData]:
    if spec["ui"] == "voyager":
        item = capture.voyager_item or _voyager_from_html(page.content(), spec["type_id"])
        if item is None:
            return None
        puzzle = next(v for v in item["gamePuzzle"].values() if isinstance(v, dict))
        state = (item.get("gameStoredRecord") or {}).get("gamePlayState")
        return GameData(
            puzzle,
            guest=GUEST_MEMBER_MARKER in item.get("entityUrn", ""),
            finished=state not in ("UNKNOWN", "IN_PROGRESS", None),
            puzzle_id=item.get("puzzleId"),
        )

    game = _sdui_game(capture.document or "")
    if game is None:
        return None
    case = game["puzzle"]["$case"]
    player = ((game.get("gameUrn") or {}).get("player") or {})
    return GameData(
        game["puzzle"][case],
        guest=not player.get("memberId"),
        finished=_sdui_finished(game),
        puzzle_id=(game.get("gameUrn") or {}).get("puzzleId"),
    )


def _game_frame(page: Page, slug: str, board_selector: str, timeout_s: float = 20) -> Frame:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            if f"/games/view/{slug}/" in frame.url:
                return frame
        for frame in page.frames:
            try:
                if frame.locator(f"{START_BUTTON_SELECTOR}, {board_selector}").count() > 0:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(500)
    raise RuntimeError(f"Game frame for {slug} did not load")


def _dismiss_dialogs(page: Page) -> None:
    close_selectors = (
        "dialog[open] button[aria-label='Kapat']",
        "dialog[open] button[aria-label='Close']",
        "dialog[open] button[aria-label='Dismiss']",
        "button.artdeco-modal__dismiss",
        "[role='dialog'] button[aria-label='Kapat']",
        "[role='dialog'] button[aria-label='Dismiss']",
    )
    for _ in range(3):
        closed = False
        for frame in page.frames:
            for sel in close_selectors:
                try:
                    loc = frame.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(800)
                        closed = True
                except Exception:
                    continue
        if not closed:
            if page.locator("dialog[open]").count() > 0:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
            return


def _click_start(page: Page, frame: Frame) -> None:
    for target in (frame, page):
        loc = target.locator(START_BUTTON_SELECTOR)
        if loc.count() > 0:
            loc.first.click()
            return


def _pace(total_seconds: float, moves: int) -> list[float]:
    if moves == 0:
        return []
    weights = [random.uniform(0.6, 1.4) for _ in range(moves)]
    scale = total_seconds / sum(weights)
    return [w * scale for w in weights]


def _center(frame: Frame, selector: str) -> tuple[float, float]:
    box = frame.locator(selector).first.bounding_box()
    if box is None:
        raise RuntimeError(f"Element not visible: {selector}")
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _drag_through(page: Page, frame: Frame, cells: list[int], step_wait_ms: Callable[[], int]) -> None:
    x, y = _center(frame, CELL.format(cells[0]))
    page.mouse.move(x, y)
    page.mouse.down()
    for idx in cells[1:]:
        page.wait_for_timeout(step_wait_ms())
        x, y = _center(frame, CELL.format(idx))
        page.mouse.move(x, y, steps=6)
    page.wait_for_timeout(random.randint(120, 300))
    page.mouse.up()


def play_mini_sudoku(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    solution: list[int] = puzzle["solution"]
    preset = set(puzzle["presetCellIdxes"])
    todo = [i for i in range(len(solution)) if i not in preset]
    random.shuffle(todo)
    frame.wait_for_selector(".sudoku-cell[data-cell-idx]", timeout=15_000)
    for idx, wait_s in zip(todo, _pace(duration, len(todo))):
        page.wait_for_timeout(int(wait_s * 1000))
        frame.locator(f".sudoku-cell[data-cell-idx='{idx}']").click()
        page.wait_for_timeout(random.randint(150, 400))
        frame.locator(f"button.sudoku-input-button[data-number='{solution[idx]}']").click()


def play_queens(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    size = puzzle["gridSize"]
    cells = [p["row"] * size + p["col"] for p in puzzle["solution"]]
    random.shuffle(cells)
    for idx, wait_s in zip(cells, _pace(duration, len(cells))):
        page.wait_for_timeout(int(wait_s * 1000))
        cell = frame.locator(CELL.format(idx))
        queen = cell.locator("svg[aria-label='Queen'], svg[aria-label='Kraliçe']")
        for _ in range(4):
            if queen.count() > 0:
                break
            cell.click()
            page.wait_for_timeout(random.randint(350, 600))


def play_tango(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    preset = set(puzzle["presetCellIdxes"])
    solution = puzzle["solution"]
    todo = [i for i in range(len(solution)) if i not in preset]
    random.shuffle(todo)
    for idx, wait_s in zip(todo, _pace(duration, len(todo))):
        page.wait_for_timeout(int(wait_s * 1000))
        target = "cell-zero" if solution[idx].endswith("ZERO") else "cell-one"
        cell = frame.locator(CELL.format(idx))
        for _ in range(3):
            cell.click()
            page.wait_for_timeout(random.randint(250, 450))
            if cell.locator(f"[data-testid='{target}']").count() > 0:
                break
        else:
            logger.warning(f"Tango cell {idx}: could not reach {target}")


def play_zip(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    path: list[int] = puzzle["solution"]
    waits = iter(_pace(duration, len(path) - 1))
    _drag_through(page, frame, path, lambda: int(next(waits) * 1000))


def play_patches(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    cols = puzzle["gridCols"]
    regions = list(puzzle["solution"])
    random.shuffle(regions)
    for region, wait_s in zip(regions, _pace(duration, len(regions))):
        page.wait_for_timeout(int(wait_s * 1000))
        idxes = region["cellIdxes"]
        rows = [i // cols for i in idxes]
        cs = [i % cols for i in idxes]
        top_left = min(rows) * cols + min(cs)
        bottom_right = max(rows) * cols + max(cs)
        _drag_through(page, frame, [top_left, bottom_right], lambda: random.randint(300, 700))


def play_wend(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    words = [w["sequencingIndex"] for w in puzzle["solutionWords"]]
    preset = set(puzzle.get("presetWordsIndexes") or [])
    words = [w for i, w in enumerate(words) if i not in preset]
    random.shuffle(words)
    for word, wait_s in zip(words, _pace(duration, len(words))):
        page.wait_for_timeout(int(wait_s * 1000))
        _drag_through(page, frame, word, lambda: random.randint(250, 600))


def play_pinpoint(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    answers = {s.lower() for s in puzzle["solutions"]}
    wrong = [w for w in WRONG_PINPOINT_GUESSES if w not in answers]
    random.shuffle(wrong)
    guesses = wrong[:4] + [puzzle["solutions"][0]]
    box = frame.locator(".pinpoint__input")
    for guess, wait_s in zip(guesses, _pace(duration, len(guesses))):
        page.wait_for_timeout(int(wait_s * 1000))
        if box.count() == 0 or not box.first.is_enabled():
            break
        box.first.click()
        box.first.press_sequentially(guess, delay=random.randint(90, 180))
        page.wait_for_timeout(random.randint(300, 600))
        box.first.press("Enter")


def _crossclimb_words(frame: Frame) -> list[str]:
    return frame.locator(".crossclimb__guess").evaluate_all(
        "els => els.map(e => Array.from(e.querySelectorAll('input')).map(i => i.value).join(''))"
    )


def _crossclimb_type(page: Page, frame: Frame, guess_id: int, word: str) -> None:
    row = frame.locator(f".crossclimb__guess[data-guess-id='{guess_id}']")
    have = "".join(row.locator("input").evaluate_all("els => els.map(e => e.value)"))
    if have == word:
        return
    if not word.startswith(have):
        have = ""
    row.locator("input").nth(len(have)).click()
    page.wait_for_timeout(random.randint(200, 400))
    page.keyboard.type(word[len(have):], delay=random.randint(120, 220))
    page.wait_for_timeout(400)


def _crossclimb_move(page: Page, frame: Frame, src_slot: int, dst_slot: int) -> None:
    handle = frame.locator(
        f".crossclimb__guess[data-guess-id='{src_slot}'] .crossclimb__guess-dragger"
    ).first
    box = handle.bounding_box()
    if box is None:
        raise RuntimeError(f"Crossclimb handle not visible for row {src_slot}")
    sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    _, src_y = _center(frame, f".crossclimb__guess[data-guess-id='{src_slot}']")
    _, dst_y = _center(frame, f".crossclimb__guess[data-guess-id='{dst_slot}']")
    ty = sy + (dst_y - src_y) + (8 if dst_y > src_y else -8)
    steps = max(8, int(abs(ty - sy) // 12))
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.wait_for_timeout(random.randint(250, 350))
    for i in range(1, steps + 1):
        page.mouse.move(sx, sy + (ty - sy) * i / steps)
        page.wait_for_timeout(25)
    page.wait_for_timeout(random.randint(350, 450))
    page.mouse.up()
    page.wait_for_timeout(900)


def play_crossclimb(page: Page, frame: Frame, puzzle: dict, duration: float) -> None:
    rungs = puzzle["rungs"]
    last = len(rungs) - 1
    middle = list(range(1, last))
    waits = iter(_pace(duration, len(middle) + 2))

    for guess_id in middle:
        page.wait_for_timeout(int(next(waits) * 1000))
        _crossclimb_type(page, frame, guess_id, rungs[guess_id]["word"])

    word_at_position = {r["solutionRungIndex"]: r["word"] for r in rungs}
    for target_pos in middle:
        wanted = word_at_position[target_pos]
        for _ in range(3):
            current = _crossclimb_words(frame)
            if current[target_pos] == wanted:
                break
            if wanted not in current[1:last]:
                break
            _crossclimb_move(page, frame, current.index(wanted, 1, last), target_pos)
        else:
            logger.warning(f"Crossclimb: could not move {wanted} to position {target_pos}")
    logger.info(f"Crossclimb order now {_crossclimb_words(frame)[1:last]}")

    for guess_id in (0, last):
        page.wait_for_timeout(int(next(waits) * 1000))
        _crossclimb_type(page, frame, guess_id, rungs[guess_id]["word"])


GAMES = {
    "queens": {"slug": "queens", "ui": "sdui", "type_id": None, "board": "[data-cell-idx]", "play": play_queens},
    "tango": {"slug": "tango", "ui": "sdui", "type_id": None, "board": "[data-cell-idx]", "play": play_tango},
    "zip": {"slug": "zip", "ui": "sdui", "type_id": None, "board": "[data-cell-idx]", "play": play_zip},
    "patches": {"slug": "patches", "ui": "sdui", "type_id": None, "board": "[data-cell-idx]", "play": play_patches},
    "wend": {"slug": "wend", "ui": "sdui", "type_id": None, "board": "[data-cell-idx]", "play": play_wend},
    "mini_sudoku": {"slug": "mini-sudoku", "ui": "voyager", "type_id": 7, "board": ".sudoku-cell[data-cell-idx]", "play": play_mini_sudoku},
    "pinpoint": {"slug": "pinpoint", "ui": "voyager", "type_id": 1, "board": ".pinpoint__input", "play": play_pinpoint},
    "crossclimb": {"slug": "crossclimb", "ui": "voyager", "type_id": 2, "board": ".crossclimb__guess", "play": play_crossclimb},
}


def _finished_view(page: Page) -> bool:
    for frame in page.frames:
        if "/results" in frame.url:
            return True
        try:
            if frame.locator(".pr-golden-chiclet, .pr-top__headline").count() > 0:
                return True
            if frame.get_by_text(FINISHED_RE).count() > 0:
                return True
        except Exception:
            continue
    return False


RESULTS_URL = "https://www.linkedin.com/games/{slug}/results/"


def _results_available(page: Page, slug: str, settle_s: float = 6.0) -> bool:
    try:
        page.goto(RESULTS_URL.format(slug=slug), wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        return False
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        if "/results" not in page.url:
            return False
        page.wait_for_timeout(500)
    return "/results" in page.url and "login" not in page.url


def _wait_for_finish(page: Page, slug: str, timeout_s: float = 30) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _finished_view(page) or _results_available(page, slug):
            return True
        page.wait_for_timeout(2_000)
    return False


def play_one(page: Page, game_key: str, *, duration: float, guest: bool, debug_dir: Optional[Path]) -> bool:
    spec = GAMES[game_key]
    capture = PageCapture(spec["slug"], spec["type_id"])
    page.on("response", capture.on_response)
    moves_started = False
    try:
        if not guest and _results_available(page, spec["slug"]):
            logger.info(f"{game_key}: already finished today — skipping")
            return True

        page.goto(f"https://www.linkedin.com/games/{spec['slug']}/", wait_until="domcontentloaded", timeout=30_000)

        data = None
        deadline = time.monotonic() + 20
        while data is None and time.monotonic() < deadline:
            data = _load_game_data(page, capture, spec)
            if data is None:
                page.wait_for_timeout(500)
        if data is None:
            logger.error(f"{game_key}: puzzle payload not found — not touching the board")
            return False
        if not guest and data.guest:
            logger.error(f"{game_key}: page loaded as a guest — saved session is invalid. Run setup_auth.py.")
            return False
        if data.finished:
            logger.info(f"{game_key}: already finished today — skipping")
            return True

        frame = _game_frame(page, spec["slug"], spec["board"])
        _dismiss_dialogs(page)
        _click_start(page, frame)
        _dismiss_dialogs(page)
        frame = _game_frame(page, spec["slug"], spec["board"])
        logger.info(f"{game_key} #{data.puzzle_id}: playing over ~{duration:.0f}s")
        started = time.monotonic()
        moves_started = True
        spec["play"](page, frame, data.puzzle, duration)
        logger.info(f"{game_key}: moves entered in {time.monotonic() - started:.0f}s, waiting for completion")

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(debug_dir / f"{game_key}_end.png"), full_page=True)
            (debug_dir / f"{game_key}_puzzle.json").write_text(json.dumps(data.puzzle), encoding="utf-8")
        done = _wait_for_finish(page, spec["slug"])
        if done:
            logger.info(f"{game_key}: solved")
            if not guest:
                record_bot_played(game_key)
        else:
            logger.error(f"{game_key}: moves entered but completion not confirmed (url: {page.url})")
        return done
    except Exception as exc:
        first_line = (str(exc).splitlines() or [repr(exc)])[0]
        logger.error(f"{game_key}: {first_line}")
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(debug_dir / f"{game_key}_error.png"), full_page=True)
                (debug_dir / f"{game_key}_error.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        if not guest and _results_available(page, spec["slug"]):
            if moves_started:
                logger.info(f"{game_key}: solved (completion confirmed on the results page)")
                record_bot_played(game_key)
            else:
                logger.info(f"{game_key}: already finished today — skipping")
            return True
        return False
    finally:
        page.remove_listener("response", capture.on_response)


def play(game_keys: list[str], *, duration: float, guest: bool, debug_dir: Optional[Path]) -> int:
    state = None
    if not guest:
        state = get_linkedin_state()
        if state is None:
            logger.error("LinkedIn session not found. Run setup_auth.py first.")
            return 1

    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=state,
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        for key in game_keys:
            if not play_one(page, key, duration=duration, guest=guest, debug_dir=debug_dir):
                failures.append(key)
            page.wait_for_timeout(random.randint(2_000, 5_000))
        browser.close()

    if failures:
        logger.error(f"Not completed: {', '.join(failures)}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Play today's LinkedIn games on the saved account")
    parser.add_argument("--game", choices=["all", *GAMES], required=True)
    parser.add_argument("--duration", type=float, default=30, help="Target seconds per game (default 30)")
    parser.add_argument("--guest", action="store_true", help="Play logged out (test run, nothing is recorded)")
    parser.add_argument("--debug", action="store_true", help="Save end screenshot + puzzle JSON to debug/play/")
    args = parser.parse_args()

    keys = list(GAMES) if args.game == "all" else [args.game]
    debug_dir = SCRIPTS_DIR / "debug" / "play" if args.debug else None
    return play(keys, duration=args.duration, guest=args.guest, debug_dir=debug_dir)


if __name__ == "__main__":
    sys.exit(main())
