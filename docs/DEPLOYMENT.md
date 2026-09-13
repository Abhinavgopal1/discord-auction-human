# Deploy the Discord bot

Use an always-running background worker on your existing host. The bot connects
out to Discord and does not need a public website or an incoming HTTP port.
Run exactly one replica: multiple processes can respond to the same commands
and overwrite each other's saved state.

## Build and startup

Connect the host to this GitHub repository and select the branch you want to
deploy. If the host supports Docker, use the included `Dockerfile` from the
repository root. It installs Python 3.12 and the dependencies, copies only the
runtime code and player lists, and starts `python bot.py`.

For a Python worker without Docker, choose Python 3.12 or newer, build with
`python -m pip install -r requirements.txt`, and start with `python bot.py`.
Configure restart-on-failure and keep the worker running continuously.

## Environment and storage

### Render free web service

For a free Render deployment, create a **Web Service** from this repository's
`main` branch, select **Docker** and **Free**, and use the root `Dockerfile`.
Set `DISCORD_BOT_TOKEN` privately and override `ENABLE_KEEP_ALIVE` to `true`.
Set the health check path to `/`. The HTTP endpoint binds to Render's `PORT`
(default `10000`); the Docker command starts the Discord bot alongside it.
Check the logs for `Connected as ...` to verify the Discord login.

Render's free web service sleeps after 15 minutes without inbound traffic.
Discord commands cannot wake a sleeping service; visit its public web URL
and allow it to start before playing. Free instances have no persistent disk:
saved squads, balances, and statistics in `/data` can be lost on a restart,
redeploy, or spin-down. Treat this configuration as a temporary demo.
See [Render's free service limits](https://render.com/docs/free).

### Always-running worker with persistent storage

Set these in the host's runtime settings:

| Setting | Value |
| --- | --- |
| `DISCORD_BOT_TOKEN` | The bot token, stored as a secret |
| `AUCTION_DATA_DIR` | `/data` |
| `ENABLE_KEEP_ALIVE` | `false` |

Attach a persistent volume at `/data` and ensure the process can write there.
The bot saves squads, wallets, lineups, and career statistics to
`/data/state.json`. An ordinary container filesystem is temporary; the mount
must survive deploys and restarts. Keep backups of this file using the host's
volume backup feature or by copying it while the bot is stopped.

The token belongs in the host's secret settings, never in GitHub, the Dockerfile,
build arguments, or an image. The Docker build excludes `.env` and runtime data.

When moving an existing installation, stop its bot process and copy its
`state.json` into the new mounted directory before starting the new worker.
For an older installation, copy its legacy JSON saves (`teams.json`,
`budgets.json`, `lineups.json`, `stats.json`, `active_lineups.json`,
`draft_clash_wins.json`, and `koth.json`, where present) into that directory.
When no `state.json` exists, they are loaded and the next save creates the new
format. Keep the original files as a backup. Stop the old deployment before
starting the new one so only one worker uses the bot token.

## Discord setup and verification

1. In the Discord Developer Portal, open the application, then its **Bot** page,
   and enable **Message Content Intent**. The bot uses commands beginning with
   `!`.
2. Install the bot in your Discord server if needed. In the channel you will use,
   allow it to view the channel, send messages, embed links, and read message
   history.
3. Deploy and check the logs for `Connected as ...`. A running container alone
   does not confirm that the bot connected to Discord.
4. Send `!footy` in that server channel. Confirm that the menu appears and its
   buttons respond. Then try `!market 26-27 st` to check the current player set.
5. After a saved squad or lineup exists, restart the worker and confirm that
   `!myplayers` or `!viewlineup` still shows it. This checks the volume mount.

If the worker reports a missing or invalid token, correct the host's secret.
If Discord rejects privileged intents, enable Message Content Intent. If it
connects but does not answer, check the intent and the bot's channel permissions.
Errors about saving or loading state need the volume path and permissions
checked; do not delete existing saves to get past an error.

## Restart behavior

Saved purchases, wallets, lineups, and career statistics reload on startup.
Live auctions, match invitations, drafts, King of the Hill sessions, and mini
leagues are held in memory and end when the process restarts. Their old buttons
will no longer work; start a fresh session after a deploy. Deploy between games
when possible.

Leave the optional Flask keep-alive endpoint disabled for a worker. It reports
HTTP availability, not the Discord connection, and is not a substitute for an
always-running service.
