import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo("America/New_York")  # "days" and the snipe period use Eastern time
DB_PATH = "snipes.db"
TOP_N = 5

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS config (
        guild_id INTEGER, key TEXT, value TEXT,
        PRIMARY KEY (guild_id, key)
    );
    CREATE TABLE IF NOT EXISTS point_values (
        guild_id INTEGER, user_id INTEGER, points INTEGER,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS snipes (
        guild_id INTEGER, message_id INTEGER, sniper_id INTEGER,
        sniped_id INTEGER, created_at TEXT,
        PRIMARY KEY (message_id, sniped_id)
    );
    """
)
db.commit()


def get_cfg(guild_id, key):
    row = db.execute(
        "SELECT value FROM config WHERE guild_id=? AND key=?", (guild_id, key)
    ).fetchone()
    return row["value"] if row else None


def set_cfg(guild_id, key, value):
    db.execute(
        "INSERT OR REPLACE INTO config (guild_id, key, value) VALUES (?, ?, ?)",
        (guild_id, key, str(value)),
    )
    db.commit()


def del_cfg(guild_id, key):
    db.execute("DELETE FROM config WHERE guild_id=? AND key=?", (guild_id, key))
    db.commit()


# ----------------------------------------------------------------------------
# Snipe period helpers (stored as YYYY-MM-DD strings, interpreted in Eastern)
# ----------------------------------------------------------------------------
def parse_date(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=TZ)


def period_bounds(guild_id):
    """Returns (start_dt, end_dt_exclusive); either can be None if unset."""
    start_s = get_cfg(guild_id, "period_start")
    end_s = get_cfg(guild_id, "period_end")
    start = parse_date(start_s) if start_s else None
    end = parse_date(end_s) + timedelta(days=1) if end_s else None
    return start, end


def period_label(guild_id) -> str:
    start_s = get_cfg(guild_id, "period_start")
    end_s = get_cfg(guild_id, "period_end")
    if not start_s and not end_s:
        return "All time (no period set)"
    return f"{start_s or 'beginning'} → {end_s or 'ongoing'} (Eastern)"


def fmt_day(d) -> str:
    return f"{d:%a, %b} {d.day}"


# ----------------------------------------------------------------------------
# Recording snipes
# ----------------------------------------------------------------------------
def has_image(message: discord.Message) -> bool:
    return any((a.content_type or "").startswith("image/") for a in message.attachments)


def record_snipe(message: discord.Message) -> int:
    """(Re)records the snipes for a message. Returns number of people sniped."""
    db.execute("DELETE FROM snipes WHERE message_id=?", (message.id,))
    count = 0
    if not message.author.bot and has_image(message):
        created = message.created_at.astimezone(timezone.utc).isoformat()
        targets = {
            u.id for u in message.mentions if not u.bot and u.id != message.author.id
        }
        for target in targets:
            db.execute(
                "INSERT OR IGNORE INTO snipes "
                "(guild_id, message_id, sniper_id, sniped_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (message.guild.id, message.id, message.author.id, target, created),
            )
            count += 1
    db.commit()
    return count


# ----------------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------------
def load_snipes(guild_id):
    """Snipes inside the current period as (sniper, sniped, eastern_date, points)."""
    start, end = period_bounds(guild_id)
    rows = db.execute(
        """
        SELECT s.sniper_id, s.sniped_id, s.created_at, COALESCE(p.points, 1) AS pts
        FROM snipes s
        LEFT JOIN point_values p
          ON p.guild_id = s.guild_id AND p.user_id = s.sniped_id
        WHERE s.guild_id = ?
        """,
        (guild_id,),
    ).fetchall()
    out = []
    for r in rows:
        t = datetime.fromisoformat(r["created_at"])
        if start and t < start:
            continue
        if end and t >= end:
            continue
        out.append((r["sniper_id"], r["sniped_id"], t.astimezone(TZ).date(), r["pts"]))
    return out


def ranked(scores: dict):
    """Competition ranking (ties share a rank). Returns [(rank, user_id, score)]."""
    items = sorted(
        ((u, s) for u, s in scores.items() if s > 0), key=lambda kv: (-kv[1], kv[0])
    )
    out = []
    for i, (uid, score) in enumerate(items):
        rank = out[-1][0] if out and out[-1][2] == score else i + 1
        out.append((rank, uid, score))
    return out


def compute_stats(guild_id):
    sniper_pts = defaultdict(int)
    sniper_cnt = defaultdict(int)
    sniped_cnt = defaultdict(int)
    per_day = defaultdict(int)
    per_day_sniped = defaultdict(int)

    for sniper, sniped, day, pts in load_snipes(guild_id):
        sniper_pts[sniper] += pts
        sniper_cnt[sniper] += 1
        sniped_cnt[sniped] += 1
        per_day[day] += 1
        per_day_sniped[(day, sniped)] += 1

    # Ties go to the earliest date.
    best_day = max(per_day.items(), key=lambda kv: (kv[1], -kv[0].toordinal()), default=None)
    best_sniped_day = max(
        per_day_sniped.items(),
        key=lambda kv: (kv[1], -kv[0][0].toordinal()),
        default=None,
    )
    return {
        "sniper_rows": ranked(sniper_pts),
        "sniper_cnt": sniper_cnt,
        "sniped_rows": ranked(sniped_cnt),
        "best_day": best_day,
        "best_sniped_day": best_sniped_day,
    }


def rank_of(rows, uid):
    for rank, u, score in rows:
        if u == uid:
            return rank, score
    return None


def rank_badge(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")


def my_stats_text(guild_id: int, uid: int) -> str:
    stats = compute_stats(guild_id)
    sniping = rank_of(stats["sniper_rows"], uid)
    sniped = rank_of(stats["sniped_rows"], uid)

    if sniping:
        rank, pts = sniping
        n = stats["sniper_cnt"][uid]
        line1 = f"🎯 **Sniping:** rank #{rank} with {pts} pts ({n} snipes)"
    else:
        line1 = "🎯 **Sniping:** unranked (no snipes yet)"
    if sniped:
        rank, n = sniped
        line2 = f"👀 **Sniped:** rank #{rank}, caught {n} times"
    else:
        line2 = "👀 **Sniped:** unranked (never sniped)"
    return f"Period: {period_label(guild_id)}\n{line1}\n{line2}"


def build_embed(guild_id: int) -> discord.Embed:
    stats = compute_stats(guild_id)
    embed = discord.Embed(
        title="📸 Snipe Leaderboard",
        description=f"**Snipe period:** {period_label(guild_id)}",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    sniper_lines = [
        f"{rank_badge(r)} <@{uid}> — **{pts}** pts ({stats['sniper_cnt'][uid]} snipes)"
        for r, uid, pts in stats["sniper_rows"][:TOP_N]
    ]
    embed.add_field(
        name="🎯 Top Snipers",
        value="\n".join(sniper_lines) or "No snipes yet!",
        inline=False,
    )

    sniped_lines = [
        f"{rank_badge(r)} <@{uid}> — **{n}** times"
        for r, uid, n in stats["sniped_rows"][:TOP_N]
    ]
    embed.add_field(
        name="👀 Most Sniped",
        value="\n".join(sniped_lines) or "Nobody sniped yet!",
        inline=False,
    )

    if stats["best_day"]:
        day, n = stats["best_day"]
        day_text = f"{fmt_day(day)} — **{n}** snipes"
    else:
        day_text = "—"
    embed.add_field(name="🔥 Most Snipes in a Day", value=day_text, inline=True)

    if stats["best_sniped_day"]:
        (day, uid), n = stats["best_sniped_day"]
        sd_text = f"<@{uid}> — **{n}** times on {fmt_day(day)}"
    else:
        sd_text = "—"
    embed.add_field(name="💀 Most Sniped in a Day", value=sd_text, inline=True)

    embed.set_footer(text="Press “My Rank” to see your own rank • Last updated")
    return embed


# ----------------------------------------------------------------------------
# Bot setup
# ----------------------------------------------------------------------------
class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="My Rank",
        emoji="📍",
        style=discord.ButtonStyle.primary,
        custom_id="snipes:my_rank",
    )
    async def my_rank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            my_stats_text(interaction.guild_id, interaction.user.id), ephemeral=True
        )


class SnipeBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # must also be enabled in the Developer Portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(LeaderboardView())
        await self.tree.sync()
        refresh_loop.start()


client = SnipeBot()
tree = client.tree


async def update_leaderboard(guild_id: int):
    channel_id = get_cfg(guild_id, "leaderboard_channel")
    if not channel_id:
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return
    embed = build_embed(guild_id)
    try:
        msg_id = get_cfg(guild_id, "leaderboard_message")
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=LeaderboardView())
                return
            except discord.NotFound:
                pass
        msg = await channel.send(embed=embed, view=LeaderboardView())
        set_cfg(guild_id, "leaderboard_message", msg.id)
    except discord.HTTPException as e:
        print(f"Could not update leaderboard: {e}")


@tasks.loop(minutes=10)
async def refresh_loop():
    for guild in client.guilds:
        await update_leaderboard(guild.id)


@refresh_loop.before_loop
async def before_refresh():
    await client.wait_until_ready()


def is_snipes_channel(guild_id, channel_id) -> bool:
    configured = get_cfg(guild_id, "snipes_channel")
    return configured is not None and int(configured) == channel_id


async def sync_history(guild_id: int):
    """Rescan the snipes channel so the database matches what's actually there.

    Picks up snipes sent while the bot was offline and drops snipes whose
    messages were deleted while it was offline. Returns (scanned, snipes).
    """
    channel_id = get_cfg(guild_id, "snipes_channel")
    channel = client.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        return None

    start, _ = period_bounds(guild_id)
    seen_ids = set()
    scanned = found = 0
    async for message in channel.history(limit=None, after=start, oldest_first=True):
        scanned += 1
        seen_ids.add(message.id)
        found += record_snipe(message)

    # Remove snipes (within the scanned range) whose message no longer exists.
    rows = db.execute(
        "SELECT DISTINCT message_id, created_at FROM snipes WHERE guild_id=?", (guild_id,)
    ).fetchall()
    for r in rows:
        in_range = start is None or datetime.fromisoformat(r["created_at"]) >= start
        if in_range and r["message_id"] not in seen_ids:
            db.execute("DELETE FROM snipes WHERE message_id=?", (r["message_id"],))
    db.commit()
    return scanned, found


_synced = False


@client.event
async def on_ready():
    global _synced
    print(f"Logged in as {client.user}")
    if _synced:  # on_ready can fire again after reconnects
        return
    _synced = True
    for guild in client.guilds:
        result = await sync_history(guild.id)
        if result:
            print(f"[{guild.name}] scanned {result[0]} messages, {result[1]} snipes")
        await update_leaderboard(guild.id)
    print("Leaderboard updated. Leave this running to track live, or press Ctrl+C to stop.")


@client.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    if not is_snipes_channel(message.guild.id, message.channel.id):
        return
    if record_snipe(message):
        await update_leaderboard(message.guild.id)


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.guild_id is None:
        return
    if not is_snipes_channel(payload.guild_id, payload.channel_id):
        return
    channel = client.get_channel(payload.channel_id)
    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.HTTPException, AttributeError):
        return
    record_snipe(message)
    await update_leaderboard(payload.guild_id)


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    cur = db.execute("DELETE FROM snipes WHERE message_id=?", (payload.message_id,))
    db.commit()
    if cur.rowcount:
        await update_leaderboard(payload.guild_id)


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
admin_only = app_commands.checks.has_permissions(manage_guild=True)


@tree.command(name="setup", description="Set the snipes and leaderboard channels")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setup_cmd(
    interaction: discord.Interaction,
    snipes_channel: discord.TextChannel,
    leaderboard_channel: discord.TextChannel,
):
    gid = interaction.guild_id
    if get_cfg(gid, "leaderboard_channel") != str(leaderboard_channel.id):
        del_cfg(gid, "leaderboard_message")  # post a fresh message in the new channel
    set_cfg(gid, "snipes_channel", snipes_channel.id)
    set_cfg(gid, "leaderboard_channel", leaderboard_channel.id)
    await interaction.response.send_message(
        f"✅ Tracking snipes in {snipes_channel.mention}, "
        f"leaderboard in {leaderboard_channel.mention}.",
        ephemeral=True,
    )
    await update_leaderboard(gid)


@tree.command(name="setperiod", description="Set the snipe period (Eastern time)")
@app_commands.describe(
    start="Start date, YYYY-MM-DD (inclusive)", end="End date, YYYY-MM-DD (inclusive)"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setperiod(interaction: discord.Interaction, start: str, end: str):
    try:
        s, e = parse_date(start), parse_date(end)
    except ValueError:
        await interaction.response.send_message(
            "Dates must look like `2026-09-01`.", ephemeral=True
        )
        return
    if e < s:
        await interaction.response.send_message(
            "The end date can't be before the start date.", ephemeral=True
        )
        return
    set_cfg(interaction.guild_id, "period_start", start.strip())
    set_cfg(interaction.guild_id, "period_end", end.strip())
    await interaction.response.send_message(
        f"✅ Snipe period set: {period_label(interaction.guild_id)}", ephemeral=True
    )
    await update_leaderboard(interaction.guild_id)


@tree.command(name="setpoints", description="Set how many points sniping someone is worth")
@app_commands.describe(user="The person being sniped", points="Points the sniper earns (default 1)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setpoints(
    interaction: discord.Interaction,
    user: discord.Member,
    points: app_commands.Range[int, 0, 100],
):
    db.execute(
        "INSERT OR REPLACE INTO point_values (guild_id, user_id, points) VALUES (?, ?, ?)",
        (interaction.guild_id, user.id, points),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ Sniping {user.mention} is now worth **{points}** pts.", ephemeral=True
    )
    await update_leaderboard(interaction.guild_id)


@tree.command(name="pointvalues", description="Show anyone worth more or less than 1 point")
@app_commands.guild_only()
async def pointvalues(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, points FROM point_values WHERE guild_id=? ORDER BY points DESC",
        (interaction.guild_id,),
    ).fetchall()
    text = "\n".join(f"<@{r['user_id']}> — {r['points']} pts" for r in rows)
    await interaction.response.send_message(
        text or "Everyone is worth the default 1 point.", ephemeral=True
    )


@tree.command(name="mystats", description="See your (or someone's) snipe rank")
@app_commands.guild_only()
async def mystats(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    await interaction.response.send_message(
        f"Stats for {target.mention}\n{my_stats_text(interaction.guild_id, target.id)}",
        ephemeral=True,
    )


@tree.command(name="backfill", description="Scan the snipes channel history for past snipes")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def backfill(interaction: discord.Interaction):
    gid = interaction.guild_id
    if not get_cfg(gid, "snipes_channel"):
        await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await sync_history(gid)
    if result is None:
        await interaction.followup.send("I can't see the snipes channel.", ephemeral=True)
        return
    await interaction.followup.send(
        f"✅ Scanned {result[0]} messages and recorded {result[1]} snipes.", ephemeral=True
    )
    await update_leaderboard(gid)


@tree.command(name="refresh", description="Force the leaderboard to update now")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def refresh(interaction: discord.Interaction):
    await interaction.response.send_message("Refreshing…", ephemeral=True)
    await update_leaderboard(interaction.guild_id)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission to use this command."
    else:
        msg = "Something went wrong. Check the bot's console for details."
        print(f"Command error: {error!r}")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found. Check your .env file.")

client.run(TOKEN)