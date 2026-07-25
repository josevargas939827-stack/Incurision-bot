import random
import unittest

from arcadion_bot.bot import counterattack_summary
from arcadion_bot.game import Units, apply_damage_to_units, roll_arcadion_counterattack_dice


class CounterattackTests(unittest.TestCase):
    def test_counterattack_prefers_weighted_available_units(self) -> None:
        random.seed(1)
        result = apply_damage_to_units(Units(bulls=1, rhinos=1), 1500)
        self.assertEqual(result.destroyed.bulls, 1)
        self.assertEqual(result.destroyed.rhinos, 0)

    def test_arcadion_counterattack_uses_three_dice(self) -> None:
        random.seed(1)
        roll = roll_arcadion_counterattack_dice()
        self.assertEqual(len(roll.rolls), 3)
        self.assertEqual(roll.text, "2 - 5 - 6")

    def test_counterattack_summary_includes_arcadion_dice(self) -> None:
        summary = counterattack_summary("Test Commander", 18000, Units(), Units())
        self.assertIn("Dice: 2 - 5 - 6", summary)


if __name__ == "__main__":
    unittest.main()
