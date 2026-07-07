import os
import tempfile
import unittest

from arcadion_bot.game import Units
from arcadion_bot.storage import Store


class PermanentArmyUpdateTests(unittest.TestCase):
    def test_add_units_updates_existing_army_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(os.path.join(tmpdir, "test.sqlite3"))
            store.init()
            store.upsert_player(1, "Alpha", Units(bulls=20, rhinos=5, lieutenants=2, generals=1, mechas=1))

            updated = store.add_player_units(1, Units(bulls=5, rhinos=2, lieutenants=1))

            self.assertTrue(updated)
            player = store.get_player(1)
            self.assertEqual(player["bulls"], 25)
            self.assertEqual(player["rhinos"], 7)
            self.assertEqual(player["lieutenants"], 3)
            self.assertEqual(player["generals"], 1)
            self.assertEqual(player["mechas"], 1)

    def test_remove_units_rejects_insufficient_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(os.path.join(tmpdir, "test.sqlite3"))
            store.init()
            store.upsert_player(1, "Alpha", Units(bulls=2, rhinos=1))

            removed = store.remove_player_units(1, Units(bulls=3, rhinos=1))

            self.assertFalse(removed)
            player = store.get_player(1)
            self.assertEqual(player["bulls"], 2)
            self.assertEqual(player["rhinos"], 1)


if __name__ == "__main__":
    unittest.main()
