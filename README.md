# Football Auction Discord Bot

Build a squad through a live auction, save multiple starting XIs, and play
consent-based football games with your friends. The bot now has one consistent
state file, an interactive Discord UI, deterministic player prices, and safe
expiry/error handling.

## Start it

1. Install Python 3.12 or newer and run `python -m pip install -r requirements.txt`.
2. Create a `.env` file containing `DISCORD_BOT_TOKEN=...`.
3. Run `python bot.py`.

The message-content intent must be enabled for the bot in the Discord Developer
Portal. `ENABLE_KEEP_ALIVE=true` is optional for hosts that need the tiny Flask
health endpoint; it is disabled by default so importing the bot never starts a
second web server.

For hosting, use the included Dockerfile or a Python background worker. Follow
[the deployment guide](docs/DEPLOYMENT.md) to configure the bot secret, persistent
storage, and Discord verification.

## Main commands

`!footy` opens the interactive command menu. The menu covers:

- `!startauction @friends` → choose `26-27` → use `!st`, `!cm`, `!gk`, and the
  other position commands to nominate cards. Managers bid with `!bid 25m` or
  the buttons, and the host resolves lots with `!sold`, `!unsold`, or `!retry`.
- `!myplayers`, `!budget`, `!market [26-27] [st]`, and `!setlineup` manage squads.
  `!autolineup 4-3-3` saves the highest-rated legal XI automatically.
- `!battle @manager` sends an accept/decline invitation before a saved-XI match.
- `!quickmatch @manager` creates equally rated free XIs. `!penalties @manager`
  starts a private-choice shootout.
- `!draftclash start 26-27` → `join` → `begin` → `pick 1`; `!koth start auction`
  and `!challenge` run King of the Hill; `!league create` runs a 2–8 manager
  round robin with standings and one fixture per manager per round.

Game prices are fictional starting prices. Player ability comes from the
curated rating field, never from what a manager paid. The current-season pool,
source links, verification date, price formula, and limitations are documented
in [docs/ROSTER_SOURCES.md](docs/ROSTER_SOURCES.md).

## Data and tests

Player and manager data live in `players/` and `data/state.json`. If the old
`teams.json`, `budgets.json`, `lineups.json`, or stats files exist, they are
loaded into the new format on the first save without deleting the originals.
Saves are serialized and atomically replaced, so a failed write leaves the
previous snapshot in place.

Run the full offline regression suite with:

```text
python -m unittest discover -s tests -v
```

It covers every formation, currency validation, price independence, roster
integrity, auction/draft/KOTH ownership rules, shootouts, and league schedules.
