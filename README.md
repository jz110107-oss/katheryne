# ⚔️ Katheryne: Adventurer's Guild Bot

Katheryne runs our group's Adventurer's Guild. She tracks **snipes**, hosts **Donut Buddies**, posts a **Photo of the Week**, records attendance at **socials** and **office hours**, and rewards people for reading the **weekly recap**. Everything earns **Primogems** 💎, and the person with the most at the end of the semester wins free hotpot.

She keeps one live message in the Guild channel with the leaderboards, and every member can press a button to see their own **Adventurer card**.

## Ways to earn Primogems

| Activity | Reward | Rules |
|---|---|---|
| **Snipe** someone | 1 💎 (or that person's point value) | Post a photo in the snipes channel and tag who you sniped. You earn the points, not them. |
| **Donut Buddies** | 4 💎 each (dining hall) or 5 💎 each (elsewhere) | Meet your weekly buddy for a meal, post a photo tagging them in your private channel, and answer where you went. |
| **Photo of the Week** | 2 💎 | Post a photo in the photo channel. Once per person per week. |
| **Social** | 8 💎 | An admin posts a photo tagging you. Once per person per socials channel. |
| **Office hours** | 3 💎 | An admin posts a photo tagging you. Once per person per week. |
| **Weekly recap** | 1 💎 | React to the weekly recap message. Once per recap. |

Reward amounts live at the top of `bot.py` if you want to change them.

## The Adventurer's Guild message

One message in the Guild channel, edited in place (it never floods the channel). It shows:

- The season dates, this week's photo theme, whether Donut Buddies is running, and a countdown to the next weekly reset
- **Primogem leaderboard** (top 5 across everything)
- **Top snipers** (top 5, with snipe counts)
- **Most sniped** (top 5)
- The day with the most snipes, and who was sniped the most in a single day

Ties share a rank. Primogems only count inside the season set with `/setperiod`.

### Buttons

- **📜 My Adventurer Card** shows a private card starting with *"Ad astra abyssosque!"*. It has your primogems and rank, snipes and rank, times sniped and rank, and this week's **commissions**: Donut Buddies, Photo of the Week, Weekly Recap, and Office Hours, each marked ✅ or ❌. A feature that's off or not set up shows "No ___ this week".
- **🍩 Donut Buddies: Opt In/Out** toggles whether you get paired. Everyone is opted in by default, and changes apply from the next reset.

## How each feature works

### Snipes

- A message counts if it's in the **snipes channel**, has an **image attached**, and **@mentions** at least one person.
- The sender is the sniper. Each tagged person is a separate snipe, so tagging 3 people is 3 snipes.
- Tagging yourself or a bot is ignored.
- Editing a message re-counts it. Deleting it removes its snipes.
- `/setpoints` sets how many primogems sniping a particular person is worth (default 1). Changes apply to past snipes too.

### Donut Buddies

- At each weekly reset, Katheryne randomly pairs everyone who is opted in (one group of 3 if the number is odd). She avoids repeating partners when possible.
- She creates a **private channel per group** under your "Text Channels" category (or the category set in `/configure`). Only the group and roles with **Manage Server** can see it. The pair gets tagged in a welcome message there. Nobody else is pinged.
- To finish: a group member posts a **photo tagging everyone else**, then Katheryne asks **dining hall or somewhere else** with buttons. Each buddy earns +4 or +5, once per group per week.
- At the next reset the channels are deleted and new buddies are paired. If a group posted its photo but never answered the question, its channel is kept one extra week, and after that it defaults to the dining hall reward.
- `/setdonuts` sets the start and end dates. Groups form at each reset that falls inside that range.
- `/configure participant_role:@Role` limits pairing to people with that role. Without it, everyone in the server who hasn't opted out is paired, including admins.

### Photo of the Week

- At each reset Katheryne posts a new theme in the photo channel. Custom themes added with `/addtheme` are used first (in the order added), then the 20 built-in ones.
- Any image posted in that channel during the week earns 2 💎 (once per person per week). Photos from earlier weeks no longer count.
- Deleting your photo removes its reward.

### Socials

- Register each socials channel with `/addsocial #channel`. Each one is tracked separately.
- Only people with **Manage Server** can award. They post a photo and tag attendees, and each tagged person gets 8 💎 once per socials channel.
- Admins can't tag themselves. Another admin has to.
- Deleting the admin's message takes the awards back.

### Office hours

- Same as socials, in the office hours channel, but 3 💎 once per **week** (so you can earn it each week).

### Weekly recap

- Set a recap channel. A **recap** is any message posted there by someone with Manage Server.
- Any reaction to it earns 1 💎 per recap. Removing the reaction or adding more doesn't change anything, and the author can't earn from their own recap.
- Only the **current week's** recap earns primogems. Deleting the recap takes its awards back.

### The weekly reset

Donut Buddies, the Photo of the Week theme, the office hours limit, and the recap window all reset together at one time, set with `/setreset` (default **Sunday 6:00 PM Eastern**). Dates and "days" everywhere use Eastern time.

## Commands

Commands marked 🔒 need the **Manage Server** permission.

| Command | What it does |
|---|---|
| 🔒 `/setup snipes_channel guild_channel` | Choose the snipes channel and where the Guild message lives |
| 🔒 `/configure` | Set the photo, office hours, and recap channels, the donut category, and a participant role. Run it with nothing filled in to see all current settings. |
| 🔒 `/setperiod start end` | Set the season, as `YYYY-MM-DD` (inclusive, Eastern) |
| 🔒 `/setreset weekday time` | Set the weekly reset, e.g. Sunday `18:00` |
| 🔒 `/setdonuts start end` | Set the dates Donut Buddies runs |
| 🔒 `/toggle feature enabled` | Turn Donut Buddies, Photo of the Week, Weekly Recap, or Office Hours on or off |
| 🔒 `/setpoints user points` | Set what sniping someone is worth |
| 🔒 `/addsocial` / `/removesocial` | Register or unregister a socials channel |
| 🔒 `/addtheme theme` | Add a Photo of the Week theme |
| 🔒 `/givegems user amount reason` | Add or remove primogems manually (negative amounts work). Useful for fixing mistakes. |
| 🔒 `/sync` | Rescan all channels, run any due reset, and refresh the Guild |
| `/mystats [user]` | Show your (or someone's) Adventurer card |
| `/pointvalues` | List anyone not worth the default 1 |

Katheryne doesn't ping anyone, except the pair inside their private Donut Buddies channel.

## Setup

### 1. Create the bot (Discord Developer Portal)

1. Go to the [Developer Portal](https://discord.com/developers/applications) and create a **New Application** (name it Katheryne and add an avatar if you like).
2. On the **Bot** tab, click **Reset Token** and copy it.
3. On the same tab, turn on **Message Content Intent** and **Server Members Intent**.
4. Go to **OAuth2 → URL Generator**. Check the scopes `bot` and `applications.commands`, and these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Messages (to pin the current Photo of the Week prompt)
   - Manage Channels (to create and delete donut channels)
5. Open the generated URL and add the bot to your server.

If the bot is already in the server, re-invite it with the new URL or edit its role (Server Settings → Roles). If donut channel creation fails with a 403, also enable **Manage Roles**. Also check the channels for permission overrides that block the bot.

**Optional:** upload a primogem image to your server as a custom emoji named exactly `primogem`. Katheryne finds it automatically and uses 💎 until then.

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

1. `/setup` with your snipes channel and Guild channel
2. `/setperiod` with the season dates
3. `/configure` with the photo of the week, office hours, and recap channels (and the donut category or participant role if needed)
4. `/setreset` to pick your weekly reset time
5. `/setdonuts` with the dates Donut Buddies should run
6. `/addsocial` for each socials channel
7. `/setpoints` for anyone worth more than 1 point (optional)
8. `/sync` to count snipes that were already posted

Settings and data are saved in `snipes.db`, so you don't repeat this.

To test quickly: set `/setreset` to a time a few minutes ago, run `/setdonuts` with dates that include today, and check that channels appear.

## Running it occasionally

Katheryne doesn't have to run all the time. Every time you start her she:

1. Rescans every tracked channel (snipes, photos, office hours, socials, recap, and open donut channels)
2. Adds new awards and removes ones whose messages were deleted
3. Runs the weekly reset if one is due (cleaning up old donut channels, posting a new theme, pairing new buddies)
4. Refreshes the Guild message

A typical routine:

```bash
python bot.py
# wait for "Adventurer's Guild updated." in the terminal
# then press Ctrl+C to stop
```

Things to know when running occasionally:

- **Slash commands, buttons, and live updates only work while she's running.** The Guild message stays at its last state until the next run.
- **A reset happens when you start her after the reset time.** If you skip a whole week, she doesn't catch up on the missed one.
- **Recap reactions** are caught on startup, but only for the current week, plus the week that just ended if a reset is due. Discord doesn't record reaction times, so reactions found at that point are counted for that week.
- **Donut photos posted while she was off** get their dining hall/elsewhere question the next time she starts.

To keep the Guild always current and make resets happen on time, host her somewhere that stays on (a Raspberry Pi, a small VPS, or Railway/Fly.io).

## Making changes

- **Settings:** use the slash commands while the bot is running.
- **Code or rewards:** edit `bot.py`, stop the bot (Ctrl+C), and run it again. The Guild message is updated in place.
- **Fresh start:** stop the bot, delete `snipes.db`, run it, and redo the Discord setup.
- **Changing the snipes channel** with `/setup` and then syncing drops snipes recorded from the old channel.
- **Changing `/setreset` mid-week** can start a new cycle right away, which deletes the current donut channels.

## Files

| File | Purpose |
|---|---|
| `bot.py` | The whole bot |
| `.env` | Your bot token (secret, not in git) |
| `snipes.db` | SQLite database of settings, snipes, and awards (not in git, back it up occasionally) |
| `.gitignore` | Keeps `.env` and `snipes.db` out of git |
| `README.md` | This file |

Recommended `.gitignore`:

```
.env
snipes.db
__pycache__/
venv/
.venv/
```

### Saving to git

```bash
git add bot.py README.md
git commit -m "Describe what you changed"
git push
```

Check `git status` before committing and make sure `.env` and `snipes.db` aren't listed. GitHub blocks pushes that contain a Discord token. If that happens, remove the file from your commits (see the troubleshooting table) and push again.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not update the Adventurer's Guild message: 403 ... Missing Permissions` | The bot lacks View Channel, Send Messages, or Embed Links in the Guild channel. Check the role and channel overrides. |
| `Could not create donut buddy channels: 403` | Give the bot **Manage Channels** (and **Manage Roles** if it still fails). Then run `/sync`. |
| Nobody gets paired | Donut Buddies needs `/setdonuts` dates that include the current week, at least 2 opted-in people, and **Server Members Intent** turned on. |
| `PrivilegedIntentsRequired` | Turn on **Message Content Intent** and **Server Members Intent** in the Developer Portal's Bot tab. |
| Slash commands don't show up | Wait a minute, then restart Discord (Ctrl/Cmd+R). |
| A feature says "No ___ this week" | It's turned off (`/toggle`) or its channel isn't set (`/configure`). |
| `CERTIFICATE_VERIFY_FAILED` on Mac | Run `/Applications/Python\ 3.13/Install\ Certificates.command` (adjust the version number to yours). |
| `command not found: python` on Mac | Use `python3` instead. |
| `ModuleNotFoundError: discord` | Install with the same Python you run: `python3 -m pip install discord.py python-dotenv tzdata` |
| `LoginFailure: Improper token` | Re-copy the token into `.env`, with no quotes or extra spaces. |
| Leaderboard says nobody has sniped | Check the season dates, and that messages have a real image attached and a real @mention (plain-text names don't count). Then run `/sync`. |
| Guild message was deleted | Run `/sync` to post a new one. |
| Someone got points by mistake | Delete the message that awarded them (for snipes, socials, office hours, photos, recaps), or use `/givegems` with a negative amount. |
| `git push` blocked for a secret | `.env` was committed. Run `git rm --cached .env`, make sure `.gitignore` lists it, recommit, and consider resetting the token. |

## Security notes

- Keep `.env` out of git. If the token is ever exposed, click **Reset Token** in the Developer Portal and update `.env`.
- Katheryne doesn't need Administrator. The permissions listed above are enough.