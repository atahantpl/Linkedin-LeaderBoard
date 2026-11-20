import unittest

from leaderboard_format import drop_bot_entry, drop_excluded


ENTRIES = [
    {"name": "Ali Y.", "score": "0:20", "rank": 1, "is_me": False},
    {"name": "Ayşe K.", "score": "0:30", "rank": 2, "is_me": False},
    {"name": "Me", "score": "0:40", "rank": 3, "is_me": True},
    {"name": "Veli Z.", "score": "0:50", "rank": 4, "is_me": False},
]


class DropExcludedTest(unittest.TestCase):
    def test_excluded_player_is_removed_and_ranks_close_up(self):
        result = drop_excluded(ENTRIES, ["ayşe"])
        self.assertEqual([e["name"] for e in result], ["Ali Y.", "Me", "Veli Z."])
        self.assertEqual([e["rank"] for e in result], [1, 2, 3])

    def test_self_is_never_excluded(self):
        result = drop_excluded(ENTRIES, ["Me"])
        self.assertIn("Me", [e["name"] for e in result])

    def test_empty_list_keeps_everyone(self):
        self.assertEqual(drop_excluded(ENTRIES, []), ENTRIES)

    def test_bot_entry_drop_still_re_ranks(self):
        result = drop_bot_entry(ENTRIES)
        self.assertEqual([e["rank"] for e in result], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
