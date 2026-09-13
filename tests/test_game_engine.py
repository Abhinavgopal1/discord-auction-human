import copy
import unittest

from game_engine import (
    FORMATIONS,
    auto_lineup,
    formation_slots,
    normalize_player,
    normalize_position,
    parse_currency,
    price_for_rating,
    simulate_game,
    team_strength,
    validate_lineup,
)


def squad(rating=80, prefix="Player"):
    return [
        {"name": f"{prefix} {i}", "position": pos, "rating": rating}
        for i, pos in enumerate(formation_slots("4-3-3"))
    ]


class EngineTests(unittest.TestCase):
    def test_every_formation_has_one_goalkeeper_and_eleven_players(self):
        for name, counts in FORMATIONS.items():
            with self.subTest(name=name):
                self.assertEqual(sum(counts.values()), 11)
                self.assertEqual(counts["gk"], 1)

    def test_currency_suffixes_and_precision(self):
        for value, expected in [
            ("5m", 5_000_000),
            ("1.25M", 1_250_000),
            ("500k", 500_000),
            ("$1,234,567", 1_234_567),
            ("0.001k", 1),
            (1234, 1234),
        ]:
            self.assertEqual(parse_currency(value), expected)
        for value in [
            "nan",
            "inf",
            float("inf"),
            "1e9",
            "-5m",
            "0",
            "10,00",
            "1.5",
            True,
            "1001m",
            "9999999999999999999999999999999999999999999999999m",
            "1_000",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_currency(value)

    def test_normalization_deterministic_preserves_rating_and_game_price(self):
        card = {
            "name": "Example",
            "position": "CDM",
            "rating": 90,
            "base_price": 12_500_000,
            "paid_price": 999_000_000,
        }
        normalized = normalize_player(card)
        self.assertEqual(normalized["rating"], 90)
        self.assertEqual(normalized["base_price"], 12_500_000)
        self.assertEqual(normalized["position"], "cm")
        self.assertEqual(normalized, normalize_player(card))
        self.assertNotIn("positions", card)

    def test_invalid_price_and_rating_fall_back_to_finite_values(self):
        for price in [float("nan"), float("inf"), -1, 0, 2_000_000_000, True, "100m"]:
            card = normalize_player(
                {
                    "name": "X",
                    "position": "cf",
                    "base_price": price,
                    "rating": float("nan"),
                }
            )
            self.assertTrue(1_000_000 <= card["base_price"] <= 50_000_000)
            self.assertTrue(1 <= card["rating"] <= 99)
        self.assertEqual(normalize_position("left-wing-back"), "lb")
        self.assertLess(price_for_rating(75), price_for_rating(90))
        self.assertEqual(price_for_rating(65), 1_000_000)
        self.assertEqual(price_for_rating(70), 2_000_000)
        self.assertEqual(price_for_rating(91), 40_000_000)
        self.assertEqual(price_for_rating(94), 50_000_000)

    def test_auto_lineup_uses_best_legal_combination_not_greedy(self):
        players = squad()
        # One elite versatile CM must remain available to CAM; two other CMs fill CM slots.
        players = [p for p in players if p["position"] not in ("cm", "cam")]
        players += [
            {"name": "Versatile", "position": "cm", "positions": ["cam"], "rating": 95},
            {"name": "CM one", "position": "cm", "rating": 83},
            {"name": "CM two", "position": "cm", "rating": 82},
            {"name": "Weak CAM", "position": "cam", "rating": 50},
        ]
        original = copy.deepcopy(players)
        lineup = auto_lineup(players)
        self.assertEqual(validate_lineup(lineup["players"], "4-3-3", players), [])
        versatile = next(p for p in lineup["players"] if p["name"] == "Versatile")
        self.assertEqual(versatile["assigned_position"], "cam")
        self.assertNotIn("Weak CAM", [p["name"] for p in lineup["players"]])
        self.assertEqual(players, original)

    def test_missing_position_and_duplicate_athlete_rejected(self):
        players = squad()
        with self.assertRaisesRegex(ValueError, "GK"):
            auto_lineup([p for p in players if p["position"] != "gk"])
        players[2]["name"] = players[1]["name"]
        self.assertTrue(
            any("more than once" in e for e in validate_lineup(players, "4-3-3"))
        )
        with self.assertRaises(ValueError):
            auto_lineup(players)

    def test_unowned_or_illegal_slot_rejected(self):
        lineup = auto_lineup(squad())
        self.assertTrue(
            any(
                "not in your squad" in e
                for e in validate_lineup(lineup["players"], "4-3-3", [])
            )
        )
        lineup["players"][0]["assigned_position"] = "st"
        self.assertTrue(
            any("cannot play" in e for e in validate_lineup(lineup["players"], "4-3-3"))
        )

    def test_simulation_seed_price_independence_and_goal_events(self):
        first, second = auto_lineup(squad(prefix="A")), auto_lineup(squad(prefix="B"))
        result = simulate_game(first, second, seed=123)
        expensive = copy.deepcopy(first)
        for player in expensive["players"]:
            player.update(
                price=1_000_000_000, paid_price=1_000_000_000, base_price=50_000_000
            )
        self.assertEqual(simulate_game(expensive, second, seed=123), result)
        self.assertEqual(team_strength(first), 80)
        self.assertEqual(result["expected_goals"], [1.3, 1.3])
        self.assertEqual(len(result["events"]), sum(result["goals"]))
        for team in (0, 1):
            self.assertEqual(
                sum(e["team"] == team for e in result["events"]), result["goals"][team]
            )

    def test_rating_advantage_and_tactic_tradeoff(self):
        weak = auto_lineup(squad(70))
        strong = auto_lineup(squad(90, "Strong"))
        result = simulate_game(strong, weak, seed=4)
        self.assertGreater(result["expected_goals"][0], result["expected_goals"][1])
        strong["tactic"] = "Attacking"
        attack = simulate_game(strong, weak, seed=4)
        self.assertGreater(attack["expected_goals"][0], result["expected_goals"][0])
        self.assertGreater(attack["expected_goals"][1], result["expected_goals"][1])
        # Averaged over fixed seeds, ratings matter without determining every result.
        wins = sum(
            simulate_game(strong, weak, seed=s)["winner"] == 0 for s in range(200)
        )
        self.assertGreater(wins, 130)
        self.assertLess(wins, 200)


if __name__ == "__main__":
    unittest.main()
