import copy
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands

from classic_modes import DraftSession, free_lineup, knockout, setup_classic_modes
from game_engine import auto_lineup, formation_slots, validate_lineup


def pool(n=20):
    return [
        {
            "name": f"{pos} {i}",
            "position": pos,
            "rating": 75 + i % 15,
            "positions": [pos],
            "base_price": 20_000_000,
        }
        for pos in set(formation_slots("4-3-3"))
        for i in range(n)
    ]


class DraftTests(unittest.TestCase):
    def test_snake_draft_builds_unique_legal_xis_and_checks_turns(self):
        session = DraftSession("1", "26-27", ["1", "2", "3"])
        session.begin(pool(), random.Random(4))
        order = list(session.entrants)
        turns = []
        while session.current() is not None:
            turns.append(session.current())
            session.offer(random.Random(session.turn))
            version = session.version
            with self.assertRaisesRegex(ValueError, "another manager"):
                session.pick("999", 0)
            with self.assertRaisesRegex(ValueError, "expired"):
                session.pick(session.current(), 0, version - 1)
            session.pick(session.current(), 0, version)
        self.assertEqual(turns[:3], order)
        self.assertEqual(turns[3:6], list(reversed(order)))
        names = [card["name"] for team in session.teams.values() for card in team]
        self.assertEqual(len(names), len(set(names)))
        for team in session.teams.values():
            self.assertEqual(validate_lineup(team, "4-3-3"), [])

    def test_short_pool_rejected_before_session_starts(self):
        session = DraftSession("1", "26-27", ["1", "2", "3"])
        with self.assertRaisesRegex(ValueError, "unique CB"):
            session.begin(pool(3), random.Random(1))
        self.assertFalse(session.started)

    def test_knockout_with_odd_number_has_byes_and_champion(self):
        teams = {str(i): free_lineup(pool(), random.Random(i)) for i in range(5)}
        champion, rounds = knockout(teams, random.Random(3))
        self.assertIn(champion, teams)
        matches = [match for round_ in rounds for match in round_ if "bye" not in match]
        self.assertEqual(len(matches), 4)
        self.assertTrue(any("bye" in match for round_ in rounds for match in round_))

    def test_free_lineups_dont_mutate_pool(self):
        cards = pool()
        before = copy.deepcopy(cards)
        first = free_lineup(cards, random.Random(1))
        second = free_lineup(cards, random.Random(2))
        self.assertEqual(validate_lineup(first["players"], "4-3-3"), [])
        self.assertEqual(validate_lineup(second["players"], "4-3-3"), [])
        first["players"][0]["rating"] = 99
        self.assertEqual(cards, before)


class Context:
    def __init__(self, user_id="1", channel_id=77):
        self.author = SimpleNamespace(
            id=int(user_id), guild_permissions=SimpleNamespace(manage_guild=False)
        )
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = SimpleNamespace(
            id=123, get_member=lambda uid: SimpleNamespace(id=uid)
        )
        self.messages = []

    async def send(self, message=None, **kwargs):
        self.messages.append((message, kwargs))
        return SimpleNamespace()


class ClassicCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cards = pool()
        lineup = auto_lineup(cards)
        self.runtime = SimpleNamespace(
            available_sets={"26-27": "2026/27"},
            available_positions=set(formation_slots("4-3-3")),
            load_players_by_position=lambda pos, key: {
                "A": [p for p in cards if p["position"] == pos]
            },
            user_teams={
                "1": copy.deepcopy(lineup["players"]),
                "2": copy.deepcopy(lineup["players"]),
            },
            user_lineups={"1": {"main": lineup}, "2": {"main": lineup}},
            active_lineups={},
            user_stats={},
            draft_clash_wins={},
            koth_state={},
            save_data=lambda: True,
        )
        self.sessions = setup_classic_modes(self.bot, self.runtime)

    async def asyncTearDown(self):
        for session in self.sessions["draft_sessions"].values():
            if session.task:
                session.task.cancel()
            if session.view:
                session.view.stop()
        await self.bot.close()

    async def call(self, name, ctx, *args, **kwargs):
        await self.bot.get_command(name).callback(ctx, *args, **kwargs)

    async def test_draft_start_join_begin_pick_cancel_authorization(self):
        host, other = Context(), Context("2")
        await self.call("draftclash", host, "start")
        session = self.sessions["draft_sessions"][77]
        await self.call("draftclash", other, "join")
        await self.call("draftclash", other, "begin")
        self.assertFalse(session.started)
        await self.call("draftclash", host, "begin")
        self.assertTrue(session.started)
        manager = Context(session.current())
        await self.call("draftclash", manager, "pick", argument="1")
        self.assertEqual(session.turn, 1)
        await self.call("draftclash", other, "cancel")
        self.assertIn(77, self.sessions["draft_sessions"])
        await self.call("draftclash", host, "cancel")
        self.assertNotIn(77, self.sessions["draft_sessions"])

    async def test_koth_draw_keeps_crown_win_moves_it_and_owned_squads_unchanged(self):
        host, challenger = Context(), Context("2")
        original = copy.deepcopy(self.runtime.user_teams)
        await self.call("koth", host, "start", "auction")
        await self.call("koth", host, "join")
        await self.call("koth", challenger, "join")
        hill = self.sessions["hill_sessions"][77]
        self.assertEqual(hill["king"], "1")
        with patch(
            "classic_modes.simulate_game",
            return_value={
                "winner": None,
                "goals": [1, 1],
                "expected_goals": [1.3, 1.3],
            },
        ):
            await self.call("challenge", challenger)
        self.assertEqual(hill["king"], "1")
        self.assertEqual(
            self.runtime.koth_state["leaderboards"]["123"]["auction"]["2"]["draws"], 1
        )
        hill["last_challenge"].clear()
        with patch(
            "classic_modes.simulate_game",
            return_value={"winner": 0, "goals": [2, 1], "expected_goals": [1.3, 1.3]},
        ):
            await self.call("challenge", challenger)
        self.assertEqual(hill["king"], "2")
        self.assertEqual(hill["streak"], 1)
        self.assertEqual(self.runtime.user_teams, original)
        self.assertNotIn("teams", self.runtime.koth_state)

    async def test_koth_channel_isolation_and_unowned_lineup_rejected(self):
        host = Context()
        await self.call("koth", host, "start", "auction")
        self.runtime.user_teams["1"] = []
        await self.call("koth", host, "join")
        self.assertEqual(self.sessions["hill_sessions"][77]["teams"], {})
        elsewhere = Context(channel_id=88)
        await self.call("koth", elsewhere, "start", "draft")
        await self.call("koth", elsewhere, "join")
        self.assertEqual(self.sessions["hill_sessions"][88]["king"], "1")
        self.assertIsNone(self.sessions["hill_sessions"][77]["king"])


if __name__ == "__main__":
    unittest.main()
