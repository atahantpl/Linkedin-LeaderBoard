"""Tests for hybrid leaderboard parsing."""

import json
import tempfile
import unittest
from pathlib import Path

from leaderboard_parser import (
    extract_leaderboard,
    parse_leaderboard_from_api,
    parse_leaderboard_from_html,
    score_to_sortable,
)


SAMPLE_HTML = """
<section class="pr-leaderboard">
  <h3>Connections leaderboard</h3>
  <ul>
    <li class="pr-leaderboard__item">
      <span class="pr-leaderboard__rank">1</span>
      <a href="https://www.linkedin.com/in/aliy/">Ali Y.</a>
      <span class="pr-leaderboard__score">0:15</span>
    </li>
    <li class="pr-leaderboard__item">
      <span class="pr-leaderboard__rank">2</span>
      <span class="pr-leaderboard__name">You</span>
      <span class="pr-leaderboard__score">0:28</span>
    </li>
    <li class="pr-leaderboard__item">
      <span class="pr-leaderboard__rank">3</span>
      <a href="https://www.linkedin.com/in/aysek/">Ayşe K.</a>
      <span class="pr-leaderboard__score">0:42</span>
    </li>
  </ul>
</section>
"""

LINKEDIN_SNAPSHOT_HTML = """
<div class="pr-connections-leaderboard-snapshot__component">
  <div class="pr-connections-leaderboard-player__container">
    <div class="pr-connections-leaderboard-player__name">Siz</div>
    <div class="pr-connections-leaderboard-player__score">0:07</div>
  </div>
  <div class="pr-connections-leaderboard-player__container">
    <div class="pr-connections-leaderboard-player__name">Deniz Kaya</div>
    <div class="pr-connections-leaderboard-player__score">0:08</div>
  </div>
  <div class="pr-connections-leaderboard-player__container">
    <div class="pr-connections-leaderboard-player__name">Can Demir</div>
    <div class="pr-connections-leaderboard-player__score">0:08</div>
  </div>
</div>
"""

SAMPLE_API = {
    "url": "https://www.linkedin.com/voyager/api/graphql?queryId=gamesLeaderboard",
    "body": {
        "data": {
            "gamesLeaderboard": {
                "entries": [
                    {
                        "rank": 1,
                        "memberName": "Ali Y.",
                        "completionTime": "0:15",
                        "profileUrl": "https://www.linkedin.com/in/aliy/",
                    },
                    {
                        "rank": 2,
                        "displayName": "You",
                        "score": "0:28",
                    },
                    {
                        "rank": 4,
                        "firstName": "Mehmet",
                        "lastName": "S.",
                        "elapsedTime": "1:05",
                    },
                ]
            }
        }
    },
}


class LeaderboardParserTests(unittest.TestCase):
    def test_html_parser_finds_connections(self):
        entries = parse_leaderboard_from_html(SAMPLE_HTML, is_time=True)
        names = {e.name for e in entries}
        self.assertIn("Ali Y.", names)
        self.assertIn("You", names)
        self.assertIn("Ayşe K.", names)
        self.assertTrue(any(e.is_me for e in entries if e.name == "You"))

    def test_api_parser_finds_connections(self):
        entries = parse_leaderboard_from_api([SAMPLE_API], is_time=True)
        names = {e.name for e in entries}
        self.assertIn("Ali Y.", names)
        self.assertIn("You", names)
        self.assertIn("Mehmet S.", names)

    def test_hybrid_merge_prefers_both_sources(self):
        entries, source = extract_leaderboard(
            html=SAMPLE_HTML,
            api_payloads=[SAMPLE_API],
            is_time=True,
        )
        self.assertEqual(source, "merged")
        self.assertGreaterEqual(len(entries), 3)

    def test_friends_filter(self):
        entries, _ = extract_leaderboard(
            html=SAMPLE_HTML,
            api_payloads=[],
            is_time=True,
            friends_filter=["Ali"],
        )
        names = {e.name for e in entries}
        self.assertIn("Ali Y.", names)
        self.assertIn("You", names)  # always keep self
        self.assertNotIn("Ayşe K.", names)

    def test_score_sorting(self):
        self.assertLess(score_to_sortable("0:15", True), score_to_sortable("1:05", True))
        self.assertLess(score_to_sortable("3", False), score_to_sortable("5", False))

    def test_linkedin_snapshot_parser(self):
        entries = parse_leaderboard_from_html(LINKEDIN_SNAPSHOT_HTML, is_time=True)
        names = {e.name for e in entries}
        self.assertEqual(len(entries), 3)
        self.assertIn("Deniz Kaya", names)
        self.assertIn("Can Demir", names)
        self.assertTrue(any(e.is_me for e in entries if e.name == "Siz"))

    def test_save_api_debug(self):
        from leaderboard_parser import save_api_debug

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api.json"
            save_api_debug([SAMPLE_API], path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
