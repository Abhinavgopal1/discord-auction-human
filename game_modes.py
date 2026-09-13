"""Consent-based, session-only games. No mode here changes wallets or squads.

Call ``setup_game_modes(bot, runtime)`` once, after the main bot's state exists.
The pure state classes are also used by the regression tests. Invitations and
turns expire; mini leagues keep their table until ended, idle, or bot restart.
"""

from __future__ import annotations

import asyncio
import copy
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands

from game_engine import (
    FORMATIONS,
    normalize_player,
    player_key,
    simulate_game,
    validate_lineup,
)

BLUE = 0x5865F2
GREEN = 0x23A55A
AMBER = 0xF0B232
INVITE_TIMEOUT = 60
TURN_TIMEOUT = 90
LEAGUE_TIMEOUT = 1800
QUICK_FORMATION = FORMATIONS["4-3-3"]
TACTICS = ("Attacking", "Balanced", "Defensive")
DIRECTIONS = ("left", "centre", "right")


def safe_name(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[:80]


def scope_for(ctx: Any) -> tuple[int, int]:
    return (ctx.guild.id if ctx.guild else 0, ctx.channel.id)


def round_robin(user_ids: list[str]) -> list[list[tuple[str, str]]]:
    """Circle schedule: every pair once, at most one game per player per round."""
    if len(user_ids) < 2 or len(user_ids) > 8 or len(set(user_ids)) != len(user_ids):
        raise ValueError("A league needs 2–8 different managers.")
    rotation: list[str | None] = list(user_ids)
    if len(rotation) % 2:
        rotation.append(None)
    rounds = []
    for number in range(len(rotation) - 1):
        games = []
        for i in range(len(rotation) // 2):
            home, away = rotation[i], rotation[-i - 1]
            if home is not None and away is not None:
                games.append((home, away) if (number + i) % 2 == 0 else (away, home))
        rounds.append(games)
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]
    return rounds


def quick_lineups(
    runtime: Any, set_name: str, rng: random.Random | None = None
) -> list[dict]:
    """Draft unique players; matching roles receive identical normalized ratings."""
    rng = rng or random.Random()
    teams: list[list[dict]] = [[], []]
    used: set[str] = set()
    for position, count in QUICK_FORMATION.items():
        raw = runtime.load_players_by_position(position, set_name)
        candidates = (
            [p for group in raw.values() for p in group]
            if isinstance(raw, dict)
            else raw
        )
        unique = {}
        for record in candidates:
            player = normalize_player(record, position=position)
            key = player_key(player)
            if key not in used:
                unique.setdefault(key, player)
        if len(unique) < count * 2:
            raise ValueError(
                f"The {set_name} set needs {count * 2} different {position.upper()} players for Quick Match."
            )
        selected = rng.sample(list(unique.values()), count * 2)
        for offset in range(count):
            pair = selected[offset * 2 : offset * 2 + 2]
            rating = round(sum(p["rating"] for p in pair) / 2, 1)
            for side, player in enumerate(pair):
                player = copy.deepcopy(player)
                player["rating"] = rating
                player["position"] = position
                player["set"] = set_name
                teams[side].append(player)
                used.add(player_key(player))
    return [
        {"players": team, "formation": "4-3-3", "tactic": "Balanced"} for team in teams
    ]


def saved_lineup(runtime: Any, user_id: str) -> dict:
    """Snapshot an explicit, valid saved XI and verify every card is still owned."""
    lineups = runtime.user_lineups.get(str(user_id), {})
    name = runtime.active_lineups.get(str(user_id), "main")
    # Accept the original single-lineup save format during migration.
    lineup = lineups if "players" in lineups else lineups.get(name)
    if not lineup or not lineup.get("players"):
        raise ValueError("Save a complete starting XI with `!setlineup` first.")
    errors = validate_lineup(
        lineup["players"],
        lineup.get("formation", "4-3-3"),
        squad=runtime.user_teams.get(str(user_id), []),
    )
    if errors:
        raise ValueError("Your saved XI needs attention: " + "; ".join(errors[:3]))
    return copy.deepcopy(lineup)


@dataclass
class Shootout:
    players: tuple[str, str]
    scores: list[int] = field(default_factory=lambda: [0, 0])
    taken: list[int] = field(default_factory=lambda: [0, 0])
    choices: dict[str, str] = field(default_factory=dict)
    kicks: list[tuple[int, str, str, bool]] = field(default_factory=list)
    finished: bool = False
    winner: int | None = None

    @property
    def kicker(self) -> int:
        return len(self.kicks) % 2

    def choose(self, user_id: str, direction: str, turn: int) -> bool:
        """Commit a secret choice; resolve only after both players have committed."""
        if self.finished or turn != len(self.kicks):
            raise ValueError("That kick has already finished.")
        if user_id not in self.players:
            raise ValueError("Only the two managers in this shootout can choose.")
        if direction not in DIRECTIONS:
            raise ValueError("Choose left, centre or right.")
        if user_id in self.choices:
            raise ValueError("Your choice is locked for this kick.")
        self.choices[user_id] = direction
        if len(self.choices) < 2:
            return False
        kicker = self.kicker
        shot = self.choices[self.players[kicker]]
        dive = self.choices[self.players[1 - kicker]]
        scored = shot != dive
        self.taken[kicker] += 1
        self.scores[kicker] += int(scored)
        self.kicks.append((kicker, shot, dive, scored))
        self.choices.clear()
        if min(self.taken) < 5:
            for side in (0, 1):
                if self.scores[side] > self.scores[1 - side] + max(
                    0, 5 - self.taken[1 - side]
                ):
                    self.finished, self.winner = True, side
        elif self.taken[0] == self.taken[1]:
            if self.scores[0] != self.scores[1]:
                self.finished = True
                self.winner = 0 if self.scores[0] > self.scores[1] else 1
            elif self.taken[0] >= 10:
                self.finished = True  # Bounded sudden death ends in a sporting draw.
        return True


@dataclass
class League:
    host_id: str
    capacity: int = 6
    members: dict[str, str] = field(default_factory=dict)
    lineups: dict[str, dict] = field(default_factory=dict)
    rounds: list[list[tuple[str, str]]] = field(default_factory=list)
    results: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    round_index: int = 0
    phase: str = "lobby"
    updated_at: float = field(default_factory=time.monotonic)
    message: Any = None
    view: Any = None
    lock: Any = field(default_factory=asyncio.Lock)

    def require_host(self, user_id: str) -> None:
        if user_id != self.host_id:
            raise ValueError("Only the league host can do that.")

    def join(self, user_id: str, name: str) -> None:
        if self.phase != "lobby":
            raise ValueError("This league has already started.")
        if user_id in self.members:
            raise ValueError("You are already in this league.")
        if len(self.members) >= self.capacity:
            raise ValueError("This league is full.")
        self.members[user_id] = name

    def leave(self, user_id: str) -> None:
        if self.phase != "lobby":
            raise ValueError("The fixture list is locked after the league starts.")
        if user_id == self.host_id:
            raise ValueError("The host can close the lobby with `!league end`.")
        if user_id not in self.members:
            raise ValueError("You are not in this league.")
        del self.members[user_id]

    def start(self, user_id: str, lineups: dict[str, dict]) -> None:
        self.require_host(user_id)
        if self.phase != "lobby":
            raise ValueError("This league has already started.")
        if len(self.members) < 2:
            raise ValueError("At least two managers need to join.")
        if set(lineups) != set(self.members):
            raise ValueError("Every manager needs a valid saved XI before starting.")
        self.lineups = copy.deepcopy(lineups)
        self.rounds = round_robin(list(self.members))
        self.phase = "playing"

    def record(self, home: str, away: str, goals: tuple[int, int]) -> bool:
        """Idempotent fixture application: a match contributes to the table once."""
        if self.phase != "playing" or (home, away) not in self.rounds[self.round_index]:
            raise ValueError("That fixture is not in the current round.")
        if (home, away) in self.results:
            return False
        if (
            any(type(score) is not int or score < 0 for score in goals)
            or len(goals) != 2
        ):
            raise ValueError("A result requires two non-negative integer scores.")
        self.results[(home, away)] = goals
        return True

    def next_round(
        self, user_id: str, simulator=simulate_game
    ) -> list[tuple[str, str, tuple[int, int]]]:
        self.require_host(user_id)
        if self.phase != "playing":
            raise ValueError(
                "Start the league first."
                if self.phase == "lobby"
                else "All league fixtures are complete."
            )
        games = []
        for home, away in self.rounds[self.round_index]:
            if (home, away) not in self.results:
                result = simulator(self.lineups[home], self.lineups[away])
                self.record(home, away, tuple(result["goals"]))
            games.append((home, away, self.results[(home, away)]))
        self.round_index += 1
        if self.round_index == len(self.rounds):
            self.phase = "finished"
        return games

    def table(self) -> list[dict]:
        rows = {
            uid: {
                "user_id": uid,
                "name": name,
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "gf": 0,
                "ga": 0,
                "points": 0,
            }
            for uid, name in self.members.items()
        }
        for (home, away), (hg, ag) in self.results.items():
            for uid, goals, conceded in ((home, hg, ag), (away, ag, hg)):
                row = rows[uid]
                row["played"] += 1
                row["gf"] += goals
                row["ga"] += conceded
                row["won"] += int(goals > conceded)
                row["drawn"] += int(goals == conceded)
                row["lost"] += int(goals < conceded)
                row["points"] += 3 if goals > conceded else int(goals == conceded)
        return sorted(
            rows.values(),
            key=lambda r: (
                -r["points"],
                -(r["gf"] - r["ga"]),
                -r["gf"],
                r["name"].casefold(),
                r["user_id"],
            ),
        )


@dataclass
class Challenge:
    kind: str
    scope: tuple[int, int]
    players: tuple[str, str]
    names: tuple[str, str]
    set_name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "invited"
    lineups: list[dict] = field(default_factory=list)
    tactics: dict[str, str] = field(default_factory=dict)
    shootout: Shootout | None = None
    message: Any = None
    view: Any = None
    lock: Any = field(default_factory=asyncio.Lock)


class GameModes:
    def __init__(self, bot: commands.Bot, runtime: Any):
        self.bot, self.runtime = bot, runtime
        self.challenges: dict[tuple[int, int], Challenge] = {}
        self.busy: dict[str, Challenge] = {}
        self.leagues: dict[tuple[int, int], League] = {}

    def is_active(self, challenge: Challenge) -> bool:
        return self.challenges.get(challenge.scope) is challenge

    def close(self, challenge: Challenge) -> None:
        if self.is_active(challenge):
            del self.challenges[challenge.scope]
        for user_id in challenge.players:
            if self.busy.get(user_id) is challenge:
                del self.busy[user_id]
        if challenge.view:
            challenge.view.stop()

    async def challenge(
        self,
        ctx: commands.Context,
        opponent: discord.Member,
        kind: str,
        set_name: str = "",
    ) -> None:
        if opponent.bot or opponent.id == ctx.author.id:
            raise commands.BadArgument("Choose another human manager to challenge.")
        scope = scope_for(ctx)
        users = (str(ctx.author.id), str(opponent.id))
        # Reclaim expired sessions even if a deleted Discord message or a flood
        # of stale interactions delayed the UI framework's own timeout.
        for active in list(self.challenges.values()):
            if active.view and time.monotonic() >= active.view.deadline:
                await self.expire(active, active.view)
        if scope in self.challenges:
            raise commands.BadArgument(
                "There is already a Quick Match or shootout in this channel. Finish it or use `!cancelgame`."
            )
        if any(uid in self.busy for uid in users):
            raise commands.BadArgument(
                "One of these managers already has an active invitation or game."
            )
        if kind == "quick":
            if not set_name:
                set_name = next(
                    (
                        s
                        for s in ("26-27", "25-26", "24-25", "epl")
                        if s in self.runtime.available_sets
                    ),
                    "epl",
                )
            if set_name not in self.runtime.available_sets:
                raise commands.BadArgument(
                    "Unknown player set. Use `!sets` to see the available sets."
                )
        challenge = Challenge(
            kind,
            scope,
            users,
            (ctx.author.display_name, opponent.display_name),
            set_name,
        )
        if kind == "quick":
            try:
                challenge.lineups = quick_lineups(self.runtime, set_name)
            except ValueError as exc:
                raise commands.BadArgument(str(exc)) from exc
        self.challenges[scope] = challenge
        for uid in users:
            self.busy[uid] = challenge
        title = (
            "⚡ Quick Match invitation"
            if kind == "quick"
            else "🥅 Penalty Shootout invitation"
        )
        description = f"**{safe_name(challenge.names[0])}** challenges **{safe_name(challenge.names[1])}**.\n\n"
        description += (
            "Get two free, equally rated XIs. Lock in a secret tactic and play a full match.\n"
            if kind == "quick"
            else "Choose where to shoot and where to dive. Five penalties each, then sudden death.\n"
        )
        description += f"\nOnly {opponent.mention} can accept. Expires in {INVITE_TIMEOUT} seconds."
        embed = discord.Embed(title=title, description=description, color=BLUE)
        embed.set_footer(
            text="Casual game • No cost • Your club and career record stay as they are"
        )
        view = InvitationView(self, challenge)
        challenge.view = view
        try:
            challenge.message = await ctx.send(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self.close(challenge)
            raise

    async def update_challenge(
        self, challenge: Challenge, embed: discord.Embed, view: discord.ui.View | None
    ) -> None:
        old = challenge.view
        challenge.view = view
        if old and old is not view:
            old.stop()
        if challenge.message:
            try:
                await challenge.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                self.close(challenge)

    async def expire(
        self, challenge: Challenge, expected_view: discord.ui.View
    ) -> None:
        if not self.is_active(challenge) or challenge.view is not expected_view:
            return
        self.close(challenge)
        embed = discord.Embed(
            title="⌛ Game expired",
            description="A manager did not respond in time. Start a fresh game whenever you are ready.",
            color=AMBER,
        )
        await self.update_challenge(challenge, embed, None)

    async def cancel(self, ctx: commands.Context) -> None:
        challenge = self.challenges.get(scope_for(ctx))
        if not challenge:
            raise commands.BadArgument(
                "There is no active Quick Match or shootout in this channel."
            )
        if str(ctx.author.id) not in challenge.players:
            raise commands.BadArgument(
                "Only a manager playing this game can cancel it."
            )
        self.close(challenge)
        await self.update_challenge(
            challenge,
            discord.Embed(
                title="Game cancelled",
                description="No result was recorded.",
                color=AMBER,
            ),
            None,
        )
        await ctx.send("Game cancelled. You can start a fresh challenge.")

    def quick_embed(
        self, challenge: Challenge, result: dict | None = None
    ) -> discord.Embed:
        embed = discord.Embed(
            title="⚡ Quick Match · choose your tactic"
            if result is None
            else "⚡ Quick Match · full time",
            color=BLUE if result is None else GREEN,
        )
        if result is None:
            locked = "\n".join(
                f"{'🔒' if uid in challenge.tactics else '⏳'} {safe_name(name)}"
                for uid, name in zip(challenge.players, challenge.names)
            )
            embed.description = (
                "Your free XIs have identical ratings in every role. Choose a tactic below; choices stay secret until both are locked.\n\n"
                "**Attacking** creates more chances and opens your defence.\n"
                "**Defensive** limits chances at both ends. **Balanced** keeps an even approach.\n\n"
                + locked
            )
        else:
            a, b = result["goals"]
            embed.description = f"### {safe_name(challenge.names[0])} {a} — {b} {safe_name(challenge.names[1])}\n"
            embed.description += (
                "Honours even."
                if a == b
                else f"🏆 **{safe_name(challenge.names[0 if a > b else 1])} wins!**"
            )
            embed.add_field(
                name="Tactics revealed",
                value="\n".join(
                    f"{safe_name(name)} · **{challenge.tactics[uid]}**"
                    for uid, name in zip(challenge.players, challenge.names)
                ),
                inline=False,
            )
            if "expected_goals" in result:
                xg = result["expected_goals"]
                embed.add_field(
                    name="Expected goals",
                    value=f"{xg[0]:.2f} — {xg[1]:.2f}",
                    inline=False,
                )
        for name, lineup in zip(challenge.names, challenge.lineups):
            embed.add_field(
                name=f"{safe_name(name)} · 4-3-3",
                value="\n".join(
                    f"`{p['position'].upper():3}` {safe_name(p['name'])} · {p['rating']:g}"
                    for p in lineup["players"]
                )[:1024],
                inline=True,
            )
        embed.set_footer(
            text=f"{challenge.set_name} • Normalized ratings • No cost or career stat changes"
            if result
            else f"{TURN_TIMEOUT}s to choose • !cancelgame to stop"
        )
        return embed

    def shootout_embed(self, challenge: Challenge) -> discord.Embed:
        game = challenge.shootout
        assert game is not None
        embed = discord.Embed(
            title="🥅 Penalty Shootout" + (" · full time" if game.finished else ""),
            color=GREEN if game.finished else BLUE,
        )
        embed.description = f"### {safe_name(challenge.names[0])} {game.scores[0]} — {game.scores[1]} {safe_name(challenge.names[1])}\n"
        if game.finished:
            embed.description += (
                "Draw after ten penalties each."
                if game.winner is None
                else f"🏆 **{safe_name(challenge.names[game.winner])} wins the shootout!**"
            )
        else:
            embed.description += f"**{'Sudden death' if min(game.taken) >= 5 else 'Best of five'} · Kick {len(game.kicks) + 1}**\n"
            embed.description += f"⚽ **{safe_name(challenge.names[game.kicker])}**: choose where to shoot.\n🧤 **{safe_name(challenge.names[1 - game.kicker])}**: choose where to dive.\n\nBoth choose secretly using the same buttons. Matching directions = save; different directions = goal."
        for side, name in enumerate(challenge.names):
            marks = " ".join(
                "🟢" if scored else "🔴"
                for kicker, _, _, scored in game.kicks
                if kicker == side
            )
            embed.add_field(
                name=safe_name(name),
                value=f"{marks or 'No kicks yet'}\n{game.taken[side]} taken",
                inline=True,
            )
        if game.kicks:
            kicker, shot, dive, scored = game.kicks[-1]
            embed.add_field(
                name="Last kick · " + ("GOAL ⚽" if scored else "SAVED 🧤"),
                value=f"{safe_name(challenge.names[kicker])} shot **{shot}**; keeper dived **{dive}**.",
                inline=False,
            )
        embed.set_footer(
            text="Casual game • No cost or career stat changes"
            if game.finished
            else f"{TURN_TIMEOUT}s per kick • Sudden death capped at 10 each • !cancelgame"
        )
        return embed

    def get_league(self, scope: tuple[int, int]) -> League:
        league = self.leagues.get(scope)
        if league and time.monotonic() - league.updated_at >= LEAGUE_TIMEOUT:
            del self.leagues[scope]
            if league.view:
                league.view.stop()
            league = None
        if not league:
            raise ValueError(
                "There is no active mini league here. Use `!league create`."
            )
        return league

    def league_embed(self, league: League, games: list | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="🏆 Mini League · "
            + {
                "lobby": "lobby",
                "playing": "matchday",
                "finished": "final table",
                "ended": "closed",
            }[league.phase],
            color=GREEN if league.phase == "finished" else BLUE,
        )
        embed.description = f"Host: **{safe_name(league.members.get(league.host_id, 'Unknown'))}** · {len(league.members)}/{league.capacity} managers\n"
        if league.phase == "lobby":
            embed.description += (
                "Join with a saved starting XI. The host starts when everyone is ready. Each manager plays every other manager once.\n\n"
                + "\n".join(
                    f"{index}. {safe_name(name)}"
                    for index, name in enumerate(league.members.values(), 1)
                )
            )
        else:
            embed.description += (
                f"Rounds complete: **{league.round_index}/{len(league.rounds)}**\n"
            )
            for rank, row in enumerate(league.table(), 1):
                gd = row["gf"] - row["ga"]
                embed.add_field(
                    name=f"{rank}. {safe_name(row['name'])} · {row['points']} pts",
                    value=f"P {row['played']} · W {row['won']} · D {row['drawn']} · L {row['lost']} · GD {gd:+d} · GF {row['gf']}",
                    inline=False,
                )
            if league.phase == "finished":
                table = league.table()
                best = table[0]
                tied = [
                    r
                    for r in table
                    if (r["points"], r["gf"] - r["ga"], r["gf"])
                    == (best["points"], best["gf"] - best["ga"], best["gf"])
                ]
                embed.description += (
                    "🥇 "
                    + ("Joint winners: " if len(tied) > 1 else "Winner: ")
                    + ", ".join(f"**{safe_name(r['name'])}**" for r in tied)
                )
        if games:
            embed.add_field(
                name=f"Round {league.round_index} results",
                value="\n".join(
                    f"{safe_name(league.members[h])} **{g[0]} — {g[1]}** {safe_name(league.members[a])}"
                    for h, a, g in games
                ),
                inline=False,
            )
        if league.phase == "playing":
            embed.add_field(
                name="Next round",
                value="\n".join(
                    f"{safe_name(league.members[h])} vs {safe_name(league.members[a])}"
                    for h, a in league.rounds[league.round_index]
                ),
                inline=False,
            )
        embed.set_footer(
            text="3 points for a win • Ties: goal difference, goals scored • 30m idle timeout • Resets on restart"
        )
        return embed

    def league_action(
        self, scope: tuple[int, int], user: Any, action: str
    ) -> tuple[League, list | None]:
        league = self.get_league(scope)
        uid = str(user.id)
        games = None
        if action == "join":
            saved_lineup(self.runtime, uid)
            league.join(uid, user.display_name)
        elif action == "leave":
            league.leave(uid)
        elif action == "start":
            league.require_host(uid)
            if league.phase != "lobby":
                raise ValueError("This league has already started.")
            snapshots = {}
            for manager in league.members:
                try:
                    snapshots[manager] = saved_lineup(self.runtime, manager)
                except ValueError as exc:
                    raise ValueError(
                        f"{safe_name(league.members[manager])}: {exc}"
                    ) from exc
            league.start(uid, snapshots)
        elif action == "next":
            games = league.next_round(uid)
        elif action == "end":
            league.require_host(uid)
            league.phase = "ended"
            del self.leagues[scope]
        elif action != "table":
            raise ValueError("Unknown league action.")
        if action != "table":
            league.updated_at = time.monotonic()
        return league, games

    async def publish_league(
        self, scope: tuple[int, int], league: League, games=None, ctx=None
    ) -> None:
        if league.view:
            league.view.stop()
        view = LeagueView(self, scope, league) if league.phase != "ended" else None
        league.view = view
        embed = self.league_embed(league, games)
        try:
            if league.message:
                await league.message.edit(embed=embed, view=view)
            elif ctx:
                league.message = await ctx.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            if self.leagues.get(scope) is league:
                del self.leagues[scope]
            if view:
                view.stop()
            raise

    async def league_command(self, ctx: commands.Context, action: str) -> None:
        scope = scope_for(ctx)
        try:
            league = self.get_league(scope)
            async with league.lock:
                league, games = self.league_action(scope, ctx.author, action)
                if action == "table":
                    await ctx.send(embed=self.league_embed(league))
                else:
                    await self.publish_league(scope, league, games, ctx)
                    await ctx.send(f"Mini league updated: **{action}**.")
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc


class ChallengeView(discord.ui.View):
    def __init__(self, manager: GameModes, challenge: Challenge, timeout: float):
        super().__init__(timeout=timeout)
        self.manager, self.challenge = manager, challenge
        self.deadline = time.monotonic() + timeout
        self.expiry_task = asyncio.create_task(self._expire_after(timeout))

    async def _expire_after(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            await self.on_timeout()
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        super().stop()
        if self.expiry_task is not asyncio.current_task():
            self.expiry_task.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if time.monotonic() >= self.deadline:
            await self.manager.expire(self.challenge, self)
        if (
            not self.manager.is_active(self.challenge)
            or self.challenge.view is not self
        ):
            await interaction.response.send_message(
                "This game screen has expired. Use the latest game message.",
                ephemeral=True,
            )
            return False
        if str(interaction.user.id) not in self.challenge.players:
            await interaction.response.send_message(
                "These controls belong to the two managers playing.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        await self.manager.expire(self.challenge, self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        self.manager.close(self.challenge)
        message = "This game could not continue. Start a fresh challenge; no result was recorded."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        self.manager.bot.dispatch("game_mode_error", error)


class InvitationView(ChallengeView):
    def __init__(self, manager: GameModes, challenge: Challenge):
        super().__init__(manager, challenge, INVITE_TIMEOUT)

    @discord.ui.button(
        label="Accept challenge", style=discord.ButtonStyle.success, emoji="⚽"
    )
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        c = self.challenge
        if not self.manager.is_active(c) or c.view is not self:
            await interaction.response.send_message(
                "This invitation has expired.", ephemeral=True
            )
            return
        if str(interaction.user.id) != c.players[1]:
            await interaction.response.send_message(
                "Only the invited manager can accept this challenge.", ephemeral=True
            )
            return
        if c.phase != "invited":
            await interaction.response.send_message(
                "This invitation has already been answered.", ephemeral=True
            )
            return
        c.phase = "playing"
        if c.kind == "quick":
            view = TacticView(self.manager, c)
            embed = self.manager.quick_embed(c)
        else:
            c.shootout = Shootout(c.players)
            view = KickView(self.manager, c)
            embed = self.manager.shootout_embed(c)
        # Replace before yielding so double clicks cannot start two games.
        self.stop()
        c.view = view
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Decline / cancel", style=discord.ButtonStyle.secondary)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if (
            str(interaction.user.id) not in self.challenge.players
            or not self.manager.is_active(self.challenge)
            or self.challenge.view is not self
        ):
            await interaction.response.send_message(
                "This is not your active invitation.", ephemeral=True
            )
            return
        self.manager.close(self.challenge)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Invitation closed",
                description="Start another challenge when you are ready.",
                color=AMBER,
            ),
            view=None,
        )


class TacticView(ChallengeView):
    def __init__(self, manager: GameModes, challenge: Challenge):
        super().__init__(manager, challenge, TURN_TIMEOUT)
        for tactic, emoji in zip(TACTICS, ("🔥", "⚖️", "🛡️")):
            button = discord.ui.Button(
                label=tactic, emoji=emoji, style=discord.ButtonStyle.primary
            )

            async def callback(interaction: discord.Interaction, chosen=tactic):
                await self.choose(interaction, chosen)

            button.callback = callback
            self.add_item(button)

    async def choose(self, interaction: discord.Interaction, tactic: str) -> None:
        async with self.challenge.lock:
            await self._choose_locked(interaction, tactic)

    async def _choose_locked(
        self, interaction: discord.Interaction, tactic: str
    ) -> None:
        c = self.challenge
        uid = str(interaction.user.id)
        if uid not in c.players or not self.manager.is_active(c) or c.view is not self:
            await interaction.response.send_message(
                "This is not your active match.", ephemeral=True
            )
            return
        if uid in c.tactics:
            await interaction.response.send_message(
                "Your tactic is already locked.", ephemeral=True
            )
            return
        if tactic not in TACTICS:
            await interaction.response.send_message(
                "Choose one of the three tactics shown.", ephemeral=True
            )
            return
        c.tactics[uid] = tactic
        result = None
        if len(c.tactics) == 2:
            for side, user_id in enumerate(c.players):
                c.lineups[side]["tactic"] = c.tactics[user_id]
            result = simulate_game(*c.lineups)
            c.phase = "finished"
            self.manager.close(c)
        await interaction.response.send_message(
            f"🔒 **{tactic}** locked in.", ephemeral=True
        )
        await self.manager.update_challenge(
            c, self.manager.quick_embed(c, result), None if result else self
        )


class KickView(ChallengeView):
    def __init__(self, manager: GameModes, challenge: Challenge):
        super().__init__(manager, challenge, TURN_TIMEOUT)
        self.turn = len(challenge.shootout.kicks)
        for direction, emoji in zip(DIRECTIONS, ("⬅️", "⬆️", "➡️")):
            button = discord.ui.Button(
                label=direction.title(), emoji=emoji, style=discord.ButtonStyle.primary
            )

            async def callback(interaction: discord.Interaction, chosen=direction):
                await self.choose(interaction, chosen)

            button.callback = callback
            self.add_item(button)

    async def choose(self, interaction: discord.Interaction, direction: str) -> None:
        c = self.challenge
        if not self.manager.is_active(c) or c.view is not self:
            await interaction.response.send_message(
                "That kick has already finished.", ephemeral=True
            )
            return
        try:
            resolved = c.shootout.choose(str(interaction.user.id), direction, self.turn)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        next_view = self
        if resolved:
            if c.shootout.finished:
                c.phase = "finished"
                self.manager.close(c)
                next_view = None
            else:
                next_view = KickView(self.manager, c)
                self.stop()
                c.view = next_view
        await interaction.response.send_message(
            f"🔒 **{direction.title()}** locked in.", ephemeral=True
        )
        if resolved:
            await self.manager.update_challenge(
                c, self.manager.shootout_embed(c), next_view
            )


class LeagueView(discord.ui.View):
    def __init__(self, manager: GameModes, scope: tuple[int, int], league: League):
        super().__init__(timeout=LEAGUE_TIMEOUT)
        self.manager, self.scope, self.league = manager, scope, league
        self.expiry_task = asyncio.create_task(self._expire_after())
        actions = (
            [
                ("join", "Join", discord.ButtonStyle.success),
                ("leave", "Leave", discord.ButtonStyle.secondary),
                ("start", "Start league", discord.ButtonStyle.primary),
            ]
            if league.phase == "lobby"
            else []
        )
        if league.phase == "playing":
            actions.append(("next", "Play next round", discord.ButtonStyle.primary))
        actions.extend(
            [
                ("table", "Standings", discord.ButtonStyle.secondary),
                ("end", "End league", discord.ButtonStyle.danger),
            ]
        )
        for action, label, style in actions:
            button = discord.ui.Button(label=label, style=style)

            async def callback(interaction: discord.Interaction, selected=action):
                await self.act(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def _expire_after(self) -> None:
        try:
            await asyncio.sleep(
                max(0, LEAGUE_TIMEOUT - (time.monotonic() - self.league.updated_at))
            )
            await self.on_timeout()
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        super().stop()
        if self.expiry_task is not asyncio.current_task():
            self.expiry_task.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            self.manager.leagues.get(self.scope) is not self.league
            or self.league.view is not self
        ):
            await interaction.response.send_message(
                "This league screen has expired.", ephemeral=True
            )
            return False
        return True

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        async with self.league.lock:
            await self._act_locked(interaction, action)

    async def _act_locked(self, interaction: discord.Interaction, action: str) -> None:
        if (
            self.manager.leagues.get(self.scope) is not self.league
            or self.league.view is not self
        ):
            await interaction.response.send_message(
                "This league screen has expired.", ephemeral=True
            )
            return
        try:
            league, games = self.manager.league_action(
                self.scope, interaction.user, action
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if action == "table":
            await interaction.response.send_message(
                embed=self.manager.league_embed(league), ephemeral=True
            )
        else:
            # Invalidate the old controls before Discord I/O to prevent duplicate rounds.
            self.stop()
            league.view = None
            await interaction.response.defer()
            await self.manager.publish_league(self.scope, league, games)

    async def on_timeout(self) -> None:
        if (
            self.manager.leagues.get(self.scope) is self.league
            and self.league.view is self
        ):
            del self.manager.leagues[self.scope]
            if self.league.message:
                try:
                    embed = self.manager.league_embed(self.league)
                    embed.title = "⌛ Mini League · session expired"
                    embed.set_footer(
                        text="This session is closed. Create another with !league create"
                    )
                    await self.league.message.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass


def setup_game_modes(bot: commands.Bot, runtime: Any) -> GameModes:
    """Register commands and return their session manager for tests/inspection."""
    manager = GameModes(bot, runtime)

    @bot.command(name="quickmatch", aliases=["quick", "qm"])
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def quickmatch(ctx, opponent: discord.Member, set_name: str = ""):
        """Challenge a manager with free, equally rated XIs: !quickmatch @manager [set]."""
        await manager.challenge(ctx, opponent, "quick", set_name)

    @bot.command(name="penalties", aliases=["pens", "shootout"])
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def penalties(ctx, opponent: discord.Member):
        """Invite a manager to an interactive shootout: !penalties @manager."""
        await manager.challenge(ctx, opponent, "penalties")

    @bot.command(name="cancelgame")
    @commands.guild_only()
    async def cancelgame(ctx):
        """Cancel your active Quick Match, invitation or shootout in this channel."""
        await manager.cancel(ctx)

    @bot.group(name="league", invoke_without_command=True)
    @commands.guild_only()
    async def league(ctx):
        """Mini league: create, join, leave, start, next, table, end."""
        try:
            current = manager.get_league(scope_for(ctx))
        except ValueError:
            embed = discord.Embed(
                title="🏆 Mini League",
                description="Compete in a round robin with **2–8 managers** and your saved club XIs.\n\n`!league create [size]` — host a lobby (default 6)\n`!league join` / `!league leave` — manage your place\n`!league start` — host locks the XIs\n`!league next` — host plays the next round\n`!league table` — view the standings\n`!league end` — host closes the session",
                color=BLUE,
            )
            embed.set_footer(
                text="Session standings only • No entry fee • 30m idle timeout • Bot restarts end sessions"
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=manager.league_embed(current))

    @league.command(name="create")
    async def league_create(ctx, size: int = 6):
        """Host a league of 2–8 managers, using saved lineups."""
        if not 2 <= size <= 8:
            raise commands.BadArgument("Choose a league size between 2 and 8.")
        scope = scope_for(ctx)
        try:
            manager.get_league(scope)
        except ValueError:
            pass
        else:
            raise commands.BadArgument(
                "There is already a league here. Its host can close it with `!league end`."
            )
        uid = str(ctx.author.id)
        try:
            saved_lineup(runtime, uid)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        current = League(uid, capacity=size)
        current.join(uid, ctx.author.display_name)
        manager.leagues[scope] = current
        await manager.publish_league(scope, current, ctx=ctx)

    # Register distinct callbacks so command help and error routing remain useful.
    for action in ("join", "leave", "start", "next", "table", "end"):

        def make_callback(selected):
            async def callback(ctx):
                await manager.league_command(ctx, selected)

            return callback

        aliases = ["standings"] if action == "table" else []
        league.add_command(
            commands.Command(
                make_callback(action),
                name=action,
                aliases=aliases,
                help=f"{action.title()} the mini league in this channel.",
            )
        )
    return manager
