"""
LinkedIn Games scraper using Playwright + BeautifulSoup.

Loads a saved browser state (cookies) so no interactive login is needed
on normal runs. Call setup_auth.py once to save the state in the OS credential
store.
"""

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext

from auth_store import get_linkedin_state
from config import GAMES
from leaderboard_parser import (
    ConnectionScore,
    NetworkCapture,
    LINKEDIN_PLAYER_ROW_SELECTOR,
    FULL_LEADERBOARD_BUTTON_SELECTORS,
    captured_game_urn,
    extract_leaderboard,
    fetch_full_leaderboard,
    save_api_debug,
)
from sheet_layout import load_layout

logger = logging.getLogger(__name__)

TIME_SCORE_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
# LinkedIn localizes the avg subtext (e.g. "Today's avg" / "Bugünün ortalaması").
AVG_SUBTEXT_RE = re.compile(
    r"today.?s avg|bugünün ortalaması|\bavg\b|\bortalama",
    re.IGNORECASE,
)
RESULTS_URL_RE = re.compile(r"/games/[^/?#]+/results/?")
RESULTS_DOM_SELECTOR = (
    ".pr-golden-chiclet, .pr-top__headline, "
    ".pr-connections-leaderboard-player__container, .pr-other-games__list-item"
)


def _is_login_redirect(url: str) -> bool:
    lower = url.lower()
    return "login" in lower or "remember-me" in lower or "ssr-login" in lower


def _page_has_results(page: Page) -> bool:
    """True when the browser is on a completed game's results view."""
    url = page.url
    if "/results/" in url or RESULTS_URL_RE.search(url):
        return True
    if _is_login_redirect(url):
        return False
    try:
        if page.locator(RESULTS_DOM_SELECTOR).count() > 0 and "/games/" in url:
            return True
    except Exception:
        pass
    return False


def _navigate_to_results(page: Page, url: str, *, timeout_ms: int = 25_000) -> bool:
    """
    Open a game results URL and wait for LinkedIn's remember-me redirect chain
    to finish. Returns True when the results page loaded, False when redirected
    to the unplayed game page or auth failed.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    if _page_has_results(page):
        return True

    if _is_login_redirect(page.url):
        logger.debug("Waiting for LinkedIn session redirect …")

    deadline_ms = timeout_ms
    try:
        page.wait_for_url(RESULTS_URL_RE, timeout=deadline_ms)
        page.wait_for_selector(RESULTS_DOM_SELECTOR, timeout=5_000)
        return True
    except Exception:
        pass

    # Settled on a game page without /results/ → not played yet today.
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass

    if _is_login_redirect(page.url):
        logger.warning(
            "LinkedIn session redirect did not complete — "
            "try running setup_auth.py again."
        )
        return False

    return _page_has_results(page)


@dataclass
class GameResult:
    name: str
    score: Optional[str]   # "0:17" / "1:23" for time games, "3" for Pinpoint
    avg: Optional[str]     # same format as score
    number: Optional[str] = None  # puzzle number, e.g. "389"
    error: Optional[str] = None
    unplayed: bool = False  # True when the game hasn't been played yet today
    connections: list[ConnectionScore] = field(default_factory=list)
    leaderboard_source: Optional[str] = None  # api | html | merged | none


def _extract_from_html(html: str, game_name: str, is_time: bool) -> tuple[Optional[str], Optional[str]]:
    """
    Parse the rendered page HTML and return (score, avg).
    Both may be None if the game wasn't played or the page structure changed.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Extract puzzle number from the page header, e.g. "Zip #389" → "389"
    number: Optional[str] = None
    subtext_el = soup.select_one(".pr-top__subtext")
    if subtext_el:
        m = re.search(r"#(\d+)", subtext_el.get_text())
        if m:
            number = m.group(1)

    for chiclet in soup.select(".pr-golden-chiclet"):
        subtext_el = chiclet.select_one(".pr-golden-chiclet__subtext")
        text_el    = chiclet.select_one(".pr-golden-chiclet__text")

        if not subtext_el or not text_el:
            continue

        subtext = subtext_el.get_text(strip=True)
        if not AVG_SUBTEXT_RE.search(subtext):
            continue

        # ── Average ───────────────────────────────────────────────────────────
        avg = re.sub(
            r"(?:today.?s avg|bugünün ortalaması)\s*:?\s*",
            "",
            subtext,
            flags=re.IGNORECASE,
        ).strip()

        # ── Score ─────────────────────────────────────────────────────────────
        raw = text_el.get_text(strip=True)

        if is_time:
            # Time games: expect "M:SS" or "MM:SS"
            m = re.search(r"\d+:\d{2}", raw)
            score = m.group(0) if m else None
        else:
            # Pinpoint: "Solved in 3"
            m = re.search(r"solved in (\d+)", raw, re.IGNORECASE)
            score = m.group(1) if m else None

        return score, avg, number

    # No chiclets — check for a loss ("Practice makes perfect!" headline)
    if not is_time:
        headline = soup.select_one(".pr-top__headline")
        if headline and re.search(r"practice makes perfect", headline.get_text(), re.IGNORECASE):
            logger.info(f"{game_name}: loss detected (no correct answer found in 5 guesses)")
            return "5", None, number

    return None, None, number


def _get_unplayed_names(page: Page) -> set[str]:
    """
    Parse the 'Play another game' section on a results page and return the
    set of game names that haven't been played today.

    Each list item contains a link whose raw href is a relative path like
    /games/crossclimb/ — we match that path against GAMES config entries.
    """
    soup = BeautifulSoup(page.content(), "html.parser")
    unplayed: set[str] = set()

    for item in soup.select(".pr-other-games__list-item"):
        link = item.select_one("a[href]")
        if not link:
            continue
        href_path = link["href"].rstrip("/")
        for game in GAMES:
            game_base_path = game["url"].split("linkedin.com")[-1].replace("/results/", "").rstrip("/")
            if href_path == game_base_path:
                unplayed.add(game["name"])
                break

    return unplayed


def _expand_full_leaderboard(page: Page) -> bool:
    """
    Click the 'open full leaderboard' control when the results page only shows
    a 3-player snapshot preview.
    """
    try:
        before = page.locator(LINKEDIN_PLAYER_ROW_SELECTOR).count()
        if before == 0:
            return False

        btn = None
        for selector in FULL_LEADERBOARD_BUTTON_SELECTORS:
            loc = page.locator(selector)
            if loc.count() > 0:
                btn = loc.first
                break
        if btn is None:
            return False

        btn.scroll_into_view_if_needed()
        btn.click()

        try:
            page.wait_for_function(
                """([selector, previous]) => (
                    document.querySelectorAll(selector).length > previous
                )""",
                arg=[LINKEDIN_PLAYER_ROW_SELECTOR, before],
                timeout=8_000,
            )
        except Exception:
            page.wait_for_timeout(2_000)

        after = page.locator(LINKEDIN_PLAYER_ROW_SELECTOR).count()
        if after > before:
            logger.info(f"Expanded connection leaderboard: {before} -> {after} player(s)")
            return True
        logger.debug("Full leaderboard button clicked but player count did not increase")
    except Exception as exc:
        logger.debug(f"Could not expand full leaderboard: {exc}")
    return False


def _scroll_for_leaderboard(page: Page) -> None:
    """Scroll the results page so lazy-loaded leaderboard widgets can appear."""
    try:
        page.evaluate(
            """() => {
                window.scrollTo(0, document.body.scrollHeight);
                const nodes = document.querySelectorAll('[class*="leaderboard"], [class*="Leaderboard"]');
                for (const node of nodes) node.scrollIntoView({ block: 'center' });
            }"""
        )
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _fetch_game(
    page: Page,
    game: dict,
    debug_dir: Optional[Path] = None,
    already_loaded: bool = False,
    *,
    track_connections: bool = False,
    friends_filter: Optional[list[str]] = None,
    network_capture: Optional[NetworkCapture] = None,
) -> GameResult:
    """Navigate to a single game results URL and extract score + avg."""
    name    = game["name"]
    url     = game["url"]
    is_time = game["is_time"]
    slug    = name.lower().replace(" ", "_")

    logger.info(f"Fetching {name} ...")
    try:
        if network_capture is not None and not already_loaded:
            network_capture.clear()

        if not already_loaded:
            if not _navigate_to_results(page, url):
                logger.info(f"{name}: not yet completed today (redirected to {page.url})")
                return GameResult(name=name, score=None, avg=None, unplayed=True)
        elif not _page_has_results(page):
            if not _navigate_to_results(page, url):
                logger.info(f"{name}: not yet completed today (redirected to {page.url})")
                return GameResult(name=name, score=None, avg=None, unplayed=True)

        # If the page redirected away from /results/ the game hasn't been played yet
        if not _page_has_results(page):
            logger.info(f"{name}: not yet completed today (redirected to {page.url})")
            return GameResult(name=name, score=None, avg=None, unplayed=True)

        # Wait for either the chiclet carousel (win) or the headline (loss)
        try:
            page.wait_for_selector(".pr-golden-chiclet, .pr-top__headline", timeout=15_000)
        except Exception:
            # Selector never appeared — still save debug output so we can see why
            if debug_dir:
                _save_debug(page, debug_dir, slug, chiclets_found=False)
            raise

        # Personal score lives in the chiclet carousel, which the expanded
        # leaderboard view replaces — capture it before opening the full list.
        score_html = page.content()
        score, avg, number = _extract_from_html(score_html, name, is_time)

        if track_connections:
            _scroll_for_leaderboard(page)
            try:
                page.wait_for_selector(
                    LINKEDIN_PLAYER_ROW_SELECTOR,
                    timeout=5_000,
                )
            except Exception:
                try:
                    page.wait_for_selector(
                        "[class*='leaderboard'], [class*='Leaderboard'], .pr-leaderboard",
                        timeout=5_000,
                    )
                except Exception:
                    logger.debug(f"{name}: no obvious leaderboard widget detected")
            _expand_full_leaderboard(page)

        html = page.content()

        if debug_dir:
            _save_debug(page, debug_dir, slug, chiclets_found=True)
            if track_connections and network_capture is not None and network_capture.payloads:
                save_api_debug(
                    network_capture.payloads,
                    debug_dir / f"{slug}_api.json",
                )

        connections: list[ConnectionScore] = []
        leaderboard_source: Optional[str] = None
        if track_connections:
            payloads = network_capture.payloads if network_capture is not None else []
            full_entries: list[ConnectionScore] = []
            game_urn = captured_game_urn(payloads)
            if game_urn:
                try:
                    full_entries = fetch_full_leaderboard(page, game_urn, is_time)
                except Exception as exc:
                    logger.debug(f"{name}: full leaderboard query failed — {exc}")
            else:
                logger.debug(f"{name}: leaderboard query not captured — using the page snapshot")
            connections, leaderboard_source = extract_leaderboard(
                html=html,
                api_payloads=payloads,
                is_time=is_time,
                friends_filter=friends_filter,
                full_entries=full_entries,
            )
            if connections:
                logger.info(
                    f"{name}: leaderboard — {len(connections)} connection(s) via {leaderboard_source}"
                )
            else:
                logger.info(f"{name}: no connection leaderboard data found")

        if score is None and avg is None:
            logger.warning(f"{name}: no score card found — game may not have been played today")
        else:
            num_str = f" #{number}" if number else ""
            logger.info(f"{name}{num_str}: score={score}, avg={avg}")

        return GameResult(
            name=name,
            score=score,
            avg=avg,
            number=number,
            connections=connections,
            leaderboard_source=leaderboard_source,
        )

    except Exception as exc:
        logger.error(f"{name}: error fetching results — {exc}")
        return GameResult(name=name, score=None, avg=None, error=str(exc))


def _save_debug(page: Page, debug_dir: Path, slug: str, chiclets_found: bool) -> None:
    """Save a screenshot and HTML dump for a single game page."""
    debug_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = debug_dir / f"{slug}.png"
    html_path       = debug_dir / f"{slug}.html"

    page.screenshot(path=str(screenshot_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    # Log a quick summary of what chiclets were found in the HTML
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.content(), "html.parser")
    chiclets = soup.select(".pr-golden-chiclet")
    texts = [c.get_text(separator=" | ", strip=True)[:80] for c in chiclets]

    logger.debug(f"  [{slug}] chiclets_found={chiclets_found}, count={len(chiclets)}")
    for t in texts:
        logger.debug(f"    · {t}")
    logger.info(f"  Debug files saved: {screenshot_path.name}, {html_path.name}")


def fetch_all_scores(
    names: Optional[set[str]] = None,
    debug_dir: Optional[Path] = None,
    anchor_name: Optional[str] = None,
    *,
    track_connections: bool = False,
    friends_filter: Optional[list[str]] = None,
) -> list[GameResult]:
    """
    Launch a headless Playwright browser, restore the saved LinkedIn session,
    and scrape game results pages. Returns a list of GameResult objects in
    layout order (the games included in config.json); games excluded from
    the layout are never fetched or returned.

    names: optional set of game names to fetch. Layout-included games not in the
           set are returned as all-None placeholders so the list always covers
           every layout game. Pass None (default) to fetch every layout game.

    debug_dir: save a screenshot + HTML dump for every page visited when set.

    anchor_name: display name of a game to prefer as the anchor results page
                 when no already-recorded game is available. Typically the
                 game the user plays first each day, so its results page is
                 the most likely to be complete on a fresh day.
    """
    linkedin_state = get_linkedin_state()
    if linkedin_state is None:
        raise FileNotFoundError(
            "LinkedIn session not found in the OS credential store.\n"
            "Run setup_auth.py first to log in and save your session."
        )

    results: list[GameResult] = []

    # The layout (config.json) is the source of truth for which games
    # the collector cares about. Restrict everything below to layout-included
    # games so excluded games (e.g. CrossClimb) are never fetched, printed, or
    # returned — even on a "fetch all" run where `names` is None.
    try:
        included_names = set(load_layout().included_game_names())
        layout_games = [g for g in GAMES if g["name"] in included_names]
    except Exception as exc:
        logger.warning(f"Could not read layout ({exc}); fetching all configured games.")
        layout_games = list(GAMES)
    if not layout_games:
        layout_games = list(GAMES)

    games_to_fetch = [g for g in layout_games if names is None or g["name"] in names]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context: BrowserContext = browser.new_context(
            storage_state=linkedin_state,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        network_capture = NetworkCapture()
        if track_connections:
            network_capture.attach(page)

        # ── Determine unplayed games via a completed results page ─────────────
        # Anchor cascade (first match wins):
        #   1. Any game already recorded in today's row (not in games_to_fetch)
        #      — guaranteed complete, so the unplayed-list widget loads.
        #   2. The caller-provided anchor_name, when supplied. Intended for
        #      "the game the user typically plays first", which is the most
        #      likely to be complete on a fresh day with nothing yet recorded.
        #   3. The first game in games_to_fetch as a last resort.
        fetch_names = {g["name"] for g in games_to_fetch}
        already_recorded = next(
            (g for g in layout_games if g["name"] not in fetch_names), None
        )
        configured_anchor = (
            next((g for g in layout_games if g["name"] == anchor_name), None)
            if anchor_name else None
        )
        anchor = already_recorded or configured_anchor or games_to_fetch[0]
        logger.info(f"Loading anchor results page ({anchor['name']}) to check unplayed games …")
        anchor_played = _navigate_to_results(page, anchor["url"])

        if anchor_played:
            try:
                page.wait_for_selector(".pr-other-games__list-item", timeout=10_000)
            except Exception:
                logger.debug("No '.pr-other-games__list-item' found — all games may be complete")
            unplayed = _get_unplayed_names(page)
            if unplayed:
                logger.info(f"Not yet played today: {', '.join(sorted(unplayed))}")
        else:
            # Anchor game itself isn't complete — can't read the list
            logger.debug(f"Anchor game ({anchor['name']}) not yet completed — skipping unplayed check")
            unplayed = set()

        # ── Timer-safety guard: bail out if the anchor itself is unplayed ─────
        # When the anchor's results page redirected away from /results/, the
        # anchor game hasn't been played yet, so the unplayed-list widget never
        # loaded and we don't know which games are safe to open. Probing each
        # game individually would navigate to its page and can start the timer
        # on an unplayed *timed* game (see SETUP.md "Anchor Games"). To stay
        # timer-safe we stop here and report every to-fetch game as unplayed —
        # only the anchor page itself was loaded. A later run, once the anchor
        # has been played, collects everything normally. Picking a non-timed
        # anchor (e.g. Pinpoint) makes even that single anchor load risk-free.
        if not anchor_played:
            logger.warning(
                f"Anchor game ({anchor['name']}) has not been played yet today — "
                "skipping all game probes to avoid starting timers on unplayed "
                "games. Nothing collected this run; re-run after playing the anchor."
            )
            browser.close()
            fetched = {
                g["name"]: GameResult(name=g["name"], score=None, avg=None, unplayed=True)
                for g in games_to_fetch
            }
            return [
                fetched.get(g["name"], GameResult(name=g["name"], score=None, avg=None))
                for g in layout_games
            ]

        # ── Fetch each game that needs data and isn't known-unplayed ──────────
        # Process the anchor first while its results page (loaded above for the
        # unplayed check) is still the one in the browser. Otherwise an earlier
        # navigation would leave a different game's results page loaded, and the
        # `already_loaded` shortcut below — which only checks that *some*
        # /results/ page is open — would scrape that wrong page for the anchor.
        fetch_order = sorted(games_to_fetch, key=lambda g: g["name"] != anchor["name"])
        fetched: dict[str, GameResult] = {}
        for game in fetch_order:
            if game["name"] in unplayed:
                logger.info(f"{game['name']}: not yet played today — skipping")
                fetched[game["name"]] = GameResult(name=game["name"], score=None, avg=None, unplayed=True)
            elif game["name"] == anchor["name"] and _page_has_results(page):
                # Anchor page is already loaded — extract directly without re-navigating
                fetched[game["name"]] = _fetch_game(
                    page,
                    game,
                    debug_dir=debug_dir,
                    already_loaded=True,
                    track_connections=track_connections,
                    friends_filter=friends_filter,
                    network_capture=network_capture,
                )
            else:
                fetched[game["name"]] = _fetch_game(
                    page,
                    game,
                    debug_dir=debug_dir,
                    track_connections=track_connections,
                    friends_filter=friends_filter,
                    network_capture=network_capture,
                )

        if track_connections:
            network_capture.detach(page)
        browser.close()

    # Rebuild in layout order; games not in fetch list get all-None placeholders.
    # Excluded games are absent from layout_games, so they never appear here.
    for game in layout_games:
        results.append(fetched.get(game["name"], GameResult(name=game["name"], score=None, avg=None)))

    return results
