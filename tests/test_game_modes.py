"""Regression coverage for new game rules, ownership and Discord controls."""

import asyncio
import copy
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from game_engine import FORMATIONS, simulate_game, team_strength, validate_lineup
from game_modes import (
    Challenge,
    ChallengeView,
    GameModes,
    InvitationView,
    KickView,
    League,
    LeagueView,
    Shootout,
    TacticView,
    quick_lineups,
    round_robin,
    saved_lineup,
    setup_game_modes,
)


def lineup(prefix="A", rating=80):
    return {
        "formation": "4-3-3",
        "tactic": "Balanced",
        "players": [
            {
                "name": f"{prefix} {position} {index}",
                "position": position,
                "rating": rating,
            }
            for position, count in FORMATIONS["4-3-3"].items()
            for index in range(count)
        ],
    }


def runtime():
    lineups = {uid: {"main": lineup(uid)} for uid in ("1", "2", "3", "4")}
    pool = {
        pos: {
            "A": [
                {"name": f"Draft {pos} {i}", "position": pos, "rating": 70 + i}
                for i in range(8)
            ],
            "B": [],
            "C": [],
        }
        for pos in FORMATIONS["4-3-3"]
    }
    return SimpleNamespace(
        user_lineups=lineups,
        active_lineups={},
        user_teams={
            uid: copy.deepcopy(data["main"]["players"]) for uid, data in lineups.items()
        },
        user_budgets={"1": 1234, "2": 5678},
        user_stats={"1": {"wins": 5}},
        available_sets={"26-27": "2026/27"},
        load_players_by_position=lambda pos, season: pool[pos],
        pool=pool,
    )


def interaction(uid):
    return SimpleNamespace(
        user=SimpleNamespace(id=int(uid), display_name=f"Manager {uid}"),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=Mock(return_value=False),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class ScheduleTests(unittest.TestCase):
    def test_every_pair_plays_once_without_double_booking(self):
        for count in range(2, 9):
            with self.subTest(count=count):
                participants = [str(i) for i in range(count)]
                rounds = round_robin(participants)
                all_pairs = []
                for games in rounds:
                    players = [player for game in games for player in game]
                    self.assertEqual(len(players), len(set(players)))
                    all_pairs.extend(frozenset(game) for game in games)
                self.assertEqual(len(all_pairs), count * (count - 1) // 2)
                self.assertEqual(len(all_pairs), len(set(all_pairs)))

    def test_invalid_schedules_rejected(self):
        for players in ([], ["1"], ["1", "1"], list(map(str, range(9)))):
            with self.assertRaises(ValueError):
                round_robin(players)


class QuickMatchTests(unittest.TestCase):
    def test_free_draft_has_equal_role_strength_and_no_shared_players(self):
        state = runtime()
        before = copy.deepcopy(
            (state.pool, state.user_teams, state.user_lineups, state.user_budgets)
        )
        teams = quick_lineups(state, "26-27", random.Random(42))
        self.assertEqual([len(team["players"]) for team in teams], [11, 11])
        self.assertEqual(len({p["name"] for t in teams for p in t["players"]}), 22)
        self.assertEqual(validate_lineup(teams[0]["players"], "4-3-3"), [])
        self.assertEqual(team_strength(teams[0]), team_strength(teams[1]))
        result = simulate_game(*teams, seed=4)
        self.assertEqual(result["expected_goals"][0], result["expected_goals"][1])
        self.assertEqual(
            before,
            (state.pool, state.user_teams, state.user_lineups, state.user_budgets),
        )

    def test_insufficient_pool_fails_before_starting(self):
        state = runtime()
        state.pool["gk"]["A"] = state.pool["gk"]["A"][:1]
        with self.assertRaisesRegex(ValueError, "GK"):
            quick_lineups(state, "26-27")

    def test_saved_lineup_uses_active_slot_and_is_a_snapshot(self):
        state = runtime()
        state.user_lineups["1"]["cup"] = copy.deepcopy(state.user_lineups["1"]["main"])
        state.user_lineups["1"]["cup"]["tactic"] = "Defensive"
        state.active_lineups["1"] = "cup"
        snapshot = saved_lineup(state, "1")
        self.assertEqual(snapshot["tactic"], "Defensive")
        state.user_lineups["1"]["cup"]["players"][0]["rating"] = 99
        self.assertEqual(snapshot["players"][0]["rating"], 80)

    def test_saved_lineup_rejects_sold_cards_and_missing_xi(self):
        state = runtime()
        state.user_teams["1"].pop()
        with self.assertRaisesRegex(ValueError, "not in your squad"):
            saved_lineup(state, "1")
        with self.assertRaisesRegex(ValueError, "starting XI"):
            saved_lineup(state, "999")


class PenaltyTests(unittest.TestCase):
    def kick(self, game, shot="left", dive="right"):
        turn = len(game.kicks)
        shooter = game.players[game.kicker]
        keeper = game.players[1 - game.kicker]
        self.assertFalse(game.choose(shooter, shot, turn))
        self.assertTrue(game.choose(keeper, dive, turn))

    def test_secret_choices_commit_once_and_need_both_people(self):
        game = Shootout(("1", "2"))
        with self.assertRaises(ValueError):
            game.choose("3", "left", 0)
        self.assertFalse(game.choose("1", "left", 0))
        self.assertEqual(game.scores, [0, 0])
        self.assertEqual(game.kicks, [])
        with self.assertRaisesRegex(ValueError, "locked"):
            game.choose("1", "right", 0)
        self.assertTrue(game.choose("2", "left", 0))
        self.assertEqual(game.scores, [0, 0])
        self.assertEqual(game.taken, [1, 0])
        with self.assertRaisesRegex(ValueError, "already finished"):
            game.choose("1", "left", 0)

    def test_mathematically_decided_shootout_ends_early(self):
        game = Shootout(("1", "2"))
        for _ in range(3):
            self.kick(game)
            self.kick(game, "left", "left")
        self.assertTrue(game.finished)
        self.assertEqual(game.winner, 0)
        self.assertEqual(game.scores, [3, 0])
        self.assertEqual(len(game.kicks), 6)
        with self.assertRaises(ValueError):
            game.choose("1", "left", 6)

    def test_sudden_death_waits_for_equal_attempts(self):
        game = Shootout(("1", "2"))
        for _ in range(10):
            self.kick(game)
        self.assertFalse(game.finished)
        self.kick(game)
        self.assertFalse(game.finished)
        self.kick(game, "centre", "centre")
        self.assertTrue(game.finished)
        self.assertEqual(game.winner, 0)

    def test_tied_sudden_death_is_bounded(self):
        game = Shootout(("1", "2"))
        for _ in range(20):
            self.kick(game)
        self.assertTrue(game.finished)
        self.assertIsNone(game.winner)
        self.assertEqual(game.taken, [10, 10])


class LeagueTests(unittest.TestCase):
    def league(self, count=3):
        league = League("1", capacity=count)
        for uid in map(str, range(1, count + 1)):
            league.join(uid, f"Manager {uid}")
        return league

    def start(self, league):
        league.start("1", {uid: lineup(uid) for uid in league.members})

    def test_host_controls_capacity_and_locked_roster(self):
        league = self.league(2)
        with self.assertRaisesRegex(ValueError, "full"):
            league.join("3", "Manager 3")
        with self.assertRaisesRegex(ValueError, "already"):
            league.join("2", "Manager 2")
        with self.assertRaisesRegex(ValueError, "host"):
            league.leave("1")
        with self.assertRaisesRegex(ValueError, "host"):
            league.start("2", {})
        self.start(league)
        with self.assertRaises(ValueError):
            league.leave("2")
        with self.assertRaises(ValueError):
            league.join("3", "Manager 3")
        with self.assertRaises(ValueError):
            self.start(league)
        with self.assertRaisesRegex(ValueError, "host"):
            league.next_round("2")

    def test_lineups_are_frozen_at_start(self):
        league = self.league(2)
        snapshots = {uid: lineup(uid) for uid in league.members}
        league.start("1", snapshots)
        snapshots["1"]["players"][0]["rating"] = 99
        self.assertEqual(league.lineups["1"]["players"][0]["rating"], 80)

    def test_one_fixture_cannot_inflate_standings(self):
        league = self.league(2)
        self.start(league)
        home, away = league.rounds[0][0]
        self.assertTrue(league.record(home, away, (2, 1)))
        self.assertFalse(league.record(home, away, (9, 0)))
        table = {row["user_id"]: row for row in league.table()}
        self.assertEqual(table[home]["points"], 3)
        self.assertEqual(table[home]["gf"], 2)
        self.assertEqual(table[away]["ga"], 2)
        simulator = Mock()
        league.next_round("1", simulator)
        simulator.assert_not_called()
        self.assertEqual(league.phase, "finished")
        with self.assertRaises(ValueError):
            league.next_round("1")

    def test_table_totals_and_fixture_count_for_complete_league(self):
        league = self.league(5)
        self.start(league)
        while league.phase == "playing":
            league.next_round("1", lambda *args: {"goals": [1, 1]})
        self.assertEqual(len(league.results), 10)
        for row in league.table():
            self.assertEqual(
                (row["played"], row["drawn"], row["points"], row["gf"], row["ga"]),
                (4, 4, 4, 4, 4),
            )

    def test_partial_round_failure_does_not_replay_completed_fixture(self):
        league = self.league(4)
        self.start(league)
        simulator = Mock(
            side_effect=[{"goals": [1, 0]}, RuntimeError("engine unavailable")]
        )
        with self.assertRaises(RuntimeError):
            league.next_round("1", simulator)
        self.assertEqual(len(league.results), 1)
        self.assertEqual(league.round_index, 0)
        retry = Mock(return_value={"goals": [0, 0]})
        league.next_round("1", retry)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(len(league.results), 2)

    def test_out_of_round_and_invalid_score_rejected(self):
        league = self.league(3)
        self.start(league)
        future = league.rounds[1][0]
        with self.assertRaises(ValueError):
            league.record(*future, (1, 0))
        current = league.rounds[0][0]
        for score in ((-1, 0), (True, 0), (1.5, 0), (1,)):
            with self.assertRaises(ValueError):
                league.record(*current, score)


class DiscordControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = runtime()
        self.manager = GameModes(SimpleNamespace(dispatch=Mock()), self.state)
        self.views = []

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        for challenge in list(self.manager.challenges.values()):
            self.manager.close(challenge)
        for league in self.manager.leagues.values():
            if league.view:
                league.view.stop()

    def challenge(self, kind="quick"):
        c = Challenge(kind, (1, 1), ("1", "2"), ("One", "Two"), "26-27")
        c.message = SimpleNamespace(edit=AsyncMock())
        c.lineups = quick_lineups(self.state, "26-27", random.Random(4))
        self.manager.challenges[c.scope] = c
        self.manager.busy = {uid: c for uid in c.players}
        return c

    def attach(self, c, view_type):
        view = view_type(self.manager, c)
        c.view = view
        self.views.append(view)
        return view

    async def test_only_invited_manager_can_accept(self):
        c = self.challenge()
        view = self.attach(c, InvitationView)
        await view.accept.callback(interaction("1"))
        self.assertEqual(c.phase, "invited")
        self.assertFalse(await view.interaction_check(interaction("3")))
        await view.accept.callback(interaction("2"))
        self.assertEqual(c.phase, "playing")
        self.assertIsInstance(c.view, TacticView)
        self.views.append(c.view)

    async def test_outsider_cannot_decline_or_cancel(self):
        c = self.challenge()
        view = self.attach(c, InvitationView)
        await view.decline.callback(interaction("3"))
        self.assertTrue(self.manager.is_active(c))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=1),
            author=SimpleNamespace(id=3),
        )
        with self.assertRaises(commands.BadArgument):
            await self.manager.cancel(ctx)

    async def test_expiry_releases_both_players_and_channel(self):
        c = self.challenge()
        view = self.attach(c, InvitationView)
        await view.on_timeout()
        self.assertEqual(self.manager.challenges, {})
        self.assertEqual(self.manager.busy, {})
        self.assertIsNone(c.message.edit.call_args.kwargs["view"])

    async def test_old_timeout_does_not_delete_replacement_game(self):
        c = self.challenge()
        view = self.attach(c, InvitationView)
        self.manager.close(c)
        new = self.challenge()
        self.attach(new, InvitationView)
        await view.on_timeout()
        self.assertTrue(self.manager.is_active(new))
        self.assertIs(self.manager.busy["1"], new)

    async def test_hard_timeout_ends_session(self):
        c = self.challenge()
        view = ChallengeView(self.manager, c, 0.01)
        c.view = view
        self.views.append(view)
        await asyncio.sleep(0.03)
        self.assertFalse(self.manager.is_active(c))

    async def test_concurrent_tactics_simulate_once_and_keep_final_screen(self):
        c = self.challenge()
        c.phase = "playing"
        view = self.attach(c, TacticView)
        before = copy.deepcopy(
            (
                self.state.user_teams,
                self.state.user_lineups,
                self.state.user_budgets,
                self.state.user_stats,
            )
        )
        first, second = interaction("1"), interaction("2")

        async def delayed_ack(*args, **kwargs):
            await asyncio.sleep(0.01)

        first.response.send_message.side_effect = delayed_ack
        with patch(
            "game_modes.simulate_game",
            return_value={"goals": [2, 1], "expected_goals": [1.4, 1.1]},
        ) as simulator:
            await asyncio.gather(
                view.choose(first, "Attacking"), view.choose(second, "Defensive")
            )
            await view.choose(second, "Balanced")
            self.assertEqual(simulator.call_count, 1)
        self.assertEqual(c.phase, "finished")
        self.assertFalse(self.manager.is_active(c))
        self.assertIsNone(c.view)
        self.assertIn("full time", c.message.edit.call_args.kwargs["embed"].title)
        self.assertEqual(
            before,
            (
                self.state.user_teams,
                self.state.user_lineups,
                self.state.user_budgets,
                self.state.user_stats,
            ),
        )

    async def test_penalty_buttons_authorize_owner_and_reject_stale_turn(self):
        c = self.challenge("penalties")
        c.shootout = Shootout(c.players)
        view = self.attach(c, KickView)
        await view.choose(interaction("3"), "left")
        self.assertEqual(c.shootout.choices, {})
        await view.choose(interaction("1"), "left")
        await view.choose(interaction("2"), "right")
        self.views.append(c.view)
        self.assertEqual(len(c.shootout.kicks), 1)
        await view.choose(interaction("1"), "left")
        self.assertEqual(c.shootout.choices, {})

    async def test_league_host_actions_and_stale_buttons(self):
        league = League("1", members={"1": "One", "2": "Two"})
        self.manager.leagues[(1, 1)] = league
        view = LeagueView(self.manager, (1, 1), league)
        league.view = view
        league.message = SimpleNamespace(edit=AsyncMock())
        self.views.append(view)
        for action in ("start", "end", "next"):
            await view.act(interaction("2"), action)
            self.assertEqual(league.phase, "lobby")
        await view.act(interaction("1"), "start")
        self.views.append(league.view)
        self.assertEqual(league.phase, "playing")
        await view.act(interaction("1"), "next")
        self.assertEqual(league.round_index, 0)
        await league.view.act(interaction("1"), "next")
        self.views.append(league.view)
        self.assertEqual(league.phase, "finished")
        self.assertEqual(len(league.results), 1)

    async def test_league_rechecks_owned_lineup_when_starting(self):
        league = League("1", members={"1": "One", "2": "Two"})
        self.manager.leagues[(1, 1)] = league
        self.state.user_teams["2"].pop()
        with self.assertRaisesRegex(ValueError, "Two"):
            self.manager.league_action((1, 1), SimpleNamespace(id=1), "start")
        self.assertEqual(league.phase, "lobby")

    async def test_registration_exposes_all_modes(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        setup_game_modes(bot, self.state)
        for name in (
            "quickmatch",
            "qm",
            "penalties",
            "shootout",
            "cancelgame",
            "league create",
            "league join",
            "league start",
            "league next",
            "league standings",
            "league end",
        ):
            self.assertIsNotNone(bot.get_command(name), name)
        await bot.close()


if __name__ == "__main__":
    unittest.main()
