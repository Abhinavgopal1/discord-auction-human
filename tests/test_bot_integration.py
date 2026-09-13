import copy
import unittest
from unittest.mock import patch

import bot
from auction_engine import AuctionRoom


class BotIntegrationTests(unittest.TestCase):
    def test_all_user_facing_command_groups_are_registered(self):
        commands = {command.name for command in bot.bot.commands}
        self.assertTrue(
            {
                "startauction",
                "setlineup",
                "market",
                "battle",
                "quickmatch",
                "penalties",
                "draftclash",
                "koth",
                "league",
            }
            <= commands
        )

    def test_current_season_loader_is_complete_and_deduplicated(self):
        for position in bot.available_positions:
            players = [
                player
                for group in bot.load_players_by_position(position, "26-27").values()
                for player in group
            ]
            self.assertGreaterEqual(len(players), 20)
            self.assertEqual(
                len({player["name"].casefold() for player in players}), len(players)
            )

    def test_sale_rolls_back_wallet_squad_and_stats_when_save_fails(self):
        original = (bot.user_teams, bot.user_budgets, bot.user_stats)
        try:
            bot.user_teams, bot.user_budgets, bot.user_stats = (
                {"1": []},
                {"1": 100_000_000},
                {"1": {"money_spent": 0, "most_expensive": 0}},
            )
            room = AuctionRoom("1", {"1", "2"})
            room.start_lot(
                {
                    "name": "Test striker",
                    "position": "st",
                    "base_price": 10_000_000,
                    "rating": 80,
                    "tier": "C",
                }
            )
            room.bid("1", None, bot.user_budgets, bot.user_teams)
            before = copy.deepcopy((bot.user_teams, bot.user_budgets, bot.user_stats))
            with patch.object(bot, "save_data", return_value=False):
                with self.assertRaisesRegex(ValueError, "save failed"):
                    bot.commit_sale(room)
            self.assertEqual((bot.user_teams, bot.user_budgets, bot.user_stats), before)
            self.assertIsNotNone(room.current_player)
        finally:
            bot.user_teams, bot.user_budgets, bot.user_stats = original

    def test_successful_sale_commits_once_and_closes_the_lot(self):
        original = (bot.user_teams, bot.user_budgets, bot.user_stats)
        try:
            bot.user_teams, bot.user_budgets, bot.user_stats = (
                {"1": []},
                {"1": 100_000_000},
                {"1": {"money_spent": 0, "most_expensive": 0}},
            )
            room = AuctionRoom("1", {"1", "2"})
            room.start_lot(
                {
                    "name": "Test midfielder",
                    "position": "cm",
                    "base_price": 10_000_000,
                    "rating": 80,
                    "tier": "C",
                }
            )
            room.bid("1", None, bot.user_budgets, bot.user_teams)
            with patch.object(bot, "save_data", return_value=True) as save:
                result = bot.commit_sale(room)
            self.assertEqual(result["winner"], "1")
            self.assertIsNone(room.current_player)
            self.assertEqual(bot.user_budgets["1"], 90_000_000)
            self.assertEqual(bot.user_teams["1"][0]["name"], "Test midfielder")
            save.assert_called_once()
        finally:
            bot.user_teams, bot.user_budgets, bot.user_stats = original


if __name__ == "__main__":
    unittest.main()
