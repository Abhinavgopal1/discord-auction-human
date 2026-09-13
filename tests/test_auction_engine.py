import unittest

from auction_engine import STARTING_BUDGET, AuctionRoom, bid_increment


def card(name="Player", price=10_000_000):
    return {
        "name": name,
        "position": "st",
        "base_price": price,
        "rating": 80,
        "tier": "B",
    }


class AuctionRuleTests(unittest.TestCase):
    def setUp(self):
        self.room = AuctionRoom("1", {"1", "2"}, timer=30)
        self.room.start_lot(card())
        self.budgets = {"1": STARTING_BUDGET, "2": STARTING_BUDGET}
        self.teams = {"1": [], "2": []}

    def test_first_bid_is_opening_price_then_uses_increment(self):
        self.assertEqual(self.room.bid("1", None, self.budgets, self.teams), 10_000_000)
        self.assertEqual(
            self.room.minimum_bid(), 10_000_000 + bid_increment(10_000_000)
        )
        self.assertEqual(
            self.room.bid("2", 11_000_000, self.budgets, self.teams), 11_000_000
        )

    def test_stale_lot_token_cannot_change_new_lot(self):
        old_token = self.room.lot_id
        self.room.close_lot()
        self.room.start_lot(card("New Player"))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.room.bid("1", None, self.budgets, self.teams, lot_id=old_token)

    def test_passes_finish_only_when_no_contender_remains(self):
        self.assertFalse(self.room.pass_player("1"))
        self.assertTrue(self.room.pass_player("2"))
        self.assertEqual(self.room.passed, {"1", "2"})

    def test_leading_bidder_cannot_pass_or_exceed_wallet(self):
        self.room.bid("1", None, self.budgets, self.teams)
        with self.assertRaisesRegex(ValueError, "binding"):
            self.room.pass_player("1")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.room.bid("2", STARTING_BUDGET + 1, self.budgets, self.teams)


if __name__ == "__main__":
    unittest.main()
