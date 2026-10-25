# 📸 Snipe Bot

A Discord bot that tracks "snipes" for our group. A snipe is when someone takes a photo of a group member outside of group meeting times. The sniper posts the photo in the snipes channel and tags the person they sniped. Whoever has the most points at the end of the semester wins free hotpot.

The bot keeps a live leaderboard in its own channel and tracks a few fun stats.

## How snipes are counted

- A message counts if it is posted in the **snipes channel**, has an **image attached**, and **@mentions** at least one person.
- The **sender is the sniper** and each **tagged person is sniped**. If a photo tags 3 people, that is 3 snipes.
- The sniper earns points, not the person who was sniped.
- Each sniped person has a point value (default **1**). Admins can make certain people worth more with `/setpoints`.
- Tagging yourself or a bot is ignored.
- Editing a message re-counts it. Deleting a message removes its snipes.
- Only snipes inside the **snipe period** (set with `/setperiod`) count. Dates and "days" use **Eastern time**.

## The leaderboard

One message in the leaderboard channel, edited in place (it never floods the channel). It shows:

- The snipe period being tracked
- Top 5 snipers (points and number of snipes)
- Top 5 most sniped (number of times)
- The day with the most snipes
- Who was sniped the most in a single day

Press the **📍 My Rank** button on the leaderboard (or use `/mystats`) to privately see your own rank for both sniping and being sniped.

Ties share a rank.

## Commands

Commands marked 🔒 require the **Manage Server** permission.

| Command | What it does |
|---|---|
| 🔒 `/setup snipes_channel leaderboard_channel` | Choose the two channels and post the leaderboard |
| 🔒 `/setperiod start end` | Set the snipe period, dates as `YYYY-MM-DD` (inclusive, Eastern) |
| 🔒 `/setpoints user points` | Set how many points sniping that person is worth (default 1) |
| 🔒 `/backfill` | Rescan the snipes channel history and rebuild counts |
| 🔒 `/refresh` | Force the leaderboard to update now |
| `/pointvalues` | List everyone with a non-default point value |
| `/mystats [user]` | Show your (or someone's) rank and counts |

Point changes apply to **all** snipes in the period, including past ones.

## Setup

### 1. Create the bot (Discord Developer Portal)

1. Go to the [Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. On the **Bot** tab, click **Reset Token** and copy it.
3. On the same tab, turn on **Message Content Intent** (required to see photo attachments).
4. Go to **OAuth2 → URL Generator**. Check the scopes `bot` and `applications.commands`, and these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
5. Open the generated URL and add the bot to your server.

If the bot is already in the server with fewer permissions, re-invite it with the new URL, or enable the permissions on its role (Server Settings → Roles). Also check the snipes and leaderboard channels for permission overrides that block the bot.

### 2. Install

You need Python 3.9 or newer.

```bash
pip install discord.py python-dotenv tzdata
```

On a Mac, use `python3` and `pip3` (or `python3 -m pip`).

### 3. Add your token

Create a file named `.env` in the project folder:

```
DISCORD_TOKEN=your_token_here
```

No quotes and no spaces around the `=`. **Never commit this file.**

### 4. Run it

```bash
python bot.py
```

(`python3 bot.py` on Mac/Linux.) You should see `Logged in as ...`.

### 5. Configure it in Discord (first time only)

With the bot running:

1. `/setup` with your snipes channel and leaderboard channel
2. `/setperiod` with the start and end dates
3. `/setpoints` for anyone worth more than 1 point (optional)
4. `/backfill` to count snipes that were already posted

Settings and snipes are saved in `snipes.db`, so you don't repeat this.

## Running it regularly

The bot doesn't have to run all the time. Whenever you start it, it:

1. Rescans the snipes channel for the current period
2. Adds new snipes and removes ones whose messages were deleted
3. Updates the leaderboard message

So a typical routine is:

```bash
python bot.py
# wait for "Leaderboard updated." in the terminal
# then press Ctrl+C to stop
```

While it is running it also updates live as snipes are posted, and refreshes every 10 minutes. Slash commands and the **My Rank** button only work while the bot is running.

## Making changes

- **Points, period, channels:** use the slash commands while the bot is running.
- **Code changes:** edit `bot.py`, stop the bot (Ctrl+C), and run it again. The existing leaderboard message gets updated with the new layout.
- **Fresh start:** stop the bot, delete `snipes.db`, run it, and redo the Discord setup.

## Files

| File | Purpose |
|---|---|
| `bot.py` | The whole bot |
| `.env` | Your bot token (secret, not in git) |
| `snipes.db` | SQLite database of settings and snipes (not in git, back it up occasionally) |
| `.gitignore` | Keeps `.env` and `snipes.db` out of git |

Recommended `.gitignore`:

```
.env
snipes.db
__pycache__/
venv/
.venv/
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not update leaderboard: 403 ... Missing Permissions` | The bot lacks View Channel, Send Messages, or Embed Links in the leaderboard channel. Check the role and channel overrides. |
| `PrivilegedIntentsRequired` | Turn on **Message Content Intent** in the Developer Portal's Bot tab. |
| Slash commands don't show up | Wait a minute, then restart Discord (Ctrl/Cmd+R). |
| `CERTIFICATE_VERIFY_FAILED` on Mac | Run `/Applications/Python\ 3.13/Install\ Certificates.command` (adjust the version number to yours). |
| `command not found: python` on Mac | Use `python3` instead. |
| `ModuleNotFoundError: discord` | Install with the same Python you run: `python3 -m pip install discord.py python-dotenv tzdata` |
| `LoginFailure: Improper token` | Re-copy the token into `.env`, with no quotes or extra spaces. |
| Leaderboard says nobody has sniped | Check the period dates, and that the messages have a real image attached and a real @mention (plain-text names don't count). Then run `/backfill`. |
| Leaderboard message was deleted | Run `/refresh` to post a new one. |

## Security notes

- Keep `.env` out of git. GitHub will block pushes that contain a Discord token.
- If the token is ever exposed, click **Reset Token** in the Developer Portal and update `.env`.
- The bot only needs the four permissions listed above. It does not need Administrator.