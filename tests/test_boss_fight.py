import os
import random
import tempfile
import unittest

from arcadion_bot.game import Units, apply_phase_bonus_to_rolls, determine_arcadion_phase, resolve_surprise_attack
from arcadion_bot.storage import Store


class BossFightTests(unittest.TestCase):
    def test_phase_detection_follows_the_requested_thresholds(self) -> None:
        self.assertEqual(determine_arcadion_phase(100, 100), 1)
        self.assertEqual(determine_arcadion_phase(75, 100), 2)
        self.assertEqual(determine_arcadion_phase(50, 100), 3)
        self.assertEqual(determine_arcadion_phase(25, 100), 4)

    def test_phase_bonus_never_exceeds_six(self) -> None:
        rolls = [6, 5, 1]
        adjusted = apply_phase_bonus_to_rolls(rolls, 2)
        self.assertEqual(max(adjusted), 6)

    def test_surprise_attack_keeps_commander_active_when_units_remain(self) -> None:
        random.seed(0)
        loss, eliminated = resolve_surprise_attack(Units(bulls=4), 5000)
        self.assertFalse(eliminated)
        self.assertEqual(loss.destroyed.bulls, 3)
        self.assertEqual(loss.remaining.bulls, 1)

    def test_timeout_surprise_attack_does_not_eliminate_until_all_units_are_destroyed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(os.path.join(tmpdir, "test.sqlite3"))
            store.init()
            raid_id = store.create_raid("Raid", "City", "1", 1000, 1, Units(), 0)
            store.upsert_participant(raid_id, 1, "Alpha", Units(bulls=4))
            store.start_raid(raid_id, None)
            with store.connect() as conn:
                conn.execute(
                    "UPDATE raids SET turn_deadline_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00+00:00", raid_id),
                )
            next_player = store.process_turn_timeout(raid_id, None)
            participant = store.get_participant(raid_id, 1)
            self.assertEqual(participant["status"], "ACTIVE")
            self.assertEqual(participant["bulls"], 1)
            self.assertEqual(next_player["discord_id"], "1")


if __name__ == "__main__":
    unittest.main()
