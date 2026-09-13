"""Football Auction Discord bot.

The bot keeps Discord callbacks thin and delegates football rules to
``game_engine``, while ``game_modes`` and ``classic_modes`` provide the
consent-based competitions.  State is stored in one atomic JSON snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from copy import deepcopy
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from auction_engine import MAX_SQUAD, STARTING_BUDGET, AuctionRoom
from game_engine import (
    FORMATIONS,
    auto_lineup,
    normalize_player,
    parse_currency,
    player_key,
    simulate_game,
    team_strength,
    validate_lineup,
)
from storage import STATE_KEYS, StateStore

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("football_auction")
DATA_DIR = Path(os.getenv("AUCTION_DATA_DIR", str(ROOT / "data")))
store = StateStore(DATA_DIR)

available_positions = ("st", "rw", "lw", "cam", "cm", "lb", "cb", "rb", "gk")
available_formations = FORMATIONS
available_tactics = ("Balanced", "Attacking", "Defensive")
available_sets = {
    "26-27": "2026–27 · Current season",
    "24-25": "2024–25 · Archive",
    "wc": "World Cup · Historical XI",
    "ucl": "Champions League · Historical XI",
    "epl": "Premier League · Historical XI",
    "laliga": "La Liga · Historical XI",
    "2010-2025": "2010–2025 · Legends",
    "bundesliga": "Bundesliga · Partial archive",
    "seriea": "Serie A · Partial archive",
}

user_teams: dict[str, list[dict]] = {}
user_budgets: dict[str, int] = {}
user_lineups: dict[str, dict] = {}
active_lineups: dict[str, str] = {}
user_stats: dict[str, dict] = {}
draft_clash_wins: dict[str, int] = {}
koth_state: dict = {}
active_auctions: dict[int, AuctionRoom] = {}
auction_tasks: dict[int, asyncio.Task] = {}
auction_messages: dict[int, discord.Message] = {}
auction_views: dict[int, discord.ui.View] = {}
lineup_views: dict[str, discord.ui.View] = {}
battle_invites: dict[tuple[int, int, int], discord.ui.View] = {}
COLOR = discord.Color.from_rgb(38, 187, 153)


class FootballBot(commands.Bot):
    async def setup_hook(self):
        self.housekeeping_task = asyncio.create_task(housekeeping())

    async def close(self):
        tasks = list(auction_tasks.values())
        if hasattr(self, "housekeeping_task"):
            tasks.append(self.housekeeping_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = FootballBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
)


def format_currency(amount: float) -> str:
    return f"${amount:,.0f}"


def card(
    title: str, description: str = "", *, color: discord.Color = COLOR
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="FOOTBALL AUCTION • !footy • Prices are in-game currency")
    return embed


def safe_name(value: object) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))[
        :100
    ]


def ensure_user_structures(user_id: str) -> None:
    uid = str(user_id)
    user_teams.setdefault(uid, [])
    user_budgets.setdefault(uid, STARTING_BUDGET)
    user_lineups.setdefault(uid, {})
    active_lineups.setdefault(uid, "main")
    user_stats.setdefault(uid, {})
    for key in (
        "wins",
        "losses",
        "draws",
        "money_spent",
        "most_expensive",
        "trades_made",
    ):
        user_stats[uid].setdefault(key, 0)


def save_data() -> bool:
    try:
        store.save({key: globals()[key] for key in STATE_KEYS})
        return True
    except (OSError, TypeError, ValueError):
        log.exception("Could not save state")
        return False


def load_data() -> bool:
    try:
        loaded = store.load()
    except (OSError, TypeError, ValueError):
        log.exception("Saved state is invalid; refusing to overwrite it")
        return False
    for key, value in loaded.items():
        globals()[key].clear()
        globals()[key].update(value)
    for uid in set(user_teams) | set(user_budgets) | set(user_lineups):
        ensure_user_structures(uid)
    return True


def load_players_by_position(position: str, set_name: str) -> dict[str, list[dict]]:
    tiers = {"A": [], "B": [], "C": []}
    if position not in available_positions or set_name not in available_sets:
        return tiers
    path = ROOT / "players" / set_name / f"{position}.json"
    if not path.exists():
        return tiers
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise TypeError(f"Invalid player list in {path.relative_to(ROOT)}")
    seen: set[str] = set()
    for record in records:
        player = normalize_player(record, position=position)
        key = player_key(player)
        if key in seen:
            continue
        seen.add(key)
        player["set_key"] = set_name
        tiers[player["tier"]].append(player)
    return tiers


def player_pool(set_name: str, position: str | None = None) -> list[dict]:
    pool, seen = [], set()
    positions = [position] if position else list(available_positions)
    for pos in positions:
        for group in load_players_by_position(pos, set_name).values():
            for player in group:
                if player_key(player) not in seen:
                    seen.add(player_key(player))
                    pool.append(player)
    return pool


def is_user_in_any_auction(user_id: int | str) -> bool:
    uid = str(user_id)
    return any(uid in room.participants for room in active_auctions.values())


def require_room(ctx, host: bool = False) -> AuctionRoom:
    room = active_auctions.get(ctx.channel.id)
    if room is None:
        raise ValueError("No auction here yet. Use `!startauction @friends`.")
    uid = str(ctx.author.id)
    if host and room.host != uid:
        raise ValueError("Only this auction's host can do that.")
    if uid in room.participants:
        room.last_activity = time.monotonic()
    return room


def choose_set(room: AuctionRoom, key: str) -> None:
    key = key.lower()
    if key not in available_sets:
        raise ValueError("Unknown set. Use `!sets` to see the choices.")
    if room.set_key:
        raise ValueError("The collection is already locked for this auction.")
    missing = [pos.upper() for pos in available_positions if not player_pool(key, pos)]
    if missing:
        raise ValueError(
            f"This archive is missing {', '.join(missing)}. Choose `26-27` for a complete auction."
        )
    room.set_key = key


class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int | str, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These controls belong to the manager who opened them.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item) -> None:
        if not isinstance(error, ValueError):
            log.error(
                "UI callback failed", exc_info=(type(error), error, error.__traceback__)
            )
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Something went wrong. Please retry."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def lobby_embed(room: AuctionRoom) -> discord.Embed:
    embed = card("⚽ Auction lobby", "Build your squad, then take it onto the pitch.")
    embed.add_field(
        name="Collection", value=available_sets.get(room.set_key, "Choose below")
    )
    embed.add_field(name="Managers", value=str(len(room.participants)))
    embed.add_field(
        name="Bid clock", value=f"{room.timer} seconds; resets after every bid"
    )
    embed.add_field(
        name="Squad rule",
        value=f"{MAX_SQUAD} players maximum · {format_currency(STARTING_BUDGET)} starting wallet",
        inline=False,
    )
    embed.add_field(
        name="Host controls",
        value="`!set 26-27` · choose collection\n`!st` / `!cm` / `!gk` · nominate a position\n`!add @friend` · grow the lobby",
        inline=False,
    )
    return embed


def auction_embed(room: AuctionRoom) -> discord.Embed:
    player = room.current_player
    if player is None:
        return lobby_embed(room)
    embed = card(
        f"🔨 {safe_name(player['name'])}",
        f"**{player['position'].upper()}** · {player.get('rating', '—')} OVR · Tier {player.get('tier', 'C')}",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Club / collection",
        value=player.get("club") or available_sets.get(room.set_key, "Custom"),
        inline=False,
    )
    embed.add_field(
        name="Current bid" if room.highest_bidder else "Opening price",
        value=format_currency(room.current_price),
    )
    embed.add_field(
        name="Leading manager",
        value=f"<@{room.highest_bidder}>" if room.highest_bidder else "Open for bids",
    )
    embed.add_field(name="Next bid", value=format_currency(room.minimum_bid()))
    close_at = int(time.time() + max(0, room.deadline - time.monotonic()))
    embed.add_field(
        name="Closes",
        value=f"<t:{close_at}:R> · clock resets after every bid",
        inline=False,
    )
    embed.set_footer(
        text=f"Use !bid [25m] or the buttons · {len(room.passed)} managers passed"
    )
    return embed


class SetPicker(OwnedView):
    def __init__(self, ctx, room):
        super().__init__(ctx.author.id, timeout=600)
        self.ctx, self.room = ctx, room
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in available_sets.items()
        ]
        select = discord.ui.Select(
            placeholder="Choose a player collection", options=options
        )
        select.callback = self.select_set
        self.add_item(select)

    async def select_set(self, interaction):
        if active_auctions.get(self.ctx.channel.id) is not self.room:
            raise ValueError("This auction has ended.")
        choose_set(self.room, interaction.data["values"][0])
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(embed=lobby_embed(self.room), view=self)


class BidModal(discord.ui.Modal, title="Place a bid"):
    amount = discord.ui.TextInput(
        label="Amount", placeholder="25m, 500k, or 25000000", max_length=24
    )

    def __init__(self, ctx, room, lot_id):
        super().__init__(timeout=120)
        self.ctx, self.room, self.lot_id = ctx, room, lot_id

    async def on_submit(self, interaction):
        try:
            if (
                active_auctions.get(self.ctx.channel.id) is not self.room
                or self.room.lot_id != self.lot_id
            ):
                raise ValueError("This player has closed.")
            ensure_user_structures(str(interaction.user.id))
            self.room.bid(
                str(interaction.user.id),
                parse_currency(str(self.amount)),
                user_budgets,
                user_teams,
                lot_id=self.lot_id,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer()
        await refresh_auction(self.ctx.channel.id)


class AuctionControls(discord.ui.View):
    def __init__(self, ctx, room):
        super().__init__(timeout=900)
        self.ctx, self.room, self.lot_id = ctx, room, room.lot_id

    async def interaction_check(self, interaction):
        if (
            active_auctions.get(self.ctx.channel.id) is not self.room
            or self.room.lot_id != self.lot_id
        ):
            await interaction.response.send_message(
                "That player has closed. Use `!status`.", ephemeral=True
            )
            return False
        if str(interaction.user.id) not in self.room.participants:
            await interaction.response.send_message(
                "Ask the host to add you before bidding.", ephemeral=True
            )
            return False
        return True

    async def on_error(self, interaction, error, item):
        if not isinstance(error, ValueError):
            log.error(
                "Auction control failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        text = (
            str(error)
            if isinstance(error, ValueError)
            else "Unable to update this auction. Please retry."
        )
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Bid next", emoji="💰", style=discord.ButtonStyle.success)
    async def bid_next(self, interaction, button):
        ensure_user_structures(str(interaction.user.id))
        self.room.bid(
            str(interaction.user.id), None, user_budgets, user_teams, lot_id=self.lot_id
        )
        await interaction.response.defer()
        await refresh_auction(self.ctx.channel.id)

    @discord.ui.button(label="Custom bid", style=discord.ButtonStyle.primary)
    async def custom_bid(self, interaction, button):
        await interaction.response.send_modal(
            BidModal(self.ctx, self.room, self.lot_id)
        )

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.secondary)
    async def pass_lot(self, interaction, button):
        finish = self.room.pass_player(str(interaction.user.id), lot_id=self.lot_id)
        await interaction.response.defer()
        if finish:
            await finalize_sale(self.ctx, self.room, self.lot_id)
        else:
            await refresh_auction(self.ctx.channel.id)

    @discord.ui.button(label="Close lot", style=discord.ButtonStyle.secondary)
    async def close_lot(self, interaction, button):
        if str(interaction.user.id) != self.room.host:
            raise ValueError("Only the host can close a lot early.")
        await interaction.response.defer()
        await finalize_sale(self.ctx, self.room, self.lot_id)


async def refresh_auction(channel_id):
    room, message = active_auctions.get(channel_id), auction_messages.get(channel_id)
    if room and message:
        try:
            await message.edit(embed=auction_embed(room))
        except discord.NotFound:
            auction_messages.pop(channel_id, None)


def commit_sale(room: AuctionRoom) -> dict | None:
    """Mutate memory, save once, and roll back if persistence fails."""
    if room.current_player is None:
        return None
    player, winner, price = (
        deepcopy(room.current_player),
        room.highest_bidder,
        room.current_price,
    )
    if winner is None:
        room.unsold.append(player)
        room.close_lot()
        return {"player": player, "winner": None, "price": 0}
    missing = object()
    before_budget = user_budgets.get(winner, missing)
    before_team = deepcopy(user_teams[winner]) if winner in user_teams else missing
    before_stats = deepcopy(user_stats[winner]) if winner in user_stats else missing
    ensure_user_structures(winner)
    if (
        winner not in room.participants
        or user_budgets[winner] < price
        or len(user_teams[winner]) >= MAX_SQUAD
    ):
        raise ValueError("The winning bid is no longer valid; the lot remains open.")
    if any(
        player_key(existing) == player_key(player) for existing in user_teams[winner]
    ):
        raise ValueError("The winner already owns this player; the lot remains open.")
    user_budgets[winner] -= price
    user_teams[winner].append(
        {**player, "price": price, "set": available_sets.get(room.set_key, "Custom")}
    )
    user_stats[winner]["money_spent"] += price
    user_stats[winner]["most_expensive"] = max(
        user_stats[winner]["most_expensive"], price
    )
    if not save_data():
        if before_budget is missing:
            user_budgets.pop(winner, None)
        else:
            user_budgets[winner] = before_budget
        if before_team is missing:
            user_teams.pop(winner, None)
        else:
            user_teams[winner] = before_team
        if before_stats is missing:
            user_stats.pop(winner, None)
        else:
            user_stats[winner] = before_stats
        raise ValueError(
            "The save failed, so no money changed hands. Retry closing the lot."
        )
    result = {"player": player, "winner": winner, "price": price}
    room.sold.add(player_key(player))
    room.last_sale = deepcopy(result)
    room.close_lot()
    return result


async def finalize_sale(ctx, room, lot_id):
    if active_auctions.get(ctx.channel.id) is not room or room.lot_id != lot_id:
        return
    result = commit_sale(room)
    if result is None:
        return
    task = auction_tasks.pop(ctx.channel.id, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    view = auction_views.pop(ctx.channel.id, None)
    if view:
        for child in view.children:
            child.disabled = True
        view.stop()
    if result["winner"]:
        embed = card(
            "✅ Player sold",
            f"**{safe_name(result['player']['name'])}** joins <@{result['winner']}> for **{format_currency(result['price'])}**.",
        )
        embed.add_field(
            name="Wallet", value=format_currency(user_budgets[result["winner"]])
        )
        embed.add_field(
            name="Squad", value=f"{len(user_teams[result['winner']])}/{MAX_SQUAD}"
        )
    else:
        embed = card(
            "↩ Player unsold",
            f"**{safe_name(result['player']['name'])}** received no bids. Use `!retry` to offer it again.",
        )
    message = auction_messages.pop(ctx.channel.id, None)
    try:
        if message:
            await message.edit(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)
    except discord.HTTPException:
        await ctx.send(embed=embed)


async def lot_clock(ctx, room, lot_id):
    try:
        while active_auctions.get(ctx.channel.id) is room and room.lot_id == lot_id:
            remaining = room.deadline - time.monotonic()
            if remaining <= 0:
                await finalize_sale(ctx, room, lot_id)
                return
            await asyncio.sleep(min(remaining, 1))
    except asyncio.CancelledError:
        return
    except (ValueError, discord.HTTPException):
        log.exception("Auction timer failed")


async def offer_player(ctx, room, player):
    room.start_lot(player)
    view = AuctionControls(ctx, room)
    auction_views[ctx.channel.id] = view
    auction_tasks[ctx.channel.id] = asyncio.create_task(
        lot_clock(ctx, room, room.lot_id)
    )
    auction_messages[ctx.channel.id] = await ctx.send(
        embed=auction_embed(room), view=view
    )


@bot.command()
@commands.guild_only()
async def startauction(ctx, members: commands.Greedy[discord.Member]):
    """Open an auction lobby. Example: !startauction @friend @friend."""
    if ctx.channel.id in active_auctions:
        raise ValueError("An auction is already active in this channel.")
    managers = [ctx.author, *members]
    unique = {member.id for member in managers}
    if len(unique) > 16:
        raise ValueError("An auction supports up to 16 managers.")
    if any(member.bot for member in managers):
        raise ValueError("Bots cannot join auctions.")
    for member in managers:
        if is_user_in_any_auction(member.id):
            raise ValueError(
                f"{safe_name(member.display_name)} is already in another auction."
            )
    room = AuctionRoom(str(ctx.author.id), {str(member.id) for member in managers})
    for uid in room.participants:
        ensure_user_structures(uid)
    active_auctions[ctx.channel.id] = room
    view = SetPicker(ctx, room)
    view.message = await ctx.send(embed=lobby_embed(room), view=view)


@bot.command(name="set")
async def auction_set(ctx, key: str):
    room = require_room(ctx, host=True)
    choose_set(room, key)
    await ctx.send(embed=lobby_embed(room))


@bot.command()
async def sets(ctx):
    text = "\n".join(f"`{key}` · {label}" for key, label in available_sets.items())
    await ctx.send(
        embed=card(
            "Player collections",
            text
            + "\n\nPartial archives are browse-only; `26-27` is the complete current-season set.",
        )
    )


@bot.command()
async def participants(ctx):
    room = require_room(ctx)
    rows = [
        f"<@{uid}> · {format_currency(user_budgets.get(uid, STARTING_BUDGET))} · {len(user_teams.get(uid, []))}/{MAX_SQUAD} players"
        for uid in sorted(room.participants)
    ]
    await ctx.send(embed=card("Auction managers", "\n".join(rows)))


@bot.command()
@commands.guild_only()
async def add(ctx, member: discord.Member):
    room = require_room(ctx, host=True)
    if member.bot or is_user_in_any_auction(member.id):
        raise ValueError("That member is a bot or is already in another auction.")
    if len(room.participants) >= 16:
        raise ValueError("This auction already has 16 managers.")
    room.participants.add(str(member.id))
    ensure_user_structures(str(member.id))
    await ctx.send(f"{member.mention} joined the auction.")


@bot.command()
@commands.guild_only()
async def remove(ctx, member: discord.Member):
    room = require_room(ctx, host=True)
    uid = str(member.id)
    if uid == room.host or uid == room.highest_bidder:
        raise ValueError("The host and leading bidder cannot be removed.")
    if uid not in room.participants:
        raise ValueError("That manager is not in this auction.")
    room.participants.remove(uid)
    room.passed.discard(uid)
    await ctx.send(f"{member.mention} left; their squad and wallet remain saved.")


def create_position_command(position: str):
    @bot.command(name=position)
    async def nominate_position(ctx):
        room = require_room(ctx, host=True)
        if not room.set_key:
            raise ValueError("Choose a collection first with `!set 26-27`.")
        excluded = room.sold | {player_key(p) for p in room.unsold}
        excluded |= {
            player_key(p) for uid in room.participants for p in user_teams.get(uid, [])
        }
        candidates = [
            p
            for p in player_pool(room.set_key, position)
            if player_key(p) not in excluded
        ]
        if not candidates:
            raise ValueError(
                f"No unoffered {position.upper()} players remain in this collection."
            )
        await offer_player(ctx, room, random.choice(candidates))


for _position in available_positions:
    create_position_command(_position)


@bot.command()
async def nominate(ctx, position: str, amount: str, *, player_name: str):
    """Offer a custom player with an explicit position and opening price."""
    room = require_room(ctx, host=True)
    position = position.lower()
    if position not in available_positions:
        raise ValueError("Use one of: " + ", ".join(available_positions))
    if not 2 <= len(player_name.strip()) <= 80:
        raise ValueError("Player names must be 2–80 characters.")
    price = parse_currency(amount, maximum=50_000_000)
    if price < 1_000_000:
        raise ValueError("Custom opening prices must be between $1m and $50m.")
    player = normalize_player(
        {"name": player_name.strip(), "position": position, "base_price": price}
    )
    if any(
        player_key(player) == player_key(existing)
        for uid in room.participants
        for existing in user_teams.get(uid, [])
    ):
        raise ValueError("A manager in this auction already owns that player.")
    await offer_player(ctx, room, player)


@bot.command()
async def bid(ctx, amount: str | None = None):
    room = require_room(ctx)
    ensure_user_structures(str(ctx.author.id))
    value = room.bid(
        str(ctx.author.id),
        parse_currency(amount) if amount else None,
        user_budgets,
        user_teams,
    )
    await refresh_auction(ctx.channel.id)
    if ctx.channel.id not in auction_messages:
        await ctx.send(f"{ctx.author.mention} leads at {format_currency(value)}.")


@bot.command(name="pass")
async def pass_lot(ctx):
    room = require_room(ctx)
    finish = room.pass_player(str(ctx.author.id))
    if finish:
        await finalize_sale(ctx, room, room.lot_id)
    else:
        await refresh_auction(ctx.channel.id)


@bot.command()
async def retry(ctx):
    room = require_room(ctx, host=True)
    if room.current_player:
        raise ValueError("Finish the current lot first.")
    if not room.unsold:
        raise ValueError("There are no unsold players to retry.")
    await offer_player(ctx, room, room.unsold.pop(0))


@bot.command()
async def sold(ctx):
    room = require_room(ctx, host=True)
    if not room.current_player:
        raise ValueError("No player is currently being auctioned.")
    await finalize_sale(ctx, room, room.lot_id)


@bot.command()
async def unsold(ctx):
    room = require_room(ctx, host=True)
    if not room.current_player:
        raise ValueError("No player is currently being auctioned.")
    if room.highest_bidder:
        raise ValueError("A winning bid is binding; use `!sold`.")
    await finalize_sale(ctx, room, room.lot_id)


@bot.command()
async def status(ctx):
    await ctx.send(embed=auction_embed(require_room(ctx)))


@bot.command()
async def timer(ctx, seconds: int):
    room = require_room(ctx, host=True)
    if room.current_player:
        raise ValueError("Change the clock between lots.")
    if not 10 <= seconds <= 120:
        raise ValueError("Choose a bid clock from 10 to 120 seconds.")
    room.timer = seconds
    await ctx.send(f"Bid clock set to {seconds} seconds.")


@bot.command()
async def endauction(ctx):
    room = require_room(ctx, host=True)
    if room.current_player:
        raise ValueError("Resolve the current lot before ending the auction.")
    active_auctions.pop(ctx.channel.id, None)
    task = auction_tasks.pop(ctx.channel.id, None)
    if task:
        task.cancel()
    view = auction_views.pop(ctx.channel.id, None)
    if view:
        view.stop()
    auction_messages.pop(ctx.channel.id, None)
    await ctx.send(
        embed=card(
            "Auction complete",
            "Squads, wallets and lineups remain saved. Use `!setlineup` when you are ready to play.",
        )
    )


async def housekeeping():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(30)
        now = time.monotonic()
        for channel_id, room in list(active_auctions.items()):
            if room.current_player or now - room.last_activity < 900:
                continue
            active_auctions.pop(channel_id, None)
            task = auction_tasks.pop(channel_id, None)
            if task:
                task.cancel()
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(
                        "The idle auction lobby closed. Everyone's squads and wallets were kept."
                    )
                except discord.HTTPException:
                    log.warning("Could not announce idle auction close: %s", channel_id)


def get_lineup(uid: int | str, name: str | None = None) -> dict:
    uid = str(uid)
    slot = name or active_lineups.get(uid, "main")
    lineup = user_lineups.get(uid, {}).get(slot)
    if not lineup:
        raise ValueError(f"<@{uid}> has no saved lineup. Use `!setlineup` first.")
    errors = validate_lineup(lineup.get("players", []), lineup.get("formation", ""))
    if errors:
        raise ValueError(f"Lineup `{slot}` needs fixing: {'; '.join(errors[:3])}")
    return deepcopy(lineup)


def lineup_embed(uid: str, lineup: dict, title: str = "Starting XI") -> discord.Embed:
    embed = card(
        f"⚽ {title}",
        f"<@{uid}> · **{lineup['formation']}** · {lineup.get('tactic', 'Balanced')} · {team_strength(lineup):.1f} OVR",
    )
    groups = (
        ("Goalkeeper", {"gk"}),
        ("Defence", {"cb", "lb", "rb"}),
        ("Midfield", {"cm", "cam"}),
        ("Attack", {"st", "lw", "rw"}),
    )
    for label, positions in groups:
        rows = [
            f"`{p.get('assigned_position', p['position']).upper():3}` **{safe_name(p['name'])}** · {p.get('rating', '—')}"
            for p in lineup["players"]
            if p.get("assigned_position", p["position"]).lower() in positions
        ]
        if rows:
            embed.add_field(name=label, value="\n".join(rows)[:1024], inline=False)
    return embed


class LineupBuilder(OwnedView):
    def __init__(self, ctx, name: str):
        super().__init__(ctx.author.id, timeout=600)
        self.ctx, self.uid, self.name = ctx, str(ctx.author.id), name
        self.formation, self.tactic, self.players = "4-3-3", "Balanced", []
        self.render_settings()

    def preview(self):
        chosen = "\n".join(
            f"`{p.get('assigned_position', p['position']).upper()}` {safe_name(p['name'])}"
            for p in self.players
        )
        description = (
            chosen
            or "Choose a formation and tactic, then auto-pick the best legal XI from your squad."
        )
        embed = card(
            f"Build `{safe_name(self.name)}` · {len(self.players)}/11", description
        )
        embed.add_field(name="Shape", value=f"{self.formation} · {self.tactic}")
        embed.set_footer(
            text="Only you can use these controls · save commits the XI · editor expires after 10 minutes"
        )
        return embed

    def render_settings(self):
        self.clear_items()
        formation = discord.ui.Select(
            placeholder="Formation",
            options=[
                discord.SelectOption(
                    label=value, value=value, default=value == self.formation
                )
                for value in FORMATIONS
            ],
        )

        async def picked_formation(interaction):
            self.formation = interaction.data["values"][0]
            self.players = []
            self.render_settings()
            await interaction.response.edit_message(embed=self.preview(), view=self)

        formation.callback = picked_formation
        self.add_item(formation)
        tactic = discord.ui.Select(
            placeholder="Tactic",
            options=[
                discord.SelectOption(
                    label=value, value=value, default=value == self.tactic
                )
                for value in available_tactics
            ],
        )

        async def picked_tactic(interaction):
            self.tactic = interaction.data["values"][0]
            self.render_settings()
            await interaction.response.edit_message(embed=self.preview(), view=self)

        tactic.callback = picked_tactic
        self.add_item(tactic)
        auto = discord.ui.Button(
            label="Auto-pick best XI", emoji="✨", style=discord.ButtonStyle.success
        )

        async def auto_pick(interaction):
            self.players = auto_lineup(
                user_teams.get(self.uid, []), self.formation, self.tactic
            )["players"]
            self.render_save()
            await interaction.response.edit_message(embed=self.preview(), view=self)

        auto.callback = auto_pick
        self.add_item(auto)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def cancel_edit(interaction):
            self.stop()
            lineup_views.pop(self.uid, None)
            await interaction.response.edit_message(
                content="Lineup editor closed.", embed=None, view=None
            )

        cancel.callback = cancel_edit
        self.add_item(cancel)

    def render_save(self):
        self.clear_items()
        save = discord.ui.Button(
            label="Save starting XI", emoji="✅", style=discord.ButtonStyle.success
        )

        async def save_lineup(interaction):
            errors = validate_lineup(
                self.players, self.formation, squad=user_teams.get(self.uid, [])
            )
            if errors:
                raise ValueError("; ".join(errors))
            ensure_user_structures(self.uid)
            before = (deepcopy(user_lineups[self.uid]), active_lineups[self.uid])
            user_lineups[self.uid][self.name] = {
                "players": deepcopy(self.players),
                "formation": self.formation,
                "tactic": self.tactic,
            }
            active_lineups[self.uid] = self.name
            if not save_data():
                user_lineups[self.uid], active_lineups[self.uid] = before
                raise ValueError("Unable to save your lineup; nothing was changed.")
            self.stop()
            lineup_views.pop(self.uid, None)
            await interaction.response.edit_message(
                embed=lineup_embed(
                    self.uid, user_lineups[self.uid][self.name], "Starting XI saved"
                ),
                view=None,
            )

        save.callback = save_lineup
        self.add_item(save)
        back = discord.ui.Button(
            label="Change settings", style=discord.ButtonStyle.secondary
        )

        async def back_settings(interaction):
            self.players = []
            self.render_settings()
            await interaction.response.edit_message(embed=self.preview(), view=self)

        back.callback = back_settings
        self.add_item(back)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def cancel_save(interaction):
            self.stop()
            lineup_views.pop(self.uid, None)
            await interaction.response.edit_message(
                content="Lineup editor closed.", embed=None, view=None
            )

        cancel.callback = cancel_save
        self.add_item(cancel)


@bot.command()
async def setlineup(ctx, lineup_name: str = "main"):
    uid = str(ctx.author.id)
    ensure_user_structures(uid)
    if lineup_name == "draft":
        raise ValueError(
            "`draft` is reserved for Draft Clash. Choose another lineup name."
        )
    if not 1 <= len(lineup_name) <= 24 or not all(
        char.isalnum() or char in "-_" for char in lineup_name
    ):
        raise ValueError(
            "Lineup names must be 1–24 characters using letters, numbers, `-` or `_`."
        )
    if len(user_teams[uid]) < 11:
        raise ValueError(
            "You need 11 owned players first. Try `!quickmatch @friend` for a free XI."
        )
    if lineup_name not in user_lineups[uid] and len(user_lineups[uid]) >= 10:
        raise ValueError("You can save up to 10 lineups.")
    old = lineup_views.get(uid)
    if old:
        old.stop()
    view = LineupBuilder(ctx, lineup_name)
    lineup_views[uid] = view
    view.message = await ctx.send(embed=view.preview(), view=view)


@bot.command()
async def autolineup(ctx, formation: str = "4-3-3", lineup_name: str = "main"):
    uid = str(ctx.author.id)
    ensure_user_structures(uid)
    if formation not in FORMATIONS:
        raise ValueError("Unknown formation. Use `!footy team` for supported shapes.")
    lineup = auto_lineup(user_teams[uid], formation, "Balanced")
    if lineup_name == "draft":
        raise ValueError("`draft` is reserved for Draft Clash.")
    before = (deepcopy(user_lineups[uid]), active_lineups[uid])
    user_lineups[uid][lineup_name] = lineup
    active_lineups[uid] = lineup_name
    if not save_data():
        user_lineups[uid], active_lineups[uid] = before
        raise ValueError("Unable to save your lineup; nothing was changed.")
    await ctx.send(embed=lineup_embed(uid, lineup, "Auto-picked XI saved"))


@bot.command()
async def viewlineup(ctx, lineup_name: str | None = None):
    await ctx.send(
        embed=lineup_embed(str(ctx.author.id), get_lineup(ctx.author.id, lineup_name))
    )


@bot.command()
async def lineups(ctx):
    uid = str(ctx.author.id)
    rows = [
        f"{'▶' if name == active_lineups.get(uid, 'main') else '•'} **{safe_name(name)}** · {len(data.get('players', []))}/11 · {data.get('formation', 'unset')}"
        for name, data in user_lineups.get(uid, {}).items()
    ]
    await ctx.send(
        embed=card(
            "Your saved lineups",
            "\n".join(rows) or "No saved lineups yet. Use `!setlineup`.",
        )
    )


@bot.command()
async def switchlineup(ctx, lineup_name: str):
    uid = str(ctx.author.id)
    get_lineup(uid, lineup_name)
    previous = active_lineups.get(uid, "main")
    active_lineups[uid] = lineup_name
    if not save_data():
        active_lineups[uid] = previous
        raise ValueError("Unable to save that change; nothing was changed.")
    await ctx.send(f"Active lineup is now **{safe_name(lineup_name)}**.")


@bot.command()
async def deletelineup(ctx, lineup_name: str):
    uid = str(ctx.author.id)
    if lineup_name == "draft":
        raise ValueError("Draft Clash owns the `draft` lineup.")
    if lineup_name not in user_lineups.get(uid, {}):
        raise ValueError("That lineup does not exist.")
    before = (deepcopy(user_lineups[uid]), active_lineups.get(uid, "main"))
    del user_lineups[uid][lineup_name]
    if active_lineups.get(uid) == lineup_name:
        active_lineups[uid] = next(iter(user_lineups[uid]), "main")
    if not save_data():
        user_lineups[uid], active_lineups[uid] = before
        raise ValueError("Unable to save; your lineup was restored.")
    await ctx.send(
        f"Deleted lineup **{safe_name(lineup_name)}**. Owned players remain in your squad."
    )


@bot.command(aliases=["squad"])
async def myplayers(ctx):
    uid = str(ctx.author.id)
    ensure_user_structures(uid)
    rows = [
        f"`{p.get('position', '?').upper():3}` **{safe_name(p['name'])}** · {p.get('rating', '—')} OVR · {format_currency(p.get('price', p.get('base_price', 0)))}"
        for p in user_teams[uid]
    ]
    embed = card(
        f"Your squad · {len(rows)}/{MAX_SQUAD}",
        "\n".join(rows)[:4000]
        or "No signings yet. Start an auction or play a free Quick Match.",
    )
    embed.add_field(name="Wallet", value=format_currency(user_budgets[uid]))
    await ctx.send(embed=embed)


@bot.command()
async def budget(ctx):
    uid = str(ctx.author.id)
    ensure_user_structures(uid)
    await ctx.send(
        embed=card(
            "Your transfer wallet",
            f"**{format_currency(user_budgets[uid])}** remaining\n{MAX_SQUAD - len(user_teams[uid])} squad places open",
        )
    )


class BattleInvite(OwnedView):
    def __init__(self, ctx, opponent, first, second):
        super().__init__(opponent.id, timeout=90)
        self.ctx, self.opponent, self.first, self.second = ctx, opponent, first, second
        self.finished = False

    @property
    def key(self):
        return (
            self.ctx.channel.id,
            min(self.ctx.author.id, self.opponent.id),
            max(self.ctx.author.id, self.opponent.id),
        )

    async def on_timeout(self):
        battle_invites.pop(self.key, None)
        await super().on_timeout()

    @discord.ui.button(
        label="Accept match", emoji="⚽", style=discord.ButtonStyle.success
    )
    async def accept(self, interaction, button):
        if self.finished:
            raise ValueError("This match has already finished.")
        self.finished = True
        self.stop()
        battle_invites.pop(self.key, None)
        result = simulate_game(self.first, self.second)
        first_id, second_id = str(self.ctx.author.id), str(self.opponent.id)
        for uid in (first_id, second_id):
            ensure_user_structures(uid)
        before = {uid: deepcopy(user_stats[uid]) for uid in (first_id, second_id)}
        goals = result["goals"]
        if goals[0] == goals[1]:
            user_stats[first_id]["draws"] += 1
            user_stats[second_id]["draws"] += 1
        else:
            winner = first_id if goals[0] > goals[1] else second_id
            loser = second_id if winner == first_id else first_id
            user_stats[winner]["wins"] += 1
            user_stats[loser]["losses"] += 1
        saved = save_data()
        if not saved:
            for uid, stats in before.items():
                user_stats[uid] = stats
        description = f"**{safe_name(self.ctx.author.display_name)}  {goals[0]} – {goals[1]}  {safe_name(self.opponent.display_name)}**\n"
        description += f"{self.first['formation']} vs {self.second['formation']} · {self.first.get('tactic', 'Balanced')} vs {self.second.get('tactic', 'Balanced')}"
        embed = card("🏁 Full time", description)
        embed.add_field(
            name="Expected goals",
            value=f"{result['expected_goals'][0]:.2f} – {result['expected_goals'][1]:.2f}",
        )
        if not saved:
            embed.add_field(
                name="Stats",
                value="The match played, but the save failed; records were rolled back.",
            )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction, button):
        self.finished = True
        self.stop()
        battle_invites.pop(self.key, None)
        await interaction.response.edit_message(
            content="Match declined.", embed=None, view=None
        )


@bot.command()
@commands.guild_only()
@commands.cooldown(1, 20, commands.BucketType.user)
async def battle(ctx, opponent: discord.Member):
    if opponent.bot or opponent.id == ctx.author.id:
        raise ValueError("Choose another human manager.")
    key = (
        ctx.channel.id,
        min(ctx.author.id, opponent.id),
        max(ctx.author.id, opponent.id),
    )
    existing = battle_invites.get(key)
    if existing:
        raise ValueError("That manager already has a pending match invitation here.")
    first, second = get_lineup(ctx.author.id), get_lineup(opponent.id)
    view = BattleInvite(ctx, opponent, first, second)
    battle_invites[key] = view
    view.message = await ctx.send(
        embed=card(
            "⚔ Match invitation",
            f"{ctx.author.mention} challenges {opponent.mention}.\nBoth saved XIs are locked for this match. Accept within 90 seconds.",
        ),
        view=view,
    )


class MarketView(OwnedView):
    def __init__(self, ctx, players, set_key, position=None):
        super().__init__(ctx.author.id, timeout=300)
        self.players, self.set_key, self.position, self.page = (
            players,
            set_key,
            position,
            0,
        )

    def embed(self):
        rows = self.players[self.page * 10 : (self.page + 1) * 10]
        title = f"🔎 {available_sets[self.set_key]} market"
        if self.position:
            title += f" · {self.position.upper()}"
        text = "\n".join(
            f"`{p['position'].upper()}` **{safe_name(p['name'])}** · {p.get('rating', '—')} OVR · {format_currency(p['base_price'])}"
            for p in rows
        )
        embed = card(title, text or "No players match this filter.")
        embed.set_footer(
            text=f"Page {self.page + 1}/{max(1, (len(self.players) + 9) // 10)} · Game prices are separate from real transfer valuations"
        )
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        self.page = min(max(0, (len(self.players) - 1) // 10), self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)


@bot.command()
async def market(ctx, set_key: str = "26-27", position: str | None = None):
    set_key = set_key.lower()
    position = position.lower() if position else None
    if set_key not in available_sets or (
        position and position not in available_positions
    ):
        raise ValueError("Usage: `!market [26-27] [st]`. Use `!sets` for collections.")
    players = sorted(
        player_pool(set_key, position),
        key=lambda player: (-player.get("rating", 0), player["name"]),
    )
    view = MarketView(ctx, players, set_key, position)
    view.message = await ctx.send(embed=view.embed(), view=view)


HELP = {
    "home": (
        "⚽ Football Auction",
        "**Build. Bid. Play.**\n\n`!startauction @friends` · open a lobby\n`!quickmatch @friend` · free equal-XI match\n`!penalties @friend` · interactive shootout\n`!draftclash start 26-27` · snake draft\n`!league create` · round-robin season\n\nBrowse a category below for the full control panel.",
    ),
    "auction": (
        "🔨 Auction night",
        "`!startauction @friends` · lobby\n`!set 26-27` · choose the current season\n`!st` / `!cm` / `!gk` · nominate a role\n`!bid [25m]` / `!pass` · bid or pass\n`!sold` / `!unsold` / `!retry` · resolve lots\n`!nominate st 10m Player Name` · custom lot\n`!participants` / `!status` / `!timer 30`\n`!endauction` · close after the current lot\n\nOpening bid equals the card's base price. Increments are $0.5m below $10m, $1m below $30m and $2m above that.",
    ),
    "team": (
        "📋 Club management",
        "`!myplayers` / `!budget` · squad and wallet\n`!market [26-27] [st]` · scout the roster\n`!setlineup [name]` · interactive XI builder\n`!autolineup [formation] [name]` · save best legal XI\n`!viewlineup` / `!lineups` · inspect saved teams\n`!switchlineup name` / `!deletelineup name`\n`!battle @friend` · consent-based match\n\nEvery formation is validated as exactly 11 legal slots. Player ability comes from ratings, never from what a manager paid.",
    ),
    "gamemodes": (
        "🎮 Match centre",
        "`!quickmatch @friend [26-27]` · free equal-XI match\n`!penalties @friend` · private shot and dive choices\n`!draftclash start 26-27` → `join` → `begin` → `pick 1–4`\n`!koth start auction` → `join` → `challenge`\n`!league create` → `join` → `start` → `next`\n\nAll invitations, turns and buttons are owner-checked and expire cleanly. These modes never change auction wallets or owned squads.",
    ),
    "leaderboards": (
        "🏆 Competition records",
        "`!leaderboard` · saved-XI match results\n`!draftclashleaderboard` · draft titles\n`!kothleaderboard` · longest reigns\n`!league table` · current season table",
    ),
}


class HelpView(OwnedView):
    def __init__(self, owner_id):
        super().__init__(owner_id, timeout=300)
        select = discord.ui.Select(
            placeholder="Explore the club",
            options=[
                discord.SelectOption(label=HELP[key][0], value=key) for key in HELP
            ],
        )

        async def selected(interaction):
            title, description = HELP[interaction.data["values"][0]]
            await interaction.response.edit_message(
                embed=card(title, description), view=self
            )

        select.callback = selected
        self.add_item(select)


@bot.command(aliases=["help", "menu"])
async def footy(ctx, category: str = "home"):
    category = category.lower()
    if category not in HELP:
        raise ValueError("Choose `auction`, `team`, `gamemodes` or `leaderboards`.")
    title, description = HELP[category]
    view = HelpView(ctx.author.id)
    view.message = await ctx.send(embed=card(title, description), view=view)


@bot.command(aliases=["rankteams"])
async def leaderboard(ctx):
    rows = sorted(
        user_stats.items(),
        key=lambda item: (
            item[1].get("wins", 0) * 3 + item[1].get("draws", 0),
            item[1].get("wins", 0),
        ),
        reverse=True,
    )[:10]
    text = "\n".join(
        f"**{index}.** <@{uid}> · {stats.get('wins', 0)}W / {stats.get('draws', 0)}D / {stats.get('losses', 0)}L"
        for index, (uid, stats) in enumerate(rows, 1)
    )
    await ctx.send(
        embed=card("🏆 Match leaderboard", text or "Play a match to get on the board.")
    )


@bot.command()
async def events(ctx):
    await ctx.send(
        "Live competitions are available from `!footy gamemodes`. Results are recorded by each mode's own table."
    )


@bot.command()
async def viewteam(ctx):
    """Compatibility shortcut for a user's Draft Clash team."""
    command = bot.get_command("draftclash")
    await command.callback(ctx, "team", argument=None)


@bot.command()
async def end(ctx, gamemode: str | None = None):
    """Compatibility shortcut for closing auction, draft, or KoTH sessions."""
    mode = (gamemode or "auction").lower()
    if mode in ("auction", "auctioneer"):
        await endauction.callback(ctx)
    elif mode in ("draft", "draftclash"):
        await bot.get_command("draftclash").callback(ctx, "end", argument=None)
    elif mode in ("koth", "hill"):
        await bot.get_command("koth").callback(ctx, "end")
    else:
        raise ValueError("Use `!end auction`, `!end draftclash`, or `!end koth`.")


@bot.command(name="koth_list")
async def koth_list(ctx, mode: str | None = None):
    """Compatibility shortcut for the active King of the Hill session."""
    if mode and mode.lower() not in ("auction", "draft"):
        raise ValueError("Use `!koth_list auction` or `!koth_list draft`.")
    await bot.get_command("koth").callback(ctx, "status")


@bot.command(name="koth_add")
@commands.guild_only()
async def koth_add(ctx, *members: discord.Member):
    """Compatibility help for the old KoTH participant command."""
    if members:
        mentions = " ".join(member.mention for member in members)
        await ctx.send(
            f"KoTH now uses self-serve joins. Ask {mentions} to use `!koth join`."
        )
    else:
        await ctx.send(
            "KoTH now uses `!koth join`; use `!challenge` to play the current king."
        )


@bot.command(name="koth_lineup")
@commands.guild_only()
async def koth_lineup(ctx):
    """Compatibility shortcut for viewing the submitted KoTH XI."""
    await bot.get_command("koth").callback(ctx, "team")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    room = active_auctions.get(message.channel.id)
    shortcut = message.content.strip().lower()
    if (
        room
        and room.set_key is None
        and str(message.author.id) == room.host
        and shortcut in available_sets
    ):
        try:
            choose_set(room, shortcut)
            await message.channel.send(embed=lobby_embed(room))
        except ValueError as error:
            await message.channel.send(str(error))
        return
    await bot.process_commands(message)


@bot.event
async def on_ready():
    log.info("Connected as %s (%s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Game(name="!footy | Football Auction"))


@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        message = f"Try again in {error.retry_after:.0f} seconds."
    elif isinstance(error, commands.MissingRequiredArgument):
        message = f"Missing `{error.param.name}`. Use `!footy` for examples."
    elif isinstance(error, commands.BadArgument):
        message = (
            "I couldn't read that argument. Check the mention or number and try again."
        )
    elif isinstance(error, commands.NoPrivateMessage):
        message = "Use this command in a server channel."
    elif isinstance(error, (ValueError, commands.CheckFailure)):
        message = str(error)
    elif isinstance(error, discord.Forbidden):
        message = "I need permission to send messages and embed links in this channel."
    else:
        log.error(
            "Command %s failed",
            getattr(ctx, "command", None),
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Something went wrong. The error was logged; please retry."
    try:
        await ctx.send(
            embed=card("Unable to do that yet", message, color=discord.Color.orange())
        )
    except discord.HTTPException:
        log.warning("Could not send command error in channel %s", ctx.channel.id)


# These modules register their own commands against this bot.  They receive the
# module namespace so their pure modes can use the same saved squads safely.
from classic_modes import setup_classic_modes
from game_modes import setup_game_modes

game_mode_manager = setup_game_modes(bot, sys.modules[__name__])
classic_mode_manager = setup_classic_modes(bot, sys.modules[__name__])


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not load_data():
        raise SystemExit("Saved state could not be loaded; refusing to overwrite it.")
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Set DISCORD_BOT_TOKEN in .env or your host environment before starting the bot."
        )
    # Keep-alive is opt-in: importing the bot must not start a second Flask server.
    if os.getenv("ENABLE_KEEP_ALIVE", "false").lower() == "true":
        from keep_alive import keep_alive

        keep_alive()
    bot.run(token)


if __name__ == "__main__":
    main()
