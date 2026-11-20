import unittest

from play import _sdui_finished


UNPLAYED_GAME = {
    "gameState": {
        "$type": "proto.com.linkedin.games.api.player.GameState",
        "mostRecentGameState": "$undefined",
        "completionAttributes": {
            "$type": "proto.com.linkedin.games.api.player.GameCompletionAttributes",
            "isMistakeFree": True,
            "isHintFree": True,
        },
    },
}


class SduiFinishedTest(unittest.TestCase):
    def test_default_completion_flags_are_not_finished(self):
        self.assertFalse(_sdui_finished(UNPLAYED_GAME))

    def test_missing_game_state_is_not_finished(self):
        self.assertFalse(_sdui_finished({}))


if __name__ == "__main__":
    unittest.main()
