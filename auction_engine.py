"""Synchronous auction rules: no network calls between validation and mutation."""

from __future__ import annotations

import time
import unicodedata
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

STARTING_BUDGET = 1_000_000_000
MAX_SQUAD = 15


def player_key(player):
    name = player["name"] if isinstance(player, dict) else str(player)
    folded = unicodedata.normalize("NFKD", name.casefold())
    return "".join(char for char in folded if char.isalnum())


def bid_increment(price):
    if price < 10_000_000:
        return 500_000
    if price < 30_000_000:
        return 1_000_000
    return 2_000_000


@dataclass
class AuctionRoom:
    host: str
    participants: set[str]
    timer: int = 30
    set_key: str | None = None
    current_player: dict | None = None
    current_price: int = 0
    highest_bidder: str | None = None
    lot_id: str | None = None
    deadline: float = 0
    passed: set[str] = field(default_factory=set)
    sold: set[str] = field(default_factory=set)
    unsold: list[dict] = field(default_factory=list)
    last_sale: dict | None = None
    last_activity: float = field(default_factory=time.monotonic)

    def start_lot(self, player):
        if self.current_player is not None:
            raise ValueError("Finish the current player before opening another lot.")
        if player_key(player) in self.sold:
            raise ValueError("This player has already been sold in this auction.")
        self.current_player = deepcopy(player)
        self.current_price = player["base_price"]
        self.highest_bidder = None
        self.lot_id = uuid.uuid4().hex
        self.passed.clear()
        self.deadline = time.monotonic() + self.timer
        self.last_activity = time.monotonic()

    def minimum_bid(self):
        return self.current_price + (
            bid_increment(self.current_price) if self.highest_bidder else 0
        )

    def bid(self, uid, amount, budgets, teams, *, lot_id=None):
        uid = str(uid)
        if not self.current_player or (lot_id is not None and lot_id != self.lot_id):
            raise ValueError(
                "This auction card has expired. Use !status for the current player."
            )
        if uid not in self.participants:
            raise ValueError("Join this auction before bidding.")
        if uid == self.highest_bidder:
            raise ValueError("You already hold the highest bid.")
        if len(teams.get(uid, [])) >= MAX_SQUAD:
            raise ValueError(f"Your squad already has {MAX_SQUAD} players.")
        if any(
            player_key(p) == player_key(self.current_player) for p in teams.get(uid, [])
        ):
            raise ValueError("You already own this player.")
        minimum = self.minimum_bid()
        amount = minimum if amount is None else amount
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < minimum:
            raise ValueError(f"The next bid must be at least ${minimum:,}.")
        if amount > budgets.get(uid, STARTING_BUDGET):
            raise ValueError("That bid exceeds your remaining budget.")
        self.current_price = amount
        self.highest_bidder = uid
        self.passed.discard(uid)
        self.deadline = time.monotonic() + self.timer
        self.last_activity = time.monotonic()
        return amount

    def pass_player(self, uid, *, lot_id=None):
        uid = str(uid)
        if not self.current_player or (lot_id is not None and lot_id != self.lot_id):
            raise ValueError("This auction card has expired.")
        if uid not in self.participants:
            raise ValueError("Only auction participants can pass.")
        if uid == self.highest_bidder:
            raise ValueError("Your leading bid is binding; wait for the sale.")
        self.passed.add(uid)
        contenders = self.participants - self.passed
        return not contenders or contenders == {self.highest_bidder}

    def close_lot(self):
        self.current_player = None
        self.current_price = 0
        self.highest_bidder = None
        self.lot_id = None
        self.passed.clear()
