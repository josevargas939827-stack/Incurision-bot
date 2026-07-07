import os
import tempfile
import unittest

from arcadion_bot.game import Units, calculate_loot_distribution, parse_integer
from arcadion_bot.storage import Store


class ParseIntegerTests(unittest.TestCase):
    def test_accepts_common_numeric_formats(self) -> None:
        cases = {
            "1500000": 1500000,
            "1.500.000": 1500000,
            "1,500,000": 1500000,
            "1500k": 1500000,
            "250k": 250000,
            "1.5m": 1500000,
            "2m": 2000000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_integer(raw), expected)

    def test_rejects_invalid_formats(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_integer("1.2.3")
        self.assertIn("Accepted formats", str(ctx.exception))

    def test_distributes_loot_proportionally_and_exactly(self) -> None:
        entries = calculate_loot_distribution(
            500000,
            [("Alejo", 500000), ("Pedro", 375000), ("María", 250000)],
        )
        self.assertEqual(
            [(entry["player"], entry["reward"]) for entry in entries],
            [("Alejo", 222222), ("Pedro", 166667), ("María", 111111)],
        )
        self.assertEqual(sum(entry["reward"] for entry in entries), 500000)
        self.assertEqual([entry["participation_percent"] for entry in entries], ["44%", "33%", "22%"])


class StoreEliminationTests(unittest.TestCase):
    def test_advances_to_non_eliminated_participant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(os.path.join(tmpdir, "test.sqlite3"))
            store.init()
            raid_id = store.create_raid("Raid", "City", "1", 100, 1, Units(), 0)
            store.upsert_participant(raid_id, 1, "Alpha", Units(bulls=1))
            store.upsert_participant(raid_id, 2, "Beta", Units(bulls=1))
            store.start_raid(raid_id, None)
            with store.connect() as conn:
                conn.execute("UPDATE raid_participants SET status = 'ELIMINATED' WHERE raid_id = ? AND discord_id = ?", (raid_id, "1"))
                conn.execute("UPDATE raids SET current_turn_discord_id = NULL WHERE id = ?", (raid_id,))
            next_player = store.advance_turn(raid_id, None)
            self.assertIsNotNone(next_player)
            self.assertEqual(next_player["discord_id"], "2")

    def test_eliminated_participant_stays_eliminated_on_rejoin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(os.path.join(tmpdir, "test.sqlite3"))
            store.init()
            raid_id = store.create_raid("Raid", "City", "1", 100, 1, Units(), 0)
            store.upsert_participant(raid_id, 1, "Alpha", Units(bulls=1))
            with store.connect() as conn:
                conn.execute("UPDATE raid_participants SET status = 'ELIMINATED' WHERE raid_id = ? AND discord_id = ?", (raid_id, "1"))
            store.upsert_participant(raid_id, 1, "Alpha", Units(bulls=2))
            participant = store.get_participant(raid_id, 1)
            self.assertEqual(participant["status"], "ELIMINATED")


if __name__ == "__main__":
    unittest.main()
