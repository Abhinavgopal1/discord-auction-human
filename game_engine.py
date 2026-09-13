"""Pure football rules shared by Discord modes; no Discord or network required.

Ratings and auction base prices are game balancing values, not market valuations.
Purchased prices are deliberately excluded from ability calculations.
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

MIN_BASE_PRICE = 1_000_000
MAX_BASE_PRICE = 50_000_000
MAX_CURRENCY = 1_000_000_000
POSITIONS = ("gk", "cb", "lb", "rb", "cm", "cam", "lw", "rw", "st")
TACTICS = ("Balanced", "Attacking", "Defensive")
FORMATIONS = {
    "4-4-2": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 2, "lw": 1, "rw": 1, "st": 2},
    "4-3-3": {
        "gk": 1,
        "cb": 2,
        "lb": 1,
        "rb": 1,
        "cm": 2,
        "cam": 1,
        "lw": 1,
        "rw": 1,
        "st": 1,
    },
    "4-2-3-1": {
        "gk": 1,
        "cb": 2,
        "lb": 1,
        "rb": 1,
        "cm": 2,
        "cam": 1,
        "lw": 1,
        "rw": 1,
        "st": 1,
    },
    "3-5-2": {"gk": 1, "cb": 3, "cm": 2, "cam": 1, "lw": 1, "rw": 1, "st": 2},
    "3-4-3": {"gk": 1, "cb": 3, "lb": 1, "rb": 1, "cm": 2, "lw": 1, "rw": 1, "st": 1},
    "5-4-1": {"gk": 1, "cb": 3, "lb": 1, "rb": 1, "cm": 2, "lw": 1, "rw": 1, "st": 1},
    "5-3-2": {"gk": 1, "cb": 3, "lb": 1, "rb": 1, "cm": 2, "cam": 1, "st": 2},
    "4-1-4-1": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 3, "lw": 1, "rw": 1, "st": 1},
    "4-5-1": {
        "gk": 1,
        "cb": 2,
        "lb": 1,
        "rb": 1,
        "cm": 2,
        "cam": 1,
        "lw": 1,
        "rw": 1,
        "st": 1,
    },
    "4-3-1-2": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 3, "cam": 1, "st": 2},
    "3-4-1-2": {"gk": 1, "cb": 3, "cm": 2, "cam": 1, "lw": 1, "rw": 1, "st": 2},
    "3-1-4-2": {"gk": 1, "cb": 3, "cm": 3, "lw": 1, "rw": 1, "st": 2},
    "4-2-2-2": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 2, "cam": 2, "st": 2},
    "4-1-2-1-2": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 3, "cam": 1, "st": 2},
    "4-3-2-1": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 3, "cam": 2, "st": 1},
    "3-2-3-2": {"gk": 1, "cb": 3, "cm": 2, "cam": 1, "lw": 1, "rw": 1, "st": 2},
    "3-6-1": {"gk": 1, "cb": 3, "cm": 3, "cam": 1, "lw": 1, "rw": 1, "st": 1},
    "4-2-4": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 2, "lw": 1, "rw": 1, "st": 2},
    "4-4-1-1": {
        "gk": 1,
        "cb": 2,
        "lb": 1,
        "rb": 1,
        "cm": 2,
        "lw": 1,
        "rw": 1,
        "cam": 1,
        "st": 1,
    },
    "4-1-3-2": {"gk": 1, "cb": 2, "lb": 1, "rb": 1, "cm": 2, "lw": 1, "rw": 1, "st": 2},
    "3-3-3-1": {"gk": 1, "cb": 3, "cm": 3, "cam": 1, "lw": 1, "rw": 1, "st": 1},
    "3-2-4-1": {"gk": 1, "cb": 3, "cm": 2, "cam": 2, "lw": 1, "rw": 1, "st": 1},
}

_ALIASES = {
    "goalkeeper": "gk",
    "keeper": "gk",
    "goalie": "gk",
    "centreback": "cb",
    "centerback": "cb",
    "lcb": "cb",
    "rcb": "cb",
    "leftback": "lb",
    "lwb": "lb",
    "leftwingback": "lb",
    "rightback": "rb",
    "rwb": "rb",
    "rightwingback": "rb",
    "cdm": "cm",
    "dm": "cm",
    "midfielder": "cm",
    "centralmidfielder": "cm",
    "defensivemidfielder": "cm",
    "attackingmidfielder": "cam",
    "am": "cam",
    "lm": "lw",
    "leftwing": "lw",
    "leftwinger": "lw",
    "leftmidfielder": "lw",
    "rm": "rw",
    "rightwing": "rw",
    "rightwinger": "rw",
    "rightmidfielder": "rw",
    "cf": "st",
    "ss": "st",
    "striker": "st",
    "forward": "st",
    "centreforward": "st",
    "centerforward": "st",
}


def normalize_position(value):
    key = re.sub(r"[\s_-]+", "", str(value).lower())
    key = _ALIASES.get(key, key)
    if key not in POSITIONS:
        raise ValueError(f"Unknown position: {value}")
    return key


def player_key(player):
    """A player cannot be fielded twice, even across editions or accent variants."""
    name = player.get("name", "") if isinstance(player, dict) else str(player)
    name = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in name if c.isalnum())


def parse_currency(value, maximum=MAX_CURRENCY):
    """Parse whole dollars or a k/m suffix without float rounding or exponent syntax."""
    if isinstance(value, bool):
        raise ValueError("Enter a positive amount, such as 5m or 500k.")
    text = str(value).strip().lower()
    if len(text) > 40:
        raise ValueError("Amount is too large.")
    match = re.fullmatch(r"\$?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)([mk]?)", text)
    if not match:
        raise ValueError("Enter a positive amount, such as 5m or 500k.")
    try:
        amount = (
            Decimal(match.group(1).replace(",", ""))
            * {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
        )
    except InvalidOperation as exc:
        raise ValueError("Invalid amount.") from exc
    if not amount.is_finite() or amount <= 0 or amount > maximum:
        raise ValueError(f"Amount must be between $1 and ${maximum:,}.")
    if amount != amount.to_integral_value():
        raise ValueError("Amount must resolve to whole dollars.")
    return int(amount)


def _valid_number(value, low, high):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def price_for_rating(rating):
    """The documented current-season game scale: 65→$1m and 94→$50m."""
    if not _valid_number(rating, 1, 99):
        raise ValueError("Rating must be a finite number from 1 to 99.")
    fraction = min(1.0, max(0.0, (rating - 65) / 29))
    millions = (
        MIN_BASE_PRICE + (MAX_BASE_PRICE - MIN_BASE_PRICE) * fraction**2
    ) / 1_000_000
    return int(round(millions) * 1_000_000)


def normalize_player(record, position=None):
    """Copy and validate a card, preserving explicit valid prices and ability ratings.

    Legacy cards have no ratings: infer a stable 60–95 rating from their base price
    or tier. Never infer ability from price/paid_price (the amount a buyer paid).
    """
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("name"), str)
        or not player_key(record)
    ):
        raise ValueError("A player needs a non-empty name.")
    result = dict(record)
    result["name"] = record["name"].strip()
    result["position"] = normalize_position(record.get("position") or position)
    raw_price = record.get("base_price")
    valid_price = (
        _valid_number(raw_price, MIN_BASE_PRICE, MAX_BASE_PRICE)
        and int(raw_price) == raw_price
    )
    rating = record.get("rating", record.get("overall"))
    if not _valid_number(rating, 1, 99):
        rating = (
            (
                65
                + 29
                * math.sqrt(
                    (raw_price - MIN_BASE_PRICE) / (MAX_BASE_PRICE - MIN_BASE_PRICE)
                )
            )
            if valid_price
            else {"A": 87, "B": 81, "C": 74}.get(
                str(record.get("tier", "C")).upper(), 74
            )
        )
    result["rating"] = round(float(rating), 1)
    result["base_price"] = (
        int(raw_price) if valid_price else price_for_rating(result["rating"])
    )
    result["tier"] = (
        "A"
        if result["base_price"] >= 40_000_000
        else "B"
        if result["base_price"] >= 25_000_000
        else "C"
    )
    raw_positions = record.get("positions", record.get("secondary_positions", []))
    if isinstance(raw_positions, str):
        raw_positions = re.split(r"[,/|]", raw_positions)
    if not isinstance(raw_positions, (list, tuple, set)):
        raw_positions = []
    positions = [result["position"]]
    for item in raw_positions:
        try:
            candidate = normalize_position(item)
        except ValueError:
            continue
        if candidate not in positions:
            positions.append(candidate)
    result["positions"] = positions
    if "assigned_position" in result:
        result["assigned_position"] = normalize_position(result["assigned_position"])
    return result


def formation_slots(formation):
    if formation not in FORMATIONS:
        raise ValueError(f"Unknown formation: {formation}")
    return [
        position
        for position, count in FORMATIONS[formation].items()
        for _ in range(count)
    ]


def validate_lineup(players, formation, squad=None):
    """Return actionable validation errors; an empty list means a legal XI."""
    if isinstance(players, dict):
        players = players.get("players", [])
    if formation not in FORMATIONS:
        return [f"Unknown formation: {formation}."]
    if not isinstance(players, (list, tuple)):
        return ["Lineup players must be a list."]
    errors = []
    if len(players) != 11:
        errors.append(f"Select exactly 11 players (currently {len(players)}).")
    seen, counts = set(), Counter()
    owned = {player_key(p) for p in squad} if squad is not None else None
    for record in players:
        try:
            card = normalize_player(record)
        except (ValueError, TypeError):
            errors.append("Lineup contains an invalid player card.")
            continue
        key = player_key(card)
        if key in seen:
            errors.append(f"{card['name']} appears more than once.")
        seen.add(key)
        if owned is not None and key not in owned:
            errors.append(f"{card['name']} is not in your squad.")
        slot = card.get("assigned_position", card["position"])
        if slot not in card["positions"]:
            errors.append(f"{card['name']} cannot play {slot.upper()}.")
        counts[slot] += 1
    for position in POSITIONS:
        need, have = FORMATIONS[formation].get(position, 0), counts[position]
        if need != have:
            errors.append(f"{position.upper()}: needs {need}, selected {have}.")
    return errors


def auto_lineup(squad, formation="4-3-3", tactic="Balanced"):
    """Maximum-rating legal XI using slot assignment, including secondary roles."""
    slots = formation_slots(formation)
    tactic = str(tactic).title()
    if tactic not in TACTICS:
        raise ValueError("Choose Balanced, Attacking, or Defensive.")
    # Group alternate cards by athlete to ensure different editions cannot be selected twice.
    grouped = {}
    for record in squad:
        card = normalize_player(record)
        grouped.setdefault(player_key(card), []).append(card)
    states = {0: (0.0, ())}
    for options in grouped.values():
        next_states = dict(states)
        for card in options:
            for mask, (score, chosen) in states.items():
                for index, slot in enumerate(slots):
                    bit = 1 << index
                    if mask & bit or slot not in card["positions"]:
                        continue
                    new_mask, new_score = mask | bit, score + card["rating"]
                    if (
                        new_mask not in next_states
                        or new_score > next_states[new_mask][0]
                    ):
                        next_states[new_mask] = (new_score, chosen + ((index, card),))
        states = next_states
    full = (1 << len(slots)) - 1
    if full not in states:
        eligible = Counter(
            position
            for options in grouped.values()
            for position in {p for card in options for p in card["positions"]}
        )
        missing = [
            f"{pos.upper()} needs {need}, available {eligible[pos]}"
            for pos, need in FORMATIONS[formation].items()
            if eligible[pos] < need
        ]
        detail = (
            "; ".join(missing) or "secondary positions compete for the same players"
        )
        raise ValueError(
            f"Cannot fill {formation}: {detail}. Try another formation or recruit players."
        )
    players = [
        dict(card, assigned_position=slots[index])
        for index, card in sorted(states[full][1], key=lambda pair: pair[0])
    ]
    return {"formation": formation, "tactic": tactic, "players": players}


def _profile(lineup):
    errors = validate_lineup(lineup.get("players", []), lineup.get("formation"))
    if errors:
        raise ValueError(" ".join(errors))
    tactic = str(lineup.get("tactic", "Balanced")).title()
    if tactic not in TACTICS:
        raise ValueError("Choose Balanced, Attacking, or Defensive.")
    attack_weights = {
        "gk": 0.03,
        "cb": 0.18,
        "lb": 0.4,
        "rb": 0.4,
        "cm": 0.7,
        "cam": 1.0,
        "lw": 1.2,
        "rw": 1.2,
        "st": 1.5,
    }
    defense_weights = {
        "gk": 1.8,
        "cb": 1.3,
        "lb": 1.0,
        "rb": 1.0,
        "cm": 0.65,
        "cam": 0.2,
        "lw": 0.1,
        "rw": 0.1,
        "st": 0.05,
    }
    cards = [normalize_player(p) for p in lineup["players"]]

    def weighted(weights):
        pairs = [
            (p["rating"], weights[p.get("assigned_position", p["position"])])
            for p in cards
        ]
        return sum(r * w for r, w in pairs) / sum(w for _, w in pairs)

    counts = FORMATIONS[lineup["formation"]]
    defenders = sum(counts.get(p, 0) for p in ("cb", "lb", "rb"))
    # More defenders reduce both teams' opportunities; tactics adjust risk, not ratings.
    shape = {3: 1.06, 4: 1.0, 5: 0.94}.get(defenders, 1.0)
    offense, exposure = {
        "Balanced": (1.0, 1.0),
        "Attacking": (1.17, 1.12),
        "Defensive": (0.84, 0.85),
    }[tactic]
    return {
        "attack": weighted(attack_weights),
        "defense": weighted(defense_weights),
        "strength": sum(p["rating"] for p in cards) / 11,
        "offense": offense * shape,
        "exposure": exposure * shape,
        "players": cards,
    }


def team_strength(lineup):
    return round(_profile(lineup)["strength"], 2)


def team_profile(lineup):
    """Expose rating-weighted attack and defense for team comparison UI."""
    profile = _profile(lineup)
    return {key: round(profile[key], 2) for key in ("attack", "defense", "strength")}


def _poisson(mean, rng):
    limit, product, count = math.exp(-mean), 1.0, 0
    while product > limit:
        product *= rng.random()
        count += 1
    return max(0, count - 1)


def simulate_game(lineup1, lineup2, seed=None, rng=None):
    """A symmetric match with reproducible optional RNG and score-consistent goals.

    1.3 expected goals at equal strength, with modest tactical/formation risk and
    a continuous rating advantage. The first argument receives no home advantage.
    """
    if seed is not None and rng is not None:
        raise ValueError("Pass a seed or an RNG, not both.")
    rng = rng if rng is not None else random.Random(seed)
    profiles = [_profile(lineup1), _profile(lineup2)]
    expected = [
        min(
            4.5,
            max(
                0.12,
                1.3
                * math.exp((profiles[i]["attack"] - profiles[1 - i]["defense"]) / 22)
                * profiles[i]["offense"]
                * profiles[1 - i]["exposure"],
            ),
        )
        for i in range(2)
    ]
    goals = [_poisson(value, rng) for value in expected]
    scorer_weights = {
        "gk": 0.01,
        "cb": 0.2,
        "lb": 0.25,
        "rb": 0.25,
        "cm": 0.5,
        "cam": 1.0,
        "lw": 1.5,
        "rw": 1.5,
        "st": 2.5,
    }
    events = []
    for team, count in enumerate(goals):
        players = profiles[team]["players"]
        weights = [
            scorer_weights[p.get("assigned_position", p["position"])] * p["rating"]
            for p in players
        ]
        for _ in range(count):
            scorer = rng.choices(players, weights=weights, k=1)[0]
            events.append(
                {
                    "minute": rng.randint(1, 90),
                    "team": team,
                    "type": "goal",
                    "player": scorer["name"],
                }
            )
    events.sort(key=lambda event: event["minute"])
    return {
        "goals": goals,
        "expected_goals": [round(x, 2) for x in expected],
        "strengths": [round(p["strength"], 2) for p in profiles],
        "winner": None if goals[0] == goals[1] else int(goals[1] > goals[0]),
        "events": events,
    }
