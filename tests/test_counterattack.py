import random
import unittest

from arcadion_bot.game import Units, apply_damage_to_units


class CounterattackTests(unittest.TestCase):
    def test_counterattack_prefers_weighted_available_units(self) -> None:
        random.seed(1)
        result = apply_damage_to_units(Units(bulls=1, rhinos=1), 1500)
        self.assertEqual(result.destroyed.bulls, 1)
        self.assertEqual(result.destroyed.rhinos, 0)


if __name__ == "__main__":
    unittest.main()
