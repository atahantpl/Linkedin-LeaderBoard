"""
Hybrid leaderboard extraction for LinkedIn Games results pages.

Strategy (in priority order):
  1. Intercept Playwright network responses (GraphQL / Voyager JSON).
  2. Parse rendered HTML for connection leaderboard widgets.
  3. Merge and deduplicate by player name.

LinkedIn's DOM and API shapes change over time; this module uses tolerant
heuristics rather than a single brittle selector.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page, Response

logger = logging.getLogger(__name__)

TIME_SCORE_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
GUESS_SCORE_RE = re.compile(r"(?:solved in\s+)?(\d+)\s*(?:guesses?)?", re.IGNORECASE)
RANK_RE = re.compile(r"^#?(\d+)$")

NAME_KEYS = frozenset({
    "name", "fullname", "displayname", "membername", "titletext",
    "firstname", "actorname", "playername", "connectionname",
})
SCORE_KEYS = frozenset({
    "score", "time", "duration", "result", "value", "gamescore",
    "completiontime", "elapsedtime", "guesses", "guesscount",
})
RANK_KEYS = frozenset({"rank", "position", "order", "place"})
URL_KEYS = frozenset({"profileurl", "url", "profile", "permalink"})

LEADERBOARD_URL_HINTS = (
    "leaderboard",
    "games",
    "graphql",
    "voyager/api",
    "rsc-action",
    "gameplay",
    "connections",
)

HTML_LEADERBOARD_SELECTORS = (
    ".pr-connections-leaderboard-snapshot__component",
    ".pr-connections-leaderboard-player__container",
    "[class*='connections-leaderboard']",
    "[class*='leaderboard']",
    "[class*='Leaderboard']",
    ".pr-leaderboard",
    ".pr-leaderboard__list",
    ".pr-leaderboard__item",
    ".pr-leaderboard__row",
    ".pr-connection-leaderboard",
    ".pr-social-proof",
    "[data-test-id*='leaderboard']",
)

LINKEDIN_PLAYER_ROW_SELECTOR = ".pr-connections-leaderboard-player__container"
LINKEDIN_PLAYER_NAME_SELECTOR = ".pr-connections-leaderboard-player__name"
LINKEDIN_PLAYER_SCORE_SELECTOR = ".pr-connections-leaderboard-player__score"

# Results-page snapshot shows ~3 players; full list needs this button/modal.
FULL_LEADERBOARD_BUTTON_SELECTORS = (
    'button:has-text("Lider panosunun tamamını aç")',
    'button:has-text("See full leaderboard")',
    'button:has-text("See the full leaderboard")',
    'button:has-text("Open full leaderboard")',
    'button:has-text("View full leaderboard")',
)

CONNECTIONS_QUERY_MARKER = "voyagerIdentityDashGameConnectionsEntities"
FULL_LEADERBOARD_QUERY_ID = "voyagerIdentityDashGameConnectionsEntities.3ba704202db4679ab5a17add51058ae8"
FULL_LEADERBOARD_PAGE_SIZE = 50
GAME_URN_RE = re.compile(r"gameUrn:([^,)&]+)")

FETCH_JSON_JS = """async (url) => {
    const m = document.cookie.match(/JSESSIONID="?([^";]+)"?/);
    const r = await fetch(url, {
        credentials: 'include',
        headers: {
            'csrf-token': m ? m[1] : '',
            'x-restli-protocol-version': '2.0.0',
            'accept': 'application/vnd.linkedin.normalized+json+2.1',
        },
    });
    return {status: r.status, body: await r.text()};
}"""

HTML_ROW_SELECTORS = (
    "[class*='leaderboard'] li",
    "[class*='leaderboard'] [class*='item']",
    "[class*='leaderboard'] [class*='row']",
    "[class*='leaderboard'] tr",
    ".pr-leaderboard__item",
    ".pr-leaderboard__row",
    ".pr-leaderboard li",
)

HTML_NAME_SELECTORS = (
    "[class*='name']",
    "[class*='actor']",
    "[class*='member']",
    "[class*='title']",
    "a[href*='/in/']",
    "span[dir='ltr']",
)

ME_MARKERS = (
    "you",
    "(you)",
    "sen",
    "siz",
    "me",
)


@dataclass
class ConnectionScore:
    name: str
    score: str
    rank: Optional[int] = None
    is_me: bool = False
    profile_url: Optional[str] = None

    def dedupe_key(self) -> str:
        return self.name.strip().lower()


@dataclass
class NetworkCapture:
    """Collect JSON API responses while a results page loads."""

    payloads: list[dict[str, Any]] = field(default_factory=list)
    _handler: Any = field(default=None, repr=False)

    def attach(self, page: Page) -> None:
        def on_response(response: Response) -> None:
            if not _is_leaderboard_candidate_url(response.url):
                return
            try:
                content_type = response.headers.get("content-type", "")
                if response.status != 200:
                    return
                if "json" not in content_type and "graphql" not in response.url:
                    return
                body = response.json()
                self.payloads.append({"url": response.url, "body": body})
                logger.debug("Captured API response: %s", response.url[:120])
            except Exception:
                return

        self._handler = on_response
        page.on("response", on_response)

    def detach(self, page: Page) -> None:
        if self._handler is not None:
            try:
                page.remove_listener("response", self._handler)
            except Exception:
                pass
            self._handler = None

    def clear(self) -> None:
        self.payloads.clear()


def _is_leaderboard_candidate_url(url: str) -> bool:
    lower = url.lower()
    return any(hint in lower for hint in LEADERBOARD_URL_HINTS)


def _normalize_score(raw: Any, is_time: bool) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if is_time:
        m = TIME_SCORE_RE.search(text)
        return m.group(1) if m else None
    if TIME_SCORE_RE.fullmatch(text):
        return None
    m = GUESS_SCORE_RE.search(text)
    if m:
        return m.group(1)
    if text.isdigit():
        return text
    return None


def _looks_like_person_name(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 80:
        return False
    if text.lower() in ME_MARKERS:
        return True
    if re.fullmatch(r"#?\d+", text):
        return False
    if re.search(r"\b(today|average|leaderboard|connections?|company|school)\b", text, re.I):
        return False
    if TIME_SCORE_RE.fullmatch(text):
        return False
    # Require at least one letter; allow initials like "Ali Y."
    return bool(re.search(r"[A-Za-zÀ-ÿ]", text))


def _is_me_name(name: str) -> bool:
    lower = name.strip().lower()
    if lower in ME_MARKERS:
        return True
    return bool(re.search(r"\(you\)|\bsen\b|\bsiz\b", lower))


def resolve_player_name(
    name: str,
    *,
    is_me: bool = False,
    my_name: Optional[str] = None,
) -> str:
    """Replace LinkedIn's localized 'You' label with the configured display name."""
    if my_name and (is_me or _is_me_name(name)):
        return my_name
    return name


def _pick_first(mapping: dict[str, Any], keys: frozenset[str]) -> Any:
    for key, value in mapping.items():
        if key.lower() in keys and value not in (None, "", [], {}):
            return value
    return None


def _extract_name_from_mapping(mapping: dict[str, Any]) -> Optional[str]:
    first = mapping.get("firstName") or mapping.get("first_name")
    last = mapping.get("lastName") or mapping.get("last_name")
    if first or last:
        combined = f"{first or ''} {last or ''}".strip()
        if _looks_like_person_name(combined):
            return combined

    direct = _pick_first(mapping, NAME_KEYS)
    if isinstance(direct, str) and _looks_like_person_name(direct):
        return direct.strip()

    nested = mapping.get("name")
    if isinstance(nested, dict):
        text = nested.get("text") or nested.get("accessibilityText")
        if isinstance(text, str) and _looks_like_person_name(text):
            return text.strip()

    return None


def _extract_profile_url(mapping: dict[str, Any]) -> Optional[str]:
    url = _pick_first(mapping, URL_KEYS)
    if isinstance(url, str) and "/in/" in url:
        return url
    for value in mapping.values():
        if isinstance(value, str) and "linkedin.com/in/" in value:
            return value
    return None


def _parse_api_object(obj: dict[str, Any], is_time: bool) -> Optional[ConnectionScore]:
    name = _extract_name_from_mapping(obj)
    if not name:
        return None

    score_raw = _pick_first(obj, SCORE_KEYS)
    score = _normalize_score(score_raw, is_time)
    if score is None:
        # Sometimes score lives in nested result/completion objects.
        for value in obj.values():
            if isinstance(value, dict):
                nested = _parse_api_object(value, is_time)
                if nested and nested.name == name:
                    score = nested.score
                    break
            elif isinstance(value, (int, float, str)):
                score = _normalize_score(value, is_time)
                if score:
                    break
    if score is None:
        return None

    rank_raw = _pick_first(obj, RANK_KEYS)
    rank: Optional[int] = None
    if rank_raw is not None:
        try:
            rank = int(rank_raw)
        except (TypeError, ValueError):
            m = RANK_RE.match(str(rank_raw))
            rank = int(m.group(1)) if m else None

    return ConnectionScore(
        name=name,
        score=score,
        rank=rank,
        is_me=_is_me_name(name),
        profile_url=_extract_profile_url(obj),
    )


def _walk_api_node(node: Any, is_time: bool, found: list[ConnectionScore]) -> None:
    if isinstance(node, dict):
        parsed = _parse_api_object(node, is_time)
        if parsed:
            found.append(parsed)
        for value in node.values():
            _walk_api_node(value, is_time, found)
    elif isinstance(node, list):
        for item in node:
            _walk_api_node(item, is_time, found)


def parse_leaderboard_from_api(payloads: list[dict[str, Any]], is_time: bool) -> list[ConnectionScore]:
    """Extract connection scores from captured network JSON payloads."""
    found: list[ConnectionScore] = []
    for item in payloads:
        _walk_api_node(item.get("body"), is_time, found)
    return _dedupe_entries(found)


def captured_game_urn(payloads: list[dict[str, Any]]) -> Optional[str]:
    for item in reversed(payloads):
        url = item.get("url") or ""
        if CONNECTIONS_QUERY_MARKER not in url or FULL_LEADERBOARD_QUERY_ID in url:
            continue
        m = GAME_URN_RE.search(url)
        if m:
            return unquote(m.group(1))
    return None


def _full_leaderboard_page(page: Page, game_urn: str, start: int) -> Optional[dict[str, Any]]:
    url = (
        "https://www.linkedin.com/voyager/api/graphql?includeWebMetadata=true"
        f"&variables=(gameUrn:{quote(game_urn, safe='')},start:{start},count:{FULL_LEADERBOARD_PAGE_SIZE})"
        f"&queryId={FULL_LEADERBOARD_QUERY_ID}"
    )
    res = page.evaluate(FETCH_JSON_JS, url)
    if res.get("status") != 200:
        logger.debug("Full leaderboard query returned HTTP %s", res.get("status"))
        return None
    try:
        return json.loads(res["body"])
    except (KeyError, ValueError):
        return None


def _format_api_score(game_score: dict[str, Any], is_time: bool) -> Optional[str]:
    if is_time:
        seconds = game_score.get("timeElapsed")
        if seconds is None:
            return None
        return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
    guesses = game_score.get("totalGuessCount")
    return str(guesses) if guesses is not None else None


def fetch_full_leaderboard(page: Page, game_urn: str, is_time: bool) -> list[ConnectionScore]:
    me_match = re.search(r"fsd_game:\(([^,]+),", game_urn)
    me_urn = f"urn:li:fsd_profile:{me_match.group(1)}" if me_match else None

    found: list[ConnectionScore] = []
    start = 0
    while True:
        data = _full_leaderboard_page(page, game_urn, start)
        if data is None:
            break
        profiles = {
            inc.get("entityUrn"): inc
            for inc in data.get("included") or []
            if isinstance(inc, dict) and "firstName" in inc
        }
        roots = [v for v in ((data.get("data") or {}).get("data") or {}).values() if isinstance(v, dict)]
        if not roots:
            break
        elements = roots[0].get("elements") or []
        for element in elements:
            score = _format_api_score(element.get("gameScore") or {}, is_time)
            player = ((element.get("playerDetails") or {}).get("player") or {})
            profile_urn = player.get("*profile")
            profile = profiles.get(profile_urn) or {}
            name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
            if not name or score is None:
                continue
            public_id = profile.get("publicIdentifier")
            found.append(ConnectionScore(
                name=name,
                score=score,
                rank=element.get("ranking"),
                is_me=profile_urn is not None and profile_urn == me_urn,
                profile_url=f"https://www.linkedin.com/in/{public_id}/" if public_id else None,
            ))
        total = ((roots[0].get("paging") or {}).get("total")) or 0
        start += FULL_LEADERBOARD_PAGE_SIZE
        if not elements or start >= total:
            break
    return found


def _score_from_element(element: Tag, is_time: bool) -> Optional[str]:
    text = element.get_text(" ", strip=True)
    return _normalize_score(text, is_time)


def _name_from_element(element: Tag) -> Optional[str]:
    for sel in HTML_NAME_SELECTORS:
        node = element.select_one(sel)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        if _looks_like_person_name(text):
            return text
    # Fallback: longest plausible text chunk in the row.
    chunks = [c.strip() for c in element.stripped_strings if _looks_like_person_name(c)]
    if not chunks:
        return None
    return max(chunks, key=len)


def _profile_from_element(element: Tag) -> Optional[str]:
    link = element.select_one("a[href*='/in/']")
    if link and link.get("href"):
        return link["href"]
    return None


def _rank_from_element(element: Tag) -> Optional[int]:
    rank_node = element.select_one("[class*='rank'], [class*='position']")
    if rank_node:
        m = RANK_RE.match(rank_node.get_text(strip=True))
        if m:
            return int(m.group(1))
    text = element.get_text(" ", strip=True)
    m = re.match(r"^#?(\d+)\b", text)
    return int(m.group(1)) if m else None


def _parse_html_row(row: Tag, is_time: bool) -> Optional[ConnectionScore]:
    name = _name_from_element(row)
    if not name:
        return None
    score = _score_from_element(row, is_time)
    if score is None:
        return None
    return ConnectionScore(
        name=name,
        score=score,
        rank=_rank_from_element(row),
        is_me=_is_me_name(name),
        profile_url=_profile_from_element(row),
    )


def _leaderboard_sections(soup: BeautifulSoup) -> list[Tag]:
    sections: list[Tag] = []
    seen_ids: set[int] = set()

    for sel in HTML_LEADERBOARD_SELECTORS:
        for node in soup.select(sel):
            nid = id(node)
            if nid not in seen_ids:
                seen_ids.add(nid)
                sections.append(node)

    # Broader fallback: headings that mention leaderboard / connections.
    for heading in soup.find_all(["h2", "h3", "h4", "span", "div"], limit=200):
        text = heading.get_text(" ", strip=True).lower()
        if not text:
            continue
        if "leaderboard" in text or "connections" in text and "played" in text:
            parent = heading.find_parent(["section", "div", "article"])
            if parent is not None and id(parent) not in seen_ids:
                seen_ids.add(id(parent))
                sections.append(parent)

    return sections


def _parse_linkedin_player_row(row: Tag, is_time: bool) -> Optional[ConnectionScore]:
    """Parse LinkedIn's pr-connections-leaderboard-player rows."""
    name_el = row.select_one(LINKEDIN_PLAYER_NAME_SELECTOR)
    score_el = row.select_one(LINKEDIN_PLAYER_SCORE_SELECTOR)
    if not name_el or not score_el:
        return None

    name = name_el.get_text(strip=True)
    score = _normalize_score(score_el.get_text(strip=True), is_time)
    if not name or score is None:
        return None

    return ConnectionScore(
        name=name,
        score=score,
        is_me=_is_me_name(name),
        profile_url=_profile_from_element(row),
    )


def parse_leaderboard_from_html(html: str, is_time: bool) -> list[ConnectionScore]:
    """Extract connection scores from rendered results-page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[ConnectionScore] = []

    # Primary: LinkedIn connection leaderboard snapshot (2025+ UI).
    for row in soup.select(LINKEDIN_PLAYER_ROW_SELECTOR):
        parsed = _parse_linkedin_player_row(row, is_time)
        if parsed:
            found.append(parsed)
    if found:
        return _dedupe_entries(found)

    sections = _leaderboard_sections(soup)
    for section in sections:
        for row_sel in HTML_ROW_SELECTORS:
            for row in section.select(row_sel):
                parsed = _parse_html_row(row, is_time)
                if parsed:
                    found.append(parsed)

    # Last resort: scan list items site-wide on results pages.
    if not found:
        for row in soup.select("li, tr"):
            parsed = _parse_html_row(row, is_time)
            if parsed:
                found.append(parsed)

    return _dedupe_entries(found)


def _matches_filter(name: str, friends_filter: Optional[list[str]]) -> bool:
    if not friends_filter:
        return True
    lower = name.lower()
    return any(f.lower() in lower for f in friends_filter)


def _dedupe_entries(entries: list[ConnectionScore]) -> list[ConnectionScore]:
    by_key: dict[str, ConnectionScore] = {}
    for entry in entries:
        key = entry.dedupe_key()
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = entry
            continue
        # Prefer entries with rank / profile URL / longer name.
        score = (
            (1 if entry.rank is not None else 0)
            + (1 if entry.profile_url else 0)
            + len(entry.name)
        )
        existing_score = (
            (1 if existing.rank is not None else 0)
            + (1 if existing.profile_url else 0)
            + len(existing.name)
        )
        if score >= existing_score:
            by_key[key] = entry
    return list(by_key.values())


def merge_leaderboard_sources(
    api_entries: list[ConnectionScore],
    html_entries: list[ConnectionScore],
) -> tuple[list[ConnectionScore], str]:
    """Merge API and HTML extractions; API wins on conflicts."""
    merged = _dedupe_entries(api_entries + html_entries)
    if api_entries and html_entries:
        source = "merged"
    elif api_entries:
        source = "api"
    elif html_entries:
        source = "html"
    else:
        source = "none"
    return merged, source


def extract_leaderboard(
    *,
    html: str,
    api_payloads: list[dict[str, Any]],
    is_time: bool,
    friends_filter: Optional[list[str]] = None,
    full_entries: Optional[list[ConnectionScore]] = None,
) -> tuple[list[ConnectionScore], str]:
    """Run hybrid extraction and apply optional friend-name filter."""
    if full_entries:
        entries, source = list(full_entries), "api"
    else:
        api_entries = parse_leaderboard_from_api(api_payloads, is_time)
        html_entries = parse_leaderboard_from_html(html, is_time)
        entries, source = merge_leaderboard_sources(api_entries, html_entries)

    if friends_filter:
        entries = [e for e in entries if e.is_me or _matches_filter(e.name, friends_filter)]

    # Assign ranks when missing (lower score = better for both time and guesses).
    entries = assign_ranks(entries, is_time)
    return entries, source


def score_to_sortable(score: str, is_time: bool) -> float:
    if is_time and ":" in score:
        mins, secs = score.split(":", 1)
        return int(mins) * 60 + int(secs)
    try:
        return float(score)
    except ValueError:
        return float("inf")


def assign_ranks(entries: list[ConnectionScore], is_time: bool) -> list[ConnectionScore]:
    if not entries:
        return entries
    sorted_entries = sorted(entries, key=lambda e: score_to_sortable(e.score, is_time))
    for i, entry in enumerate(sorted_entries, start=1):
        if entry.rank is None:
            entry.rank = i
    return sorted_entries


def save_api_debug(payloads: list[dict[str, Any]], path) -> None:
    """Persist raw API payloads for troubleshooting DOM/API drift."""
    path = path  # Path-like
    serializable = []
    for item in payloads:
        body = item.get("body")
        try:
            json.dumps(body)
            serializable.append(item)
        except TypeError:
            serializable.append({"url": item.get("url"), "body": str(body)[:5000]})
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
