import unittest

from arcadion_bot.bot import loot_summary_embed


class RewardSystemTests(unittest.TestCase):
    def test_success_embed_shows_reward_distribution(self) -> None:
        raid = {"total_loot_upx": 1000, "result": "SUCCESS"}
        participants = [
            {"discord_name": "Alice", "damage_done": 100, "attacks": 2, "status": "ACTIVE", "lost_bulls": 0, "lost_rhinos": 0, "lost_lieutenants": 0, "lost_generals": 0, "lost_mechas": 0},
            {"discord_name": "Bob", "damage_done": 200, "attacks": 1, "status": "ACTIVE", "lost_bulls": 1, "lost_rhinos": 0, "lost_lieutenants": 0, "lost_generals": 0, "lost_mechas": 0},
        ]

        embed = loot_summary_embed(raid, participants, "Arcadion has been defeated.")
        values = "\n".join(field.value for field in embed.fields)
        self.assertIn("SUCCESS", values)
        self.assertIn("Reward", values)
        self.assertIn("UPX", values)

    def test_failed_embed_omits_rewards_and_shows_failure_message(self) -> None:
        raid = {"total_loot_upx": 1000, "result": "FAILED"}
        participants = [
            {"discord_name": "Alice", "damage_done": 50, "attacks": 1, "status": "ELIMINATED", "lost_bulls": 2, "lost_rhinos": 0, "lost_lieutenants": 0, "lost_generals": 0, "lost_mechas": 0},
        ]

        embed = loot_summary_embed(raid, participants, "Arcadion wins. No active troops remain.")
        values = "\n".join(field.value for field in embed.fields)
        self.assertIn("FAILED", values)
        self.assertIn("No UPX rewards have been distributed", values)
        self.assertNotIn("Reward", values)


if __name__ == "__main__":
    unittest.main()
