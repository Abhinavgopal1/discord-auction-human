"""Draft Clash and King of the Hill, using isolated free squads and shared rules."""

from __future__ import annotations

import asyncio
import copy
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from game_engine import (
    formation_slots,
    normalize_player,
    player_key,
    simulate_game,
    team_strength,
    validate_lineup,
)

COLOR = 0x24C6A5
DRAFT_SECONDS = 45
LOBBY_SECONDS = 600


def load_pool(runtime, set_key):
    pool, seen = [], set()
    for position in runtime.available_positions:
        tiers = runtime.load_players_by_position(position, set_key)
        for tier in ("A", "B", "C"):
            for record in tiers.get(tier, []):
                try:
                    card = normalize_player(record, position)
                except ValueError:
                    continue
                key = player_key(card)
                if key not in seen:
                    seen.add(key)
                    pool.append(card)
    return pool


def free_lineup(pool, rng=None):
    """Choose an independent legal free XI without spending or changing collections."""
    rng = rng or random.Random()
    cards = list(pool)
    rng.shuffle(cards)
    chosen, seen = [], set()
    # Natural positions keep generated teams simple and avoid greedy dual-role conflicts.
    for slot in formation_slots("4-3-3"):
        candidates = [
            p for p in cards if p["position"] == slot and player_key(p) not in seen
        ]
        if not candidates:
            raise ValueError(
                f"The selected set does not have enough {slot.upper()} players."
            )
        card = dict(rng.choice(candidates), assigned_position=slot)
        seen.add(player_key(card))
        chosen.append(card)
    return {"players": chosen, "formation": "4-3-3", "tactic": "Balanced"}


def owned_lineup(runtime, user_id):
    user_id = str(user_id)
    lineup = runtime.user_lineups.get(user_id, {}).get(
        runtime.active_lineups.get(user_id, "main")
    )
    if not lineup:
        raise ValueError("Set an active XI first with `!autolineup` or `!setlineup`.")
    errors = validate_lineup(
        lineup.get("players", []),
        lineup.get("formation"),
        runtime.user_teams.get(user_id, []),
    )
    if errors:
        raise ValueError("Your active XI needs updating: " + " ".join(errors[:3]))
    snapshot = copy.deepcopy(lineup)
    snapshot["players"] = [normalize_player(p) for p in snapshot["players"]]
    return snapshot


@dataclass
class DraftSession:
    host: str
    set_key: str
    entrants: list = field(default_factory=list)
    teams: dict = field(default_factory=dict)
    pool: list = field(default_factory=list)
    started: bool = False
    turn: int = 0
    offers: list = field(default_factory=list)
    version: int = 0
    task: object = None
    view: object = None
    created: float = field(default_factory=time.monotonic)

    def current(self):
        if not self.started or self.turn >= len(self.entrants) * 11:
            return None
        round_number, index = divmod(self.turn, len(self.entrants))
        return self.entrants[
            index if round_number % 2 == 0 else len(self.entrants) - 1 - index
        ]

    def slot(self):
        return formation_slots("4-3-3")[self.turn // len(self.entrants)]

    def begin(self, pool, rng):
        if not 2 <= len(self.entrants) <= 8:
            raise ValueError("Draft Clash needs 2–8 managers. Use `!draftclash join`.")
        required = Counter(formation_slots("4-3-3"))
        counts = Counter(p["position"] for p in pool)
        missing = [
            p.upper()
            for p, count in required.items()
            if counts[p] < count * len(self.entrants)
        ]
        if missing:
            raise ValueError(
                "This set needs more unique "
                + ", ".join(missing)
                + " cards for this lobby."
            )
        self.pool = copy.deepcopy(pool)
        rng.shuffle(self.entrants)
        self.teams = {user: [] for user in self.entrants}
        self.started = True

    def offer(self, rng):
        candidates = [p for p in self.pool if p["position"] == self.slot()]
        if not candidates:
            raise ValueError("This draft ran out of eligible players.")
        self.offers = rng.sample(candidates, min(3, len(candidates)))
        self.version += 1
        return self.offers

    def pick(self, user_id, index, version=None):
        if str(user_id) != self.current():
            raise ValueError("It is another manager's turn.")
        if version is not None and version != self.version:
            raise ValueError("That pick has expired. Use the latest draft card.")
        if not 0 <= index < len(self.offers):
            raise ValueError("Choose one of the numbered cards in the current offer.")
        card = dict(self.offers[index], assigned_position=self.slot())
        self.teams[str(user_id)].append(card)
        key = player_key(card)
        self.pool = [p for p in self.pool if player_key(p) != key]
        self.turn += 1
        self.offers = []
        self.version += 1
        return card


def knockout(teams, rng=None):
    """Run a complete bracket, with byes and explicit penalties after drawn matches."""
    rng = rng or random.Random()
    remaining = list(teams)
    if len(remaining) < 2:
        raise ValueError("A knockout needs at least two teams.")
    rng.shuffle(remaining)
    rounds = []
    while len(remaining) > 1:
        next_round, matches = [], []
        for index in range(0, len(remaining), 2):
            first = remaining[index]
            if index + 1 == len(remaining):
                next_round.append(first)
                matches.append({"bye": first})
                continue
            second = remaining[index + 1]
            result = simulate_game(teams[first], teams[second], rng=rng)
            penalties = result["winner"] is None
            winner = (
                rng.choice([first, second])
                if penalties
                else [first, second][result["winner"]]
            )
            matches.append(
                {
                    "players": [first, second],
                    "result": result,
                    "winner": winner,
                    "penalties": penalties,
                }
            )
            next_round.append(winner)
        rounds.append(matches)
        remaining = next_round
    return remaining[0], rounds


def setup_classic_modes(bot, runtime):
    drafts, hills = {}, {}
    rng = random.Random()

    def default_set():
        return (
            "26-27"
            if "26-27" in runtime.available_sets
            else next(iter(runtime.available_sets))
        )

    def check_set(value):
        value = value or default_set()
        if value not in runtime.available_sets:
            raise ValueError(
                "Unknown set. Use `!sets` to see the available player sets."
            )
        return value

    def stop_timer(session):
        if session.task and session.task is not asyncio.current_task():
            session.task.cancel()
        session.task = None
        if session.view:
            session.view.stop()
            session.view = None

    async def save_warning(ctx):
        if not runtime.save_data():
            await ctx.send(
                "The result is recorded in this session, but saving failed. Please ask the host to check storage before restarting."
            )

    def lobby_embed(session):
        embed = discord.Embed(
            title="🎴 Draft Clash · Lobby",
            description="Build a free 11-player team from three-card offers, then play a knockout cup.",
            color=COLOR,
        )
        embed.add_field(
            name="Managers · 2–8",
            value="\n".join(f"<@{uid}>" for uid in session.entrants),
        )
        embed.add_field(
            name="Rules",
            value=f"{runtime.available_sets[session.set_key]} · 4-3-3\nSnake order · {DRAFT_SECONDS}s picks\nUnpicked turns choose the highest rating automatically.",
            inline=False,
        )
        embed.set_footer(
            text="Lobby closes after 10 minutes. Your auction squad and budget are separate."
        )
        return embed

    async def join_draft(ctx, session, user_id):
        uid = str(user_id)
        if session.started:
            raise ValueError("The draft has begun. Join the next lobby.")
        if uid in session.entrants:
            raise ValueError("You already joined this draft.")
        if len(session.entrants) >= 8:
            raise ValueError("This draft is full (8 managers).")
        session.entrants.append(uid)

    class LobbyView(discord.ui.View):
        def __init__(self, ctx, session):
            super().__init__(timeout=LOBBY_SECONDS)
            self.ctx, self.session = ctx, session

        @discord.ui.button(
            label="Join draft", style=discord.ButtonStyle.success, emoji="🎴"
        )
        async def join(self, interaction, button):
            if drafts.get(self.ctx.channel.id) is not self.session:
                await interaction.response.send_message(
                    "This lobby has closed.", ephemeral=True
                )
                return
            try:
                await join_draft(self.ctx, self.session, interaction.user.id)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.edit_message(
                embed=lobby_embed(self.session), view=self
            )

        @discord.ui.button(label="Begin", style=discord.ButtonStyle.primary, emoji="▶️")
        async def begin(self, interaction, button):
            if str(interaction.user.id) != self.session.host:
                await interaction.response.send_message(
                    "Only the host can begin this draft.", ephemeral=True
                )
                return
            if (
                drafts.get(self.ctx.channel.id) is not self.session
                or self.session.started
            ):
                await interaction.response.send_message(
                    "This lobby has already closed.", ephemeral=True
                )
                return
            try:
                self.session.begin(load_pool(runtime, self.session.set_key), rng)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            stop_timer(self.session)
            await interaction.response.edit_message(view=None)
            await offer_draft(self.ctx, self.session)

    class PickView(discord.ui.View):
        def __init__(self, ctx, session):
            super().__init__(timeout=DRAFT_SECONDS + 5)
            self.ctx, self.session, self.version = ctx, session, session.version
            for index in range(len(session.offers)):
                button = discord.ui.Button(
                    label=f"Pick {index + 1}", style=discord.ButtonStyle.primary
                )

                async def callback(interaction, selected=index):
                    if drafts.get(ctx.channel.id) is not session:
                        await interaction.response.send_message(
                            "This draft has ended.", ephemeral=True
                        )
                        return
                    try:
                        card = session.pick(interaction.user.id, selected, self.version)
                    except ValueError as exc:
                        await interaction.response.send_message(
                            str(exc), ephemeral=True
                        )
                        return
                    stop_timer(session)
                    await interaction.response.edit_message(view=None)
                    await ctx.send(
                        f"<@{interaction.user.id}> picks **{discord.utils.escape_markdown(card['name'])}** · {card['rating']:g} OVR"
                    )
                    await offer_draft(ctx, session)

                button.callback = callback
                self.add_item(button)

    async def offer_draft(ctx, session):
        if drafts.get(ctx.channel.id) is not session:
            return
        if session.current() is None:
            drafts.pop(ctx.channel.id, None)
            stop_timer(session)
            teams = {
                uid: {"players": cards, "formation": "4-3-3", "tactic": "Balanced"}
                for uid, cards in session.teams.items()
            }
            winner, rounds = knockout(teams, rng)
            runtime.draft_clash_wins[winner] = (
                int(runtime.draft_clash_wins.get(winner, 0)) + 1
            )
            await save_warning(ctx)
            embed = discord.Embed(
                title="🏆 Draft Clash · Cup results",
                description=f"Champion: <@{winner}>",
                color=COLOR,
            )
            for index, matches in enumerate(rounds, 1):
                lines = []
                for match in matches:
                    if "bye" in match:
                        lines.append(f"<@{match['bye']}> advances with a bye")
                    else:
                        a, b = match["players"]
                        x, y = match["result"]["goals"]
                        suffix = " on penalties" if match["penalties"] else ""
                        lines.append(
                            f"<@{a}> **{x}–{y}** <@{b}> · <@{match['winner']}> advances{suffix}"
                        )
                embed.add_field(
                    name=f"Round {index}", value="\n".join(lines), inline=False
                )
            embed.set_footer(text="Start a rematch with !draftclash start")
            await ctx.send(embed=embed)
            return
        session.offer(rng)
        version = session.version
        manager = session.current()
        embed = discord.Embed(
            title=f"🎴 Draft Clash · Pick {session.turn + 1}/{len(session.entrants) * 11}",
            description=f"<@{manager}> · choose your **{session.slot().upper()}**",
            color=COLOR,
        )
        for index, card in enumerate(session.offers, 1):
            embed.add_field(
                name=f"{index}. {discord.utils.escape_markdown(card['name'])}",
                value=f"**{card['rating']:g} OVR** · {card['position'].upper()}\n{card.get('club', card.get('league', 'Football'))}",
                inline=False,
            )
        embed.set_footer(
            text=f"Pick within {DRAFT_SECONDS}s · buttons or !draftclash pick 1/2/3"
        )
        view = PickView(ctx, session)
        session.view = view
        await ctx.send(embed=embed, view=view)

        async def auto_pick():
            await asyncio.sleep(DRAFT_SECONDS)
            if drafts.get(ctx.channel.id) is session and session.version == version:
                best = max(
                    range(len(session.offers)),
                    key=lambda i: session.offers[i]["rating"],
                )
                card = session.pick(manager, best, version)
                stop_timer(session)
                await ctx.send(
                    f"⏱️ Auto-pick for <@{manager}>: **{discord.utils.escape_markdown(card['name'])}** ({card['rating']:g} OVR)"
                )
                await offer_draft(ctx, session)

        session.task = asyncio.create_task(auto_pick())

    @bot.command(name="draftclash", aliases=["draft"])
    @commands.guild_only()
    async def draftclash(ctx, action: str = None, *, argument: str = None):
        """Free snake draft: start [set], join, begin, pick N, team, status, cancel."""
        action = (action or "help").lower()
        session = drafts.get(ctx.channel.id)
        try:
            if action == "start":
                if session:
                    raise ValueError(
                        "A draft already exists here. Use `!draftclash status`."
                    )
                session = DraftSession(
                    str(ctx.author.id), check_set(argument), [str(ctx.author.id)]
                )
                drafts[ctx.channel.id] = session
                view = LobbyView(ctx, session)
                session.view = view

                async def expire_lobby():
                    await asyncio.sleep(LOBBY_SECONDS)
                    if drafts.get(ctx.channel.id) is session and not session.started:
                        drafts.pop(ctx.channel.id, None)
                        stop_timer(session)
                        await ctx.send(
                            "Draft Clash lobby expired. Start again with `!draftclash start`."
                        )

                session.task = asyncio.create_task(expire_lobby())
                await ctx.send(embed=lobby_embed(session), view=view)
            elif action in ("join", "begin", "pick", "cancel", "end", "status", "team"):
                if not session:
                    raise ValueError(
                        "No draft in this channel. Use `!draftclash start [set]`."
                    )
                if action == "join":
                    await join_draft(ctx, session, ctx.author.id)
                    await ctx.send(embed=lobby_embed(session))
                elif action == "begin":
                    if str(ctx.author.id) != session.host:
                        raise ValueError("Only the host can begin this draft.")
                    if session.started:
                        raise ValueError("The draft has already started.")
                    session.begin(load_pool(runtime, session.set_key), rng)
                    stop_timer(session)
                    await offer_draft(ctx, session)
                elif action == "pick":
                    if not session.started or not argument or not argument.isdigit():
                        raise ValueError(
                            "Use `!draftclash pick 1`, `2`, or `3` during your turn."
                        )
                    card = session.pick(ctx.author.id, int(argument) - 1)
                    stop_timer(session)
                    await ctx.send(
                        f"<@{ctx.author.id}> picks **{discord.utils.escape_markdown(card['name'])}** ({card['rating']:g} OVR)"
                    )
                    await offer_draft(ctx, session)
                elif action in ("cancel", "end"):
                    if (
                        str(ctx.author.id) != session.host
                        and not ctx.author.guild_permissions.manage_guild
                    ):
                        raise ValueError(
                            "Only the host or a server manager can end this draft."
                        )
                    drafts.pop(ctx.channel.id, None)
                    stop_timer(session)
                    await ctx.send(
                        "Draft ended. Collections and budgets are unchanged."
                    )
                elif action == "team":
                    cards = session.teams.get(str(ctx.author.id), [])
                    await ctx.send(
                        embed=discord.Embed(
                            title="🎴 Your draft team",
                            description="\n".join(
                                f"**{p['assigned_position'].upper()}** · {discord.utils.escape_markdown(p['name'])} · {p['rating']:g}"
                                for p in cards
                            )
                            or "Your picks will appear here once the draft starts.",
                            color=COLOR,
                        )
                    )
                else:
                    if session.started:
                        await ctx.send(
                            f"Pick **{session.turn + 1}/{len(session.entrants) * 11}** · <@{session.current()}> choosing **{session.slot().upper()}**. Use the latest offer or `!draftclash pick N`."
                        )
                    else:
                        await ctx.send(embed=lobby_embed(session))
            else:
                await ctx.send(
                    embed=discord.Embed(
                        title="🎴 Draft Clash",
                        description="`!draftclash start [set]` → `join` → host `begin`\nChoose from three cards each turn; the order reverses every round.\n`!draftclash pick 1` · `team` · `status` · `cancel`\n2–8 managers · free squads · knockout cup after all 11 picks.",
                        color=COLOR,
                    )
                )
        except ValueError as exc:
            await ctx.send(str(exc))

    @bot.command(name="draftclashleaderboard")
    @commands.guild_only()
    async def draftclashleaderboard(ctx):
        rows = [
            (uid, int(wins))
            for uid, wins in runtime.draft_clash_wins.items()
            if ctx.guild.get_member(int(uid))
        ]
        rows.sort(key=lambda row: (-row[1], row[0]))
        await ctx.send(
            embed=discord.Embed(
                title="🏆 Draft Clash · Champions",
                description="\n".join(
                    f"**{i}.** <@{uid}> · {wins} cup wins"
                    for i, (uid, wins) in enumerate(rows[:15], 1)
                )
                or "No champions yet. Start with `!draftclash start`.",
                color=COLOR,
            )
        )

    def standings(ctx, mode):
        return (
            runtime.koth_state.setdefault("leaderboards", {})
            .setdefault(str(ctx.guild.id), {})
            .setdefault(mode, {})
        )

    def hill_embed(hill):
        return discord.Embed(
            title=f"👑 King of the Hill · {hill['mode'].title()}",
            description=f"King: {('<@' + hill['king'] + '>') if hill['king'] else 'Crown is waiting'}\nCurrent reign: **{hill['streak']}** wins\nManagers: **{len(hill['teams'])}**\n\n`!koth join` · `!challenge` · `!koth team` · `!kothleaderboard`",
            color=COLOR,
        )

    def join_hill(hill, uid):
        uid = str(uid)
        if uid in hill["teams"]:
            raise ValueError(
                "You already joined. Your submitted XI stays fixed for this hill."
            )
        if len(hill["teams"]) >= 32:
            raise ValueError("This hill is full (32 managers).")
        hill["teams"][uid] = (
            owned_lineup(runtime, uid)
            if hill["mode"] == "auction"
            else free_lineup(hill["pool"], rng)
        )
        if hill["king"] is None:
            hill["king"] = uid

    @bot.command(name="koth")
    @commands.guild_only()
    async def koth(ctx, action: str = None, mode: str = None, set_key: str = None):
        """King of the Hill: start auction|draft [set], join, team, status, end."""
        action = (action or "help").lower()
        hill = hills.get(ctx.channel.id)
        try:
            if action == "start":
                if hill:
                    raise ValueError("A hill already exists here. Use `!kingstatus`.")
                mode = (mode or "auction").lower()
                if mode not in ("auction", "draft"):
                    raise ValueError(
                        "Choose `!koth start auction` or `!koth start draft [set]`."
                    )
                key = check_set(set_key)
                pool = load_pool(runtime, key) if mode == "draft" else []
                if mode == "draft":
                    free_lineup(
                        pool, rng
                    )  # Ensure the set can fill a legal XI before opening.
                hill = {
                    "host": str(ctx.author.id),
                    "mode": mode,
                    "set": key,
                    "pool": pool,
                    "teams": {},
                    "king": None,
                    "streak": 0,
                    "last_challenge": {},
                }
                hills[ctx.channel.id] = hill
                await ctx.send(embed=hill_embed(hill))
            elif action in ("join", "team", "status", "end", "cancel"):
                if not hill:
                    raise ValueError(
                        "No hill in this channel. Use `!koth start auction` or `!koth start draft`."
                    )
                if action == "join":
                    join_hill(hill, ctx.author.id)
                    await ctx.send(embed=hill_embed(hill))
                elif action == "team":
                    lineup = hill["teams"].get(str(ctx.author.id))
                    if not lineup:
                        raise ValueError("Join first with `!koth join`.")
                    embed = discord.Embed(
                        title=f"👑 Your hill XI · {team_strength(lineup):g} OVR",
                        description="\n".join(
                            f"**{p.get('assigned_position', p['position']).upper()}** · {discord.utils.escape_markdown(p['name'])} · {p['rating']:g}"
                            for p in lineup["players"]
                        ),
                        color=COLOR,
                    )
                    await ctx.send(embed=embed)
                elif action in ("end", "cancel"):
                    if (
                        str(ctx.author.id) != hill["host"]
                        and not ctx.author.guild_permissions.manage_guild
                    ):
                        raise ValueError(
                            "Only the host or a server manager can close this hill."
                        )
                    hills.pop(ctx.channel.id, None)
                    await ctx.send(
                        "Hill closed. The leaderboard keeps its results. Start a fresh hill with `!koth start`."
                    )
                else:
                    await ctx.send(embed=hill_embed(hill))
            else:
                await ctx.send(
                    embed=discord.Embed(
                        title="👑 King of the Hill",
                        description="`!koth start auction` uses your active owned XI.\n`!koth start draft [set]` gives each manager a free random XI.\n\n`!koth join` → `!challenge` the king. Draws keep the crown in place.\n`!kingstatus` · `!koth team` · `!kothleaderboard [auction|draft]`\nThe first manager to join takes the crown. Teams stay fixed until the hill ends.",
                        color=COLOR,
                    )
                )
        except ValueError as exc:
            await ctx.send(str(exc))

    @bot.command(name="challenge")
    @commands.guild_only()
    async def challenge(ctx, opponent: discord.Member = None):
        hill = hills.get(ctx.channel.id)
        uid = str(ctx.author.id)
        if not hill or not hill["king"]:
            await ctx.send(
                "There is no king here yet. Start a hill and join with `!koth join`."
            )
            return
        king = hill["king"]
        if uid not in hill["teams"]:
            await ctx.send("Join this hill first with `!koth join`.")
            return
        if uid == king:
            await ctx.send("You hold the crown. Another manager must challenge you.")
            return
        if opponent and str(opponent.id) != king:
            await ctx.send(
                f"The current king is <@{king}>. Use `!challenge` to challenge them."
            )
            return
        now = time.monotonic()
        remaining = 30 - (now - hill["last_challenge"].get(uid, -30))
        if remaining > 0:
            await ctx.send(f"Your next challenge is ready in {int(remaining) + 1}s.")
            return
        result = simulate_game(hill["teams"][uid], hill["teams"][king], rng=rng)
        # Commit all match state before the first await, so overlapping commands cannot race.
        hill["last_challenge"][uid] = now
        board = standings(ctx, hill["mode"])
        for user in (uid, king):
            row = board.setdefault(
                user, {"wins": 0, "losses": 0, "draws": 0, "best_streak": 0}
            )
            for name in ("wins", "losses", "draws", "best_streak"):
                row.setdefault(name, 0)
        if result["winner"] is None:
            board[uid]["draws"] += 1
            board[king]["draws"] += 1
            outcome = f"Draw — <@{king}> keeps the crown."
        else:
            winner, loser = (uid, king) if result["winner"] == 0 else (king, uid)
            board[winner]["wins"] += 1
            board[loser]["losses"] += 1
            hill["streak"] = hill["streak"] + 1 if winner == king else 1
            hill["king"] = winner
            board[winner]["best_streak"] = max(
                board[winner]["best_streak"], hill["streak"]
            )
            outcome = (
                f"<@{winner}> holds the crown · **{hill['streak']}** consecutive wins"
            )
        await save_warning(ctx)
        x, y = result["goals"]
        embed = discord.Embed(
            title=f"👑 Hill challenge · {x}–{y}",
            description=f"<@{uid}> **{x}–{y}** <@{king}>\n\n{outcome}",
            color=COLOR,
        )
        embed.add_field(
            name="Expected goals",
            value=f"{result['expected_goals'][0]:.2f} – {result['expected_goals'][1]:.2f}",
        )
        embed.set_footer(
            text="Another manager can challenge now. Your own challenges have a 30s cooldown."
        )
        await ctx.send(embed=embed)

    @bot.command(name="kingstatus")
    @commands.guild_only()
    async def kingstatus(ctx):
        hill = hills.get(ctx.channel.id)
        if hill:
            await ctx.send(embed=hill_embed(hill))
        else:
            await ctx.send(
                "No active hill here. Use `!koth start auction` or `!koth start draft`."
            )

    @bot.command(name="kothleaderboard")
    @commands.guild_only()
    async def kothleaderboard(ctx, mode: str = None):
        mode = (mode or hills.get(ctx.channel.id, {}).get("mode", "auction")).lower()
        if mode not in ("auction", "draft"):
            await ctx.send(
                "Choose `!kothleaderboard auction` or `!kothleaderboard draft`."
            )
            return
        board = standings(ctx, mode)
        rows = sorted(
            board.items(),
            key=lambda row: (
                -row[1].get("wins", 0),
                -row[1].get("best_streak", 0),
                row[0],
            ),
        )[:15]
        embed = discord.Embed(
            title=f"👑 {mode.title()} hill · Leaderboard",
            description="\n".join(
                f"**{index}.** <@{uid}> · {row.get('wins', 0)}W {row.get('draws', 0)}D {row.get('losses', 0)}L · best reign {row.get('best_streak', 0)}"
                for index, (uid, row) in enumerate(rows, 1)
            )
            or "No hill matches yet.",
            color=COLOR,
        )
        await ctx.send(embed=embed)

    # Exposed for lifecycle cleanup and integration tests; no live sessions are persisted.
    return {"draft_sessions": drafts, "hill_sessions": hills}
