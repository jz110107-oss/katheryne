"""Katheryne: Adventurer's Guild bot.

Tracks snipes and awards Primogems for snipes, Donut Buddies, Photo of the Week,
socials, and office hours. Designed to be started occasionally: on startup it
rescans the relevant channels, then runs any weekly reset that is due.
"""
import asyncio
import json
import os
import random
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo("America/New_York")  # all dates, days, and resets use Eastern time
DB_PATH = "snipes.db"
TOP_N = 5

# Primogem rewards (snipes use per-person values set with /setpoints, default 1)
DONUT_DINING_GEMS = 4
DONUT_ELSEWHERE_GEMS = 5
POTW_GEMS = 2
SOCIAL_GEMS = 8
OFFICE_GEMS = 3
RECAP_GEMS = 1

DEFAULT_RESET_WEEKDAY = 6  # Monday=0 ... Sunday=6
DEFAULT_RESET_TIME = "13:00"

BUILTIN_THEMES = [
    "Something blue",
    "Your study spot",
    "Your dining hall meal",
    "The best thing you ate this week",
    "The view from where you are right now",
    "Something that made you smile",
    "Your favorite spot on campus",
    "Your desk or workspace",
    "Something green",
    "The sky today",
    "Your outfit of the day",
    "Something cozy",
    "Your go-to snack",
    "Something round",
    "Your shoes",
    "A reflection",
    "Your morning coffee or tea",
    "Something old",
    "Something you are grateful for",
    "A friend doing something silly",
]

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
    CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY
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
    CREATE TABLE IF NOT EXISTS awards (
        guild_id INTEGER, user_id INTEGER, source TEXT, key TEXT,
        amount INTEGER, created_at TEXT, message_id INTEGER, channel_id INTEGER,
        PRIMARY KEY (guild_id, user_id, source, key)
    );
    CREATE TABLE IF NOT EXISTS socials (
        guild_id INTEGER, channel_id INTEGER,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS donut_optouts (
        guild_id INTEGER, user_id INTEGER,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS donut_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, cycle TEXT, channel_id INTEGER, members TEXT,
        status TEXT, photo_msg_id INTEGER, prompt_msg_id INTEGER,
        grace INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS themes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER, text TEXT, used INTEGER DEFAULT 0
    );
    """
)
db.commit()

RESET_TIME_MIGRATION = "default_sunday_reset_13"
if not db.execute(
    "SELECT 1 FROM schema_migrations WHERE name=?", (RESET_TIME_MIGRATION,)
).fetchone():
    db.execute(
        """
        UPDATE config
        SET value=?
        WHERE key='reset_time' AND value='18:00'
          AND (
              NOT EXISTS (
                  SELECT 1 FROM config AS weekday
                  WHERE weekday.guild_id=config.guild_id
                    AND weekday.key='reset_weekday'
              )
              OR EXISTS (
                  SELECT 1 FROM config AS weekday
                  WHERE weekday.guild_id=config.guild_id
                    AND weekday.key='reset_weekday' AND weekday.value='6'
              )
          )
        """,
        (DEFAULT_RESET_TIME,),
    )
    db.execute(
        "INSERT INTO schema_migrations (name) VALUES (?)", (RESET_TIME_MIGRATION,)
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


def cfg_int(guild_id, key):
    v = get_cfg(guild_id, key)
    return int(v) if v else None


FEATURES = {
    "donut": "Donut Buddies",
    "potw": "Photo of the Week",
    "recap": "Weekly Recap",
    "office": "Office Hours",
}


def feature_on(guild_id, name) -> bool:
    """Weekly commission features can be switched off with /toggle."""
    return get_cfg(guild_id, f"off_{name}") != "1"


# ----------------------------------------------------------------------------
# Dates, season period, weekly cycles
# ----------------------------------------------------------------------------
def parse_date(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=TZ)


def period_bounds(guild_id):
    """(start_dt, end_dt_exclusive); either may be None if unset."""
    start_s = get_cfg(guild_id, "period_start")
    end_s = get_cfg(guild_id, "period_end")
    start = parse_date(start_s) if start_s else None
    end = parse_date(end_s) + timedelta(days=1) if end_s else None
    return start, end


def period_label(guild_id) -> str:
    start_s = get_cfg(guild_id, "period_start")
    end_s = get_cfg(guild_id, "period_end")
    if not start_s and not end_s:
        return "All time (no season set)"
    return f"{start_s or 'beginning'} → {end_s or 'ongoing'} (Eastern)"


def fmt_day(d) -> str:
    return f"{d:%a, %b} {d.day}"


def reset_schedule(guild_id):
    wd = cfg_int(guild_id, "reset_weekday")
    wd = DEFAULT_RESET_WEEKDAY if wd is None else wd
    hh, mm = (get_cfg(guild_id, "reset_time") or DEFAULT_RESET_TIME).split(":")
    return wd, int(hh), int(mm)


def cycle_start_for(guild_id, when: datetime) -> datetime:
    """The most recent weekly reset moment at or before `when` (Eastern)."""
    wd, hh, mm = reset_schedule(guild_id)
    local = when.astimezone(TZ)
    start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    start -= timedelta(days=(local.weekday() - wd) % 7)
    if start > local:
        start -= timedelta(days=7)
    return start


def next_reset_ts(guild_id, cycle_start: datetime) -> int:
    return int((cycle_start + timedelta(days=7)).timestamp())


def donuts_active(guild_id, cycle_start: datetime) -> bool:
    if not feature_on(guild_id, "donut"):
        return False
    s, e = get_cfg(guild_id, "donut_start"), get_cfg(guild_id, "donut_end")
    if not s or not e:
        return False
    return parse_date(s).date() <= cycle_start.date() <= parse_date(e).date()


# ----------------------------------------------------------------------------
# Misc helpers
# ----------------------------------------------------------------------------
def has_image(message: discord.Message) -> bool:
    return any((a.content_type or "").startswith("image/") for a in message.attachments)


def gem(guild_id) -> str:
    """Custom emoji named :primogem: if the server has one, else a diamond."""
    guild = client.get_guild(guild_id)
    if guild:
        e = discord.utils.get(guild.emojis, name="primogem")
        if e:
            return str(e)
    return "💎"


async def is_admin(guild: discord.Guild, user_id: int) -> bool:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
    return member.guild_permissions.manage_guild


def get_channel_cfg(guild: discord.Guild, key):
    cid = cfg_int(guild.id, key)
    return guild.get_channel(cid) if cid else None


def social_ids(guild_id):
    rows = db.execute("SELECT channel_id FROM socials WHERE guild_id=?", (guild_id,))
    return {r["channel_id"] for r in rows}


def group_members(row):
    return [int(x) for x in row["members"].split(",") if x]


def add_award(guild_id, user_id, source, key, amount, when, message_id=None, channel_id=None):
    """Idempotent: a (user, source, key) combination is only ever awarded once."""
    cur = db.execute(
        "INSERT OR IGNORE INTO awards "
        "(guild_id, user_id, source, key, amount, created_at, message_id, channel_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            user_id,
            source,
            str(key),
            amount,
            when.astimezone(timezone.utc).isoformat(),
            message_id,
            channel_id,
        ),
    )
    db.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------------------
# Snipes
# ----------------------------------------------------------------------------
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
# Photo of the week / office hours / socials
# ----------------------------------------------------------------------------
def record_potw(message: discord.Message) -> bool:
    if not has_image(message):
        return False
    cycle = cycle_start_for(message.guild.id, message.created_at).isoformat()
    return add_award(
        message.guild.id, message.author.id, "potw", cycle, POTW_GEMS,
        message.created_at, message.id, message.channel.id,
    )


async def record_office(message: discord.Message) -> bool:
    """Admin posts a photo tagging attendees: +OFFICE_GEMS once per weekly cycle."""
    if not has_image(message) or not await is_admin(message.guild, message.author.id):
        return False
    cycle = cycle_start_for(message.guild.id, message.created_at).isoformat()
    changed = False
    for u in message.mentions:
        if u.bot or u.id == message.author.id:
            continue
        changed |= add_award(
            message.guild.id, u.id, "office", cycle, OFFICE_GEMS,
            message.created_at, message.id, message.channel.id,
        )
    return changed


async def record_social(message: discord.Message) -> bool:
    """Admin posts a photo tagging attendees: +SOCIAL_GEMS once per social channel."""
    if not has_image(message) or not await is_admin(message.guild, message.author.id):
        return False
    changed = False
    for u in message.mentions:
        if u.bot or u.id == message.author.id:
            continue
        changed |= add_award(
            message.guild.id, u.id, "social", message.channel.id, SOCIAL_GEMS,
            message.created_at, message.id, message.channel.id,
        )
    return changed


# ----------------------------------------------------------------------------
# Weekly recap: reacting to an admin's recap message earns RECAP_GEMS (once per recap)
# ----------------------------------------------------------------------------
async def recap_award(guild: discord.Guild, message: discord.Message, user_ids) -> bool:
    if not feature_on(guild.id, "recap"):
        return False
    if message.author.bot or not await is_admin(guild, message.author.id):
        return False
    changed = False
    for uid in user_ids:
        if uid == message.author.id:
            continue
        changed |= add_award(
            guild.id, uid, "recap", message.id, RECAP_GEMS,
            message.created_at, message.id, message.channel.id,
        )
    return changed


async def scan_recap(channel) -> int:
    """Award reactions on recap messages from this week. If a weekly reset is due,
    also catch up on the week that just ended (reaction times aren't recorded by
    Discord, so reactions found now are counted for that week)."""
    guild = channel.guild
    gid = guild.id
    cs = cycle_start_for(gid, datetime.now(timezone.utc))
    window = cs
    stored = get_cfg(gid, "cycle")
    if stored and stored != cs.isoformat():
        try:
            window = min(cs, datetime.fromisoformat(stored))
        except ValueError:
            pass
    try:
        msgs = [
            m async for m in channel.history(limit=None, after=window, oldest_first=True)
        ]
    except discord.HTTPException as e:
        print(f"Could not read #{channel.name}: {e}")
        return 0
    reconcile_awards(gid, channel.id, {m.id for m in msgs}, window)
    for m in msgs:
        if m.author.bot:
            continue
        users = set()
        for reaction in m.reactions:
            async for u in reaction.users():
                if not u.bot:
                    users.add(u.id)
        await recap_award(guild, m, users)
    return len(msgs)


# ----------------------------------------------------------------------------
# Donut buddies
# ----------------------------------------------------------------------------
async def check_donut_photo(message: discord.Message) -> bool:
    """A group member posts a photo tagging the others: ask where they ate."""
    row = db.execute(
        "SELECT * FROM donut_groups WHERE guild_id=? AND channel_id=?",
        (message.guild.id, message.channel.id),
    ).fetchone()
    if not row or row["status"] != "open":
        return False
    members = group_members(row)
    if message.author.id not in members or not has_image(message):
        return False
    needed = set(members) - {message.author.id}
    if not needed <= {u.id for u in message.mentions}:
        return False

    # Flip the status before awaiting so the same photo can't prompt twice.
    db.execute(
        "UPDATE donut_groups SET status='prompted', photo_msg_id=? WHERE id=?",
        (message.id, row["id"]),
    )
    db.commit()
    prompt = await message.channel.send(
        "🍩 Katheryne has received your photo! One more thing: where did you dine?\n"
        f"School dining hall: **+{DONUT_DINING_GEMS}** {gem(message.guild.id)} each • "
        f"Somewhere else: **+{DONUT_ELSEWHERE_GEMS}** {gem(message.guild.id)} each",
        view=DonutPromptView(),
    )
    db.execute(
        "UPDATE donut_groups SET prompt_msg_id=? WHERE id=?", (prompt.id, row["id"])
    )
    db.commit()
    return True


async def resolve_donut(interaction: discord.Interaction, where: str):
    gid = interaction.guild_id
    row = db.execute(
        "SELECT * FROM donut_groups WHERE guild_id=? AND channel_id=?",
        (gid, interaction.channel_id),
    ).fetchone()
    if not row:
        await interaction.response.send_message(
            "I can't find a donut quest for this channel.", ephemeral=True
        )
        return
    members = group_members(row)
    if interaction.user.id not in members and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "Only the donut buddies in this channel can answer.", ephemeral=True
        )
        return
    if row["status"] != "prompted":
        await interaction.response.send_message(
            "This quest has already been recorded.", ephemeral=True
        )
        return

    amount = DONUT_DINING_GEMS if where == "dining" else DONUT_ELSEWHERE_GEMS
    db.execute("UPDATE donut_groups SET status='done' WHERE id=?", (row["id"],))
    db.commit()
    now = datetime.now(timezone.utc)
    for uid in members:
        add_award(gid, uid, "donut", row["id"], amount, now, row["photo_msg_id"], row["channel_id"])
    place = "the school dining hall" if where == "dining" else "somewhere new"
    await interaction.response.edit_message(
        content=(
            f"✅ Commission complete! You dined at {place}. Each buddy earned "
            f"**{amount}** {gem(gid)}. Nicely done, Adventurers!"
        ),
        view=None,
    )
    await update_guild(gid)


def make_groups(user_ids, history_pairs):
    """Random pairs (one trio if odd), preferring people who haven't been paired before."""
    ids = list(user_ids)
    if len(ids) < 2:
        return []
    best, best_score = None, None
    for _ in range(300):
        random.shuffle(ids)
        groups = [ids[i:i + 2] for i in range(0, len(ids) - 1, 2)]
        if len(ids) % 2 == 1:
            groups[-1].append(ids[-1])
        score = sum(
            1
            for g in groups
            for a, b in combinations(g, 2)
            if frozenset((a, b)) in history_pairs
        )
        if best_score is None or score < best_score:
            best, best_score = [list(g) for g in groups], score
            if score == 0:
                break
    return best


def channel_slug(member: discord.Member) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", member.display_name.lower()).strip("-")
    return slug[:12] or "buddy"


def find_category(guild: discord.Guild):
    cid = cfg_int(guild.id, "donut_category")
    if cid:
        c = guild.get_channel(cid)
        if isinstance(c, discord.CategoryChannel):
            return c
    return discord.utils.find(lambda c: c.name.lower() == "text channels", guild.categories)


def donut_overwrites(guild: discord.Guild, members):
    seen = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True
    )
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    ow[guild.me] = seen
    for m in members:
        ow[m] = seen
    for role in guild.roles:  # anyone with Manage Server (or Admin) can see every group
        if role.is_default():
            continue
        if role.permissions.manage_guild or role.permissions.administrator:
            ow[role] = seen
    return ow


async def form_donut_groups(guild: discord.Guild, cycle_id: str, cycle_start: datetime) -> bool:
    gid = guild.id
    role_id = cfg_int(gid, "participant_role")
    role = guild.get_role(role_id) if role_id else None
    optouts = {
        r["user_id"]
        for r in db.execute("SELECT user_id FROM donut_optouts WHERE guild_id=?", (gid,))
    }
    eligible = [
        m.id
        for m in guild.members
        if not m.bot and m.id not in optouts and (role is None or role in m.roles)
    ]

    history = set()
    for r in db.execute("SELECT members FROM donut_groups WHERE guild_id=?", (gid,)):
        for a, b in combinations([int(x) for x in r["members"].split(",") if x], 2):
            history.add(frozenset((a, b)))

    groups = make_groups(eligible, history)
    category = find_category(guild)
    g = gem(gid)
    reset_ts = next_reset_ts(gid, cycle_start)
    created = []  # (channel, group_row_id)
    try:
        for ids in groups:
            members = [guild.get_member(i) for i in ids]
            name = "donut-" + "-".join(channel_slug(m) for m in members)
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=donut_overwrites(guild, members),
                topic="Donut Buddies: share a meal, then post a photo tagging each other!",
                reason="Katheryne: weekly Donut Buddies",
            )
            cur = db.execute(
                "INSERT INTO donut_groups (guild_id, cycle, channel_id, members, status) "
                "VALUES (?, ?, ?, ?, 'open')",
                (gid, cycle_id, channel.id, ",".join(str(i) for i in ids)),
            )
            db.commit()
            created.append((channel, cur.lastrowid))
            mentions = " ".join(m.mention for m in members)
            await channel.send(
                f"🍩 {mentions}\n"
                "Katheryne paired you up for **Donut Buddies**! To earn primogems, get a meal "
                f"together this week, either at the dining hall (**+{DONUT_DINING_GEMS}** {g} each) "
                f"or somewhere else (**+{DONUT_ELSEWHERE_GEMS}** {g} each).\n\n"
                f"Then post a photo here tagging {'everyone else' if len(members) > 2 else 'your buddy'} "
                f"and Katheryne will ask where you went. New buddies are paired <t:{reset_ts}:R>.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Could not create donut buddy channels: {e}")
        for channel, row_id in created:  # roll back so a later run can retry cleanly
            try:
                await channel.delete(reason="Katheryne: rolling back failed donut setup")
            except discord.HTTPException:
                pass
            db.execute("DELETE FROM donut_groups WHERE id=?", (row_id,))
        db.commit()
        return False

    set_cfg(gid, "donut_cycle", cycle_id)

    return True


async def finalize_old_groups(guild: discord.Guild, new_cycle_id: str):
    """Delete last cycle's donut channels. A group whose photo is awaiting its
    dining-hall/elsewhere answer gets one extra cycle; after that it defaults to
    the dining hall reward."""
    gid = guild.id
    rows = db.execute(
        "SELECT * FROM donut_groups WHERE guild_id=? AND channel_id IS NOT NULL AND cycle != ?",
        (gid, new_cycle_id),
    ).fetchall()
    for row in rows:
        if row["status"] == "prompted" and not row["grace"]:
            db.execute("UPDATE donut_groups SET grace=1 WHERE id=?", (row["id"],))
            db.commit()
            continue
        if row["status"] == "prompted":
            now = datetime.now(timezone.utc)
            for uid in group_members(row):
                add_award(gid, uid, "donut", row["id"], DONUT_DINING_GEMS, now,
                          row["photo_msg_id"], row["channel_id"])
            db.execute("UPDATE donut_groups SET status='done' WHERE id=?", (row["id"],))
        channel = guild.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.delete(reason="Katheryne: Donut Buddies reset")
            except discord.HTTPException as e:
                print(f"Could not delete donut channel: {e}")
        db.execute("UPDATE donut_groups SET channel_id=NULL WHERE id=?", (row["id"],))
        db.commit()


# ----------------------------------------------------------------------------
# Photo of the week themes
# ----------------------------------------------------------------------------
def pick_theme(guild_id) -> str:
    row = db.execute(
        "SELECT id, text FROM themes WHERE guild_id=? AND used=0 ORDER BY id LIMIT 1",
        (guild_id,),
    ).fetchone()
    if row:
        db.execute("UPDATE themes SET used=1 WHERE id=?", (row["id"],))
        db.commit()
        return row["text"]
    used = json.loads(get_cfg(guild_id, "used_themes") or "[]")
    pool = [t for t in BUILTIN_THEMES if t not in used] or BUILTIN_THEMES
    if pool is BUILTIN_THEMES:
        used = []
    choice = random.choice(pool)
    used.append(choice)
    set_cfg(guild_id, "used_themes", json.dumps(used))
    return choice


async def post_theme(guild: discord.Guild, cycle_id: str, cycle_start: datetime) -> bool:
    channel = get_channel_cfg(guild, "potw_channel")
    if channel is None:
        return False
    gid = guild.id
    old_message = None
    old_message_id = get_cfg(gid, "potw_message")
    if old_message_id:
        try:
            old_message = await channel.fetch_message(int(old_message_id))
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"Could not fetch previous Photo of the Week prompt: {e}")
            return False

    theme = pick_theme(gid)
    reset_ts = next_reset_ts(gid, cycle_start)
    embed = discord.Embed(
        title="📸 Photo of the Week",
        description=(
            f"This week's theme: **{theme}**\n\n"
            f"Post a photo here to earn **{POTW_GEMS}** {gem(gid)}! "
            f"One reward per person. A new theme arrives <t:{reset_ts}:R>."
        ),
        color=discord.Color.blurple(),
    )
    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"Could not post theme: {e}")
        return False
    set_cfg(gid, "potw_cycle", cycle_id)
    set_cfg(gid, "potw_theme", theme)
    set_cfg(gid, "potw_message", msg.id)
    try:
        await msg.pin(reason="Katheryne: current Photo of the Week prompt")
    except discord.HTTPException as e:
        print(f"Could not pin the current Photo of the Week prompt: {e}")
    if old_message is not None:
        try:
            await old_message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"Could not delete previous Photo of the Week prompt: {e}")
    return True


# ----------------------------------------------------------------------------
# Weekly reset
# ----------------------------------------------------------------------------
reset_locks = defaultdict(asyncio.Lock)


async def maybe_reset(guild: discord.Guild):
    """Start the current weekly cycle if it hasn't started yet. Safe to call often."""
    gid = guild.id
    if not get_cfg(gid, "snipes_channel"):  # not set up yet
        return
    async with reset_locks[gid]:
        cs = cycle_start_for(gid, datetime.now(timezone.utc))
        cid = cs.isoformat()
        if get_cfg(gid, "cycle") != cid:
            await finalize_old_groups(guild, cid)
            set_cfg(gid, "cycle", cid)
        if (
            cfg_int(gid, "potw_channel")
            and feature_on(gid, "potw")
            and get_cfg(gid, "potw_cycle") != cid
        ):
            await post_theme(guild, cid, cs)
        if donuts_active(gid, cs) and get_cfg(gid, "donut_cycle") != cid:
            await form_donut_groups(guild, cid, cs)


# ----------------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------------
def load_snipes(guild_id):
    """Snipes inside the season as (sniper, sniped, eastern_date, points)."""
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


def load_awards(guild_id):
    """Non-snipe primogem awards inside the season as (user_id, amount)."""
    start, end = period_bounds(guild_id)
    out = []
    for r in db.execute(
        "SELECT user_id, amount, created_at FROM awards WHERE guild_id=?", (guild_id,)
    ):
        t = datetime.fromisoformat(r["created_at"])
        if start and t < start:
            continue
        if end and t >= end:
            continue
        out.append((r["user_id"], r["amount"]))
    return out


def gems_earned_this_cycle(guild_id: int, user_id: int) -> int:
    """Primogems earned by a user since the current weekly reset."""
    cycle_start = cycle_start_for(guild_id, datetime.now(timezone.utc))
    season_start, season_end = period_bounds(guild_id)
    total = 0

    rows = db.execute(
        """
        SELECT s.created_at, COALESCE(p.points, 1) AS points
        FROM snipes AS s
        LEFT JOIN point_values AS p
          ON p.guild_id = s.guild_id AND p.user_id = s.sniped_id
        WHERE s.guild_id=? AND s.sniper_id=?
        """,
        (guild_id, user_id),
    )
    for row in rows:
        created_at = datetime.fromisoformat(row["created_at"])
        if created_at >= cycle_start and (
            season_start is None or created_at >= season_start
        ) and (season_end is None or created_at < season_end):
            total += row["points"]

    rows = db.execute(
        "SELECT amount, created_at FROM awards WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    for row in rows:
        created_at = datetime.fromisoformat(row["created_at"])
        if created_at >= cycle_start and (
            season_start is None or created_at >= season_start
        ) and (season_end is None or created_at < season_end):
            total += row["amount"]
    return total


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
    gems = defaultdict(int)

    for sniper, sniped, day, pts in load_snipes(guild_id):
        sniper_pts[sniper] += pts
        sniper_cnt[sniper] += 1
        sniped_cnt[sniped] += 1
        per_day[day] += 1
        per_day_sniped[(day, sniped)] += 1
        gems[sniper] += pts
    for uid, amount in load_awards(guild_id):
        gems[uid] += amount

    best_day = max(per_day.items(), key=lambda kv: (kv[1], -kv[0].toordinal()), default=None)
    best_sniped_day = max(
        per_day_sniped.items(),
        key=lambda kv: (kv[1], -kv[0][0].toordinal()),
        default=None,
    )
    return {
        "gem_rows": ranked(gems),
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


# ----------------------------------------------------------------------------
# Adventurer's Guild embed and personal card
# ----------------------------------------------------------------------------
def commission_blocks(guild_id: int, uid: int) -> list:
    """One status and action or reward per weekly commission."""
    g = gem(guild_id)
    cs = cycle_start_for(guild_id, datetime.now(timezone.utc))
    cid = cs.isoformat()
    blocks = []

    # Donut Buddies (a group that already exists this week is always shown)
    mine = None
    for row in db.execute(
        "SELECT * FROM donut_groups WHERE guild_id=? AND cycle=?", (guild_id, cid)
    ):
        if uid in group_members(row):
            mine = row
            break
    if mine is None:
        if not donuts_active(guild_id, cs):
            blocks.append("🍩 No Donut Buddies this week")
        else:
            opted_out = db.execute(
                "SELECT 1 FROM donut_optouts WHERE guild_id=? AND user_id=?", (guild_id, uid)
            ).fetchone()
            blocks.append(
                "🍩 **Donut Buddies** ❌ Opt in to be matched"
                if opted_out
                else "🍩 **Donut Buddies** ❌ Wait to be matched with a buddy"
            )
    else:
        buddies = " & ".join(f"<@{i}>" for i in group_members(mine) if i != uid)
        if mine["status"] == "done":
            a = db.execute(
                "SELECT amount FROM awards WHERE guild_id=? AND user_id=? "
                "AND source='donut' AND key=?",
                (guild_id, uid, str(mine["id"])),
            ).fetchone()
            if a:
                blocks.append(f"🍩 **Donut Buddies** ✅ +{a['amount']} {g}")
            else:
                blocks.append("🍩 **Donut Buddies** ❌ Reward not recorded; ask an admin to check")
        elif mine["status"] == "prompted":
            blocks.append(
                f"🍩 **Donut Buddies** ❌ Answer Katheryne in <#{mine['channel_id']}> "
                "to claim your reward"
            )
        else:
            blocks.append(
                f"🍩 **Donut Buddies** ❌ Meet with {buddies}, share a meal, then post a photo "
                f"in <#{mine['channel_id']}>"
            )

    # Photo of the Week
    if not cfg_int(guild_id, "potw_channel") or not feature_on(guild_id, "potw"):
        blocks.append("📸 No Photo of the Week this week")
    else:
        theme = get_cfg(guild_id, "potw_theme") or "TBA"
        done = db.execute(
            "SELECT amount FROM awards WHERE guild_id=? AND user_id=? "
            "AND source='potw' AND key=?",
            (guild_id, uid, cid),
        ).fetchone()
        if done:
            blocks.append(f"📸 **Photo of the Week** ✅ +{done['amount']} {g}")
        else:
            blocks.append(
                f"📸 **Photo of the Week** ❌ Post a photo to "
                f"<#{cfg_int(guild_id, 'potw_channel')}> "
                f"for this week's theme: *{theme}*"
            )

    # Weekly Recap (reacted to a recap posted this cycle)
    if not cfg_int(guild_id, "recap_channel") or not feature_on(guild_id, "recap"):
        blocks.append("📰 No Weekly Recap this week")
    else:
        done = db.execute(
            "SELECT SUM(amount) AS amount FROM awards "
            "WHERE guild_id=? AND user_id=? AND source='recap' AND created_at>=?",
            (guild_id, uid, cs.astimezone(timezone.utc).isoformat()),
        ).fetchone()
        if done["amount"]:
            blocks.append(f"📰 **Weekly Recap** ✅ +{done['amount']} {g}")
        else:
            blocks.append(
                f"📰 **Weekly Recap** ❌ React to this week's recap in "
                f"<#{cfg_int(guild_id, 'recap_channel')}>"
            )

    # Office Hours (marked present by an admin this cycle)
    if not cfg_int(guild_id, "office_channel") or not feature_on(guild_id, "office"):
        blocks.append("🕐 No Office Hours this week")
    else:
        done = db.execute(
            "SELECT amount FROM awards WHERE guild_id=? AND user_id=? "
            "AND source='office' AND key=?",
            (guild_id, uid, cid),
        ).fetchone()
        if done:
            blocks.append(f"🕐 **Office Hours** ✅ +{done['amount']} {g}")
        else:
            blocks.append("🕐 **Office Hours** ❌ Come to office hours")
    return blocks


def build_card(guild_id: int, user) -> discord.Embed:
    g = gem(guild_id)
    stats = compute_stats(guild_id)
    uid = user.id
    gem_rank = rank_of(stats["gem_rows"], uid)
    sniping = rank_of(stats["sniper_rows"], uid)
    sniped = rank_of(stats["sniped_rows"], uid)
    weekly_gems = gems_earned_this_cycle(guild_id, uid)

    sections = [
        f"# Ad astra abyssosque!\n\n"
        f"Welcome to the Adventurer's Guild, **{user.display_name}**. Here is your record.",
        f"## You earned {weekly_gems} {g} this week.",
        "",
        "",
        f"## {g} Primogems\n"
        + (
            f"You have {gem_rank[1]} {g}. You are rank #{gem_rank[0]}."
            if gem_rank
            else f"You have 0 {g}. You are unranked."
        ),
        "## 🎯 Snipes\n"
        + (
            f"You sniped {stats['sniper_cnt'][uid]} people. You are rank #{sniping[0]}."
            if sniping
            else "You sniped 0 people. You are unranked."
        ),
        "## 👀 Times Sniped\n"
        + (
            f"You were sniped {sniped[1]} times. You are rank #{sniped[0]}."
            if sniped
            else "You were sniped 0 times. You are unranked."
        ),
        "## 📜 This Week's Commissions\n\n"
        + "\n\n".join(f"> {commission}" for commission in commission_blocks(guild_id, uid)),
    ]
    embed = discord.Embed(
        description="\n\n".join(sections),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Season: {period_label(guild_id)}")
    return embed


def build_guild_embed(guild_id: int) -> discord.Embed:
    g = gem(guild_id)
    stats = compute_stats(guild_id)
    cs = cycle_start_for(guild_id, datetime.now(timezone.utc))
    reset_ts = next_reset_ts(guild_id, cs)

    sections = [
        "# ⚔️ Adventurer's Guild\n\n"
        f"**Season:** {period_label(guild_id)}\n"
        f"🔄 **Next weekly reset:** <t:{reset_ts}:F> (<t:{reset_ts}:R>)"
    ]

    gem_lines = [
        f"{rank_badge(r)} <@{uid}> — **{n}** {g}" for r, uid, n in stats["gem_rows"][:TOP_N]
    ]
    sections.append(
        f"## {g} Primogem Leaderboard\n"
        + ("\n".join(gem_lines) or "No primogems earned yet!")
    )

    sniper_lines = [
        f"{rank_badge(r)} <@{uid}> — **{stats['sniper_cnt'][uid]}** snipes"
        for r, uid, _ in stats["sniper_rows"][:TOP_N]
    ]
    sections.append(
        "## 🎯 Top Snipers\n" + ("\n".join(sniper_lines) or "No snipes yet!")
    )

    sniped_lines = [
        f"{rank_badge(r)} <@{uid}> — **{n}** times" for r, uid, n in stats["sniped_rows"][:TOP_N]
    ]
    sections.append(
        "## 👀 Most Sniped\n" + ("\n".join(sniped_lines) or "Nobody sniped yet!")
    )

    if stats["best_day"]:
        day, n = stats["best_day"]
        day_text = f"{fmt_day(day)}: **{n}** snipes"
    else:
        day_text = "—"
    if stats["best_sniped_day"]:
        (day, uid), n = stats["best_sniped_day"]
        sd_text = f"<@{uid}>: **{n}** times on {fmt_day(day)}"
    else:
        sd_text = "—"
    sections.append(
        "## 📊 Other Statistics\n"
        f"**Most Snipes in a Day**\n{day_text}\n"
        f"**Most Sniped in a Day**\n{sd_text}"
    )

    embed = discord.Embed(
        description="\n\n".join(sections),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Katheryne • Press a button below • Last updated")
    return embed


# ----------------------------------------------------------------------------
# Views (persistent buttons)
# ----------------------------------------------------------------------------
class GuildView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="My Adventurer Card",
        emoji="📜",
        style=discord.ButtonStyle.primary,
        custom_id="guild:card",
    )
    async def card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=build_card(interaction.guild_id, interaction.user),
            ephemeral=True,
            delete_after=60,
        )

    @discord.ui.button(
        label="Donut Buddies: Opt In/Out",
        emoji="🍩",
        style=discord.ButtonStyle.secondary,
        custom_id="guild:donut",
    )
    async def donut(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid, uid = interaction.guild_id, interaction.user.id
        opted_out = db.execute(
            "SELECT 1 FROM donut_optouts WHERE guild_id=? AND user_id=?", (gid, uid)
        ).fetchone()
        if opted_out:
            db.execute(
                "DELETE FROM donut_optouts WHERE guild_id=? AND user_id=?", (gid, uid)
            )
            msg = "🍩 You're back in! You'll be matched at the next weekly reset."
        else:
            db.execute(
                "INSERT OR IGNORE INTO donut_optouts (guild_id, user_id) VALUES (?, ?)",
                (gid, uid),
            )
            msg = "💤 You've opted out of Donut Buddies. Press the button again to rejoin. This applies from the next reset."
        db.commit()
        await interaction.response.send_message(msg, ephemeral=True, delete_after=60)


class DonutPromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label=f"School dining hall (+{DONUT_DINING_GEMS})",
        emoji="🏫",
        style=discord.ButtonStyle.secondary,
        custom_id="donut:dining",
    )
    async def dining(self, interaction: discord.Interaction, button: discord.ui.Button):
        await resolve_donut(interaction, "dining")

    @discord.ui.button(
        label=f"Somewhere else (+{DONUT_ELSEWHERE_GEMS})",
        emoji="🍜",
        style=discord.ButtonStyle.success,
        custom_id="donut:elsewhere",
    )
    async def elsewhere(self, interaction: discord.Interaction, button: discord.ui.Button):
        await resolve_donut(interaction, "elsewhere")


# ----------------------------------------------------------------------------
# Bot setup
# ----------------------------------------------------------------------------
class Katheryne(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # also enable in the Developer Portal
        intents.members = True  # Server Members Intent: needed to find donut buddy candidates
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(GuildView())
        self.add_view(DonutPromptView())
        await self.tree.sync()


client = Katheryne()
tree = client.tree


async def update_guild(guild_id: int) -> str | None:
    """Create or edit the Adventurer's Guild message, returning its URL on success."""
    guild = client.get_guild(guild_id)
    if guild is None:
        return None
    channel = get_channel_cfg(guild, "leaderboard_channel")
    if channel is None:
        return None
    embed = build_guild_embed(guild_id)
    operation = "fetch the existing message"
    try:
        msg_id = get_cfg(guild_id, "leaderboard_message")
        stale_message = None
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                operation = "edit the existing message"
                updated = await msg.edit(embed=embed, view=GuildView())
                if not updated.embeds or updated.embeds[0].description != embed.description:
                    stale_message = msg
                    actual_description = (
                        updated.embeds[0].description
                        if updated.embeds
                        else "(no embed returned)"
                    )
                    print(
                        f"Discord returned unexpected Adventurer's Guild embed for message "
                        f"{updated.id}. Expected: {embed.description!r}; "
                        f"received: {actual_description!r}"
                    )
            except discord.NotFound:
                updated = None
        else:
            updated = None
        if updated is None or stale_message is not None:
            if stale_message is not None:
                print(
                    f"Discord did not apply the Adventurer's Guild embed to message "
                    f"{stale_message.id}; posting a replacement."
                )
            operation = "send a replacement message"
            updated = await channel.send(embed=embed, view=GuildView())
            if not updated.embeds or updated.embeds[0].description != embed.description:
                actual_description = (
                    updated.embeds[0].description
                    if updated.embeds
                    else "(no embed returned)"
                )
                print(
                    f"Discord returned unexpected Adventurer's Guild embed for replacement "
                    f"message {updated.id}. Expected: {embed.description!r}; "
                    f"received: {actual_description!r}"
                )
                return None
            set_cfg(guild_id, "leaderboard_message", updated.id)
            if stale_message is not None:
                try:
                    await stale_message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    print(
                        f"Could not delete stale Adventurer's Guild message "
                        f"{stale_message.id}: {e}"
                    )
        return updated.jump_url
    except discord.Forbidden as e:
        missing = []
        if guild.me is not None:
            permissions = channel.permissions_for(guild.me)
            if operation == "send a replacement message":
                for permission in ("view_channel", "send_messages", "embed_links"):
                    if not getattr(permissions, permission):
                        missing.append(permission)
        detail = f" Missing channel permissions: {', '.join(missing)}." if missing else ""
        print(
            f"Discord denied permission to {operation} in #{channel.name}: "
            f"{e}.{detail}"
        )
        return None
    except discord.HTTPException as e:
        print(f"Could not {operation} in #{channel.name}: {e}")
        return None


@tasks.loop(minutes=10)
async def refresh_loop():
    if refresh_loop.current_loop == 0:  # startup sync just ran
        return
    for guild in client.guilds:
        try:
            await maybe_reset(guild)
        except Exception as e:  # keep the loop alive
            print(f"Reset check failed for {guild.name}: {e!r}")
        await update_guild(guild.id)


# ----------------------------------------------------------------------------
# Message processing and history sync
# ----------------------------------------------------------------------------
def is_snipes_channel(guild_id, channel_id) -> bool:
    configured = get_cfg(guild_id, "snipes_channel")
    return configured is not None and int(configured) == channel_id


async def process_message(message: discord.Message) -> bool:
    """Routes a message to the right feature. Returns True if anything changed."""
    if message.guild is None or message.author.bot:
        return False
    gid, cid = message.guild.id, message.channel.id
    if is_snipes_channel(gid, cid):
        return record_snipe(message) > 0
    if cid == cfg_int(gid, "potw_channel"):
        return feature_on(gid, "potw") and record_potw(message)
    if cid == cfg_int(gid, "office_channel"):
        return feature_on(gid, "office") and await record_office(message)
    if cid in social_ids(gid):
        return await record_social(message)
    return await check_donut_photo(message)


def reconcile_snipes(guild_id, seen_ids, start):
    rows = db.execute(
        "SELECT DISTINCT message_id, created_at FROM snipes WHERE guild_id=?", (guild_id,)
    ).fetchall()
    for r in rows:
        in_range = start is None or datetime.fromisoformat(r["created_at"]) >= start
        if in_range and r["message_id"] not in seen_ids:
            db.execute("DELETE FROM snipes WHERE message_id=?", (r["message_id"],))
    db.commit()


def reconcile_awards(guild_id, channel_id, seen_ids, start):
    """Drop awards whose source message was deleted while the bot was offline."""
    rows = db.execute(
        "SELECT rowid, message_id, created_at FROM awards "
        "WHERE guild_id=? AND channel_id=? AND message_id IS NOT NULL AND source != 'donut'",
        (guild_id, channel_id),
    ).fetchall()
    for r in rows:
        in_range = start is None or datetime.fromisoformat(r["created_at"]) >= start
        if in_range and r["message_id"] not in seen_ids:
            db.execute("DELETE FROM awards WHERE rowid=?", (r["rowid"],))
    db.commit()


async def rescan(channel, after, is_snipes: bool) -> int:
    gid = channel.guild.id
    try:
        msgs = [
            m async for m in channel.history(limit=None, after=after, oldest_first=True)
        ]
    except discord.HTTPException as e:
        print(f"Could not read #{channel.name}: {e}")
        return 0
    seen = {m.id for m in msgs}
    if is_snipes:
        reconcile_snipes(gid, seen, after)
    else:
        reconcile_awards(gid, channel.id, seen, after)
    for m in msgs:
        try:
            await process_message(m)
        except Exception as e:
            print(f"Error processing message {m.id}: {e!r}")
    return len(msgs)


async def sync_guild(guild: discord.Guild) -> tuple[int, str | None]:
    """Rescan every tracked channel, run any due weekly reset, refresh the Guild."""
    gid = guild.id
    start, _ = period_bounds(gid)
    scanned = 0

    ch = get_channel_cfg(guild, "snipes_channel")
    if ch:
        scanned += await rescan(ch, start, True)
    for key, feature in (("potw_channel", "potw"), ("office_channel", "office")):
        ch = get_channel_cfg(guild, key)
        if ch and feature_on(gid, feature):
            scanned += await rescan(ch, start, False)
    for sid in social_ids(gid):
        ch = guild.get_channel(sid)
        if ch:
            scanned += await rescan(ch, start, False)
    for row in db.execute(
        "SELECT channel_id FROM donut_groups WHERE guild_id=? AND status='open' "
        "AND channel_id IS NOT NULL",
        (gid,),
    ).fetchall():
        ch = guild.get_channel(row["channel_id"])
        if ch:
            scanned += await rescan(ch, None, False)

    ch = get_channel_cfg(guild, "recap_channel")
    if ch and feature_on(gid, "recap"):
        scanned += await scan_recap(ch)  # before the reset so last week's reactions count

    await maybe_reset(guild)
    updated = await update_guild(gid)
    return scanned, updated


_synced = False


@client.event
async def on_ready():
    global _synced
    print(f"Logged in as {client.user}")
    if _synced:  # on_ready can fire again after reconnects
        return
    _synced = True
    for guild in client.guilds:
        try:
            scanned, message_url = await sync_guild(guild)
            result = (
                f"leaderboard refreshed: {message_url}"
                if message_url
                else "leaderboard was not updated"
            )
            print(f"[{guild.name}] scanned {scanned} messages; {result}")
        except Exception as e:
            print(f"[{guild.name}] sync failed: {e!r}")
    print("Bot is online. Leave this running to track live, or press Ctrl+C to stop.")
    refresh_loop.start()


@client.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    try:
        changed = await process_message(message)
    except Exception as e:
        print(f"Error processing message {message.id}: {e!r}")
        return
    if changed:
        await update_guild(message.guild.id)


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.guild_id is None or not is_snipes_channel(payload.guild_id, payload.channel_id):
        return
    channel = client.get_channel(payload.channel_id)
    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.HTTPException, AttributeError):
        return
    record_snipe(message)
    await update_guild(payload.guild_id)


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    a = db.execute("DELETE FROM snipes WHERE message_id=?", (payload.message_id,)).rowcount
    b = db.execute(
        "DELETE FROM awards WHERE message_id=? AND source != 'donut'", (payload.message_id,)
    ).rowcount
    db.commit()
    if a or b:
        await update_guild(payload.guild_id)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == client.user.id:
        return
    if payload.channel_id != cfg_int(payload.guild_id, "recap_channel"):
        return
    if payload.member is not None and payload.member.bot:
        return
    guild = client.get_guild(payload.guild_id)
    channel = client.get_channel(payload.channel_id)
    if guild is None or channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    now_cycle = cycle_start_for(guild.id, datetime.now(timezone.utc))
    if cycle_start_for(guild.id, message.created_at) != now_cycle:
        return  # only this week's recap earns primogems
    if await recap_award(guild, message, {payload.user_id}):
        await update_guild(guild.id)


# ----------------------------------------------------------------------------
# Slash commands
# ----------------------------------------------------------------------------
admin_only = app_commands.checks.has_permissions(manage_guild=True)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@tree.command(name="setup", description="Set the snipes channel and the Adventurer's Guild channel")
@app_commands.describe(
    snipes_channel="Where snipe photos are posted",
    guild_channel="Where the Adventurer's Guild message lives",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setup_cmd(
    interaction: discord.Interaction,
    snipes_channel: discord.TextChannel,
    guild_channel: discord.TextChannel,
):
    gid = interaction.guild_id
    if get_cfg(gid, "leaderboard_channel") != str(guild_channel.id):
        del_cfg(gid, "leaderboard_message")  # post a fresh message in the new channel
    set_cfg(gid, "snipes_channel", snipes_channel.id)
    set_cfg(gid, "leaderboard_channel", guild_channel.id)
    await interaction.response.send_message(
        f"✅ Snipes in {snipes_channel.mention}, Adventurer's Guild in {guild_channel.mention}.",
        ephemeral=True,
    )
    await update_guild(gid)


@tree.command(name="configure", description="Set optional channels and roles, and view current settings")
@app_commands.describe(
    photo_of_week_channel="Where the weekly theme is posted",
    office_hours_channel="Where admins mark office hours attendance",
    weekly_recap_channel="Where weekly recaps are posted (reacting earns primogems)",
    donut_category="Category for private donut buddy channels (default: 'Text Channels')",
    participant_role="Only members with this role get donut buddies (default: everyone)",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def configure(
    interaction: discord.Interaction,
    photo_of_week_channel: discord.TextChannel | None = None,
    office_hours_channel: discord.TextChannel | None = None,
    weekly_recap_channel: discord.TextChannel | None = None,
    donut_category: discord.CategoryChannel | None = None,
    participant_role: discord.Role | None = None,
):
    gid = interaction.guild_id
    await interaction.response.defer(ephemeral=True)
    if photo_of_week_channel:
        set_cfg(gid, "potw_channel", photo_of_week_channel.id)
    if office_hours_channel:
        set_cfg(gid, "office_channel", office_hours_channel.id)
    if weekly_recap_channel:
        set_cfg(gid, "recap_channel", weekly_recap_channel.id)
    if donut_category:
        set_cfg(gid, "donut_category", donut_category.id)
    if participant_role:
        set_cfg(gid, "participant_role", participant_role.id)

    def ch(key):
        c = cfg_int(gid, key)
        return f"<#{c}>" if c else "not set"

    wd, hh, mm = reset_schedule(gid)
    cat = find_category(interaction.guild)
    role_id = cfg_int(gid, "participant_role")
    socials = " ".join(f"<#{i}>" for i in social_ids(gid)) or "none"
    ds, de = get_cfg(gid, "donut_start"), get_cfg(gid, "donut_end")
    await interaction.followup.send(
        "**Current settings**\n"
        f"Snipes: {ch('snipes_channel')}\n"
        f"Adventurer's Guild: {ch('leaderboard_channel')}\n"
        f"Photo of the Week: {ch('potw_channel')}\n"
        f"Office hours: {ch('office_channel')}\n"
        f"Weekly recap: {ch('recap_channel')}\n"
        f"Socials: {socials}\n"
        f"Donut category: {cat.name if cat else 'none found (channels will be top-level)'}\n"
        f"Donut participants: {f'<@&{role_id}>' if role_id else 'everyone'}\n"
        f"Donut dates: {f'{ds} → {de}' if ds and de else 'not set'}\n"
        f"Weekly reset: {WEEKDAYS[wd]} {hh:02d}:{mm:02d} Eastern\n"
        "Features: "
        + ", ".join(
            f"{name} {'on' if feature_on(gid, key) else 'OFF'}" for key, name in FEATURES.items()
        )
        + "\n"
        f"Season: {period_label(gid)}",
        ephemeral=True,
    )
    await update_guild(gid)


@tree.command(name="toggle", description="Turn a weekly commission feature on or off")
@app_commands.describe(feature="Which feature", enabled="True to turn on, False to turn off")
@app_commands.choices(
    feature=[app_commands.Choice(name=v, value=k) for k, v in FEATURES.items()]
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def toggle(
    interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool
):
    gid = interaction.guild_id
    await interaction.response.defer(ephemeral=True)
    set_cfg(gid, f"off_{feature.value}", "0" if enabled else "1")
    await maybe_reset(interaction.guild)  # turning on mid-week starts it right away
    await update_guild(gid)
    if enabled:
        msg = f"✅ **{feature.name}** is on."
    else:
        msg = (
            f"✅ **{feature.name}** is off. The Adventurer card will say \"No {feature.name} "
            "this week\" and nothing new will be awarded for it."
        )
        if feature.value == "donut":
            msg += " Groups that already exist keep working until the next reset."
    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="setperiod", description="Set the season (Eastern time)")
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
        f"✅ Season set: {period_label(interaction.guild_id)}", ephemeral=True
    )
    await update_guild(interaction.guild_id)


@tree.command(name="setreset", description="Set the weekly reset (donut buddies, theme, office hours)")
@app_commands.describe(weekday="Day of the week", time="24-hour time, e.g. 18:00 (Eastern)")
@app_commands.choices(
    weekday=[app_commands.Choice(name=n, value=i) for i, n in enumerate(WEEKDAYS)]
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setreset(
    interaction: discord.Interaction, weekday: app_commands.Choice[int], time: str
):
    try:
        t = datetime.strptime(time.strip(), "%H:%M")
    except ValueError:
        await interaction.response.send_message(
            "Time must look like `18:00` (24-hour).", ephemeral=True
        )
        return
    gid = interaction.guild_id
    await interaction.response.defer(ephemeral=True)
    set_cfg(gid, "reset_weekday", weekday.value)
    set_cfg(gid, "reset_time", f"{t.hour:02d}:{t.minute:02d}")
    await maybe_reset(interaction.guild)
    await update_guild(gid)
    await interaction.followup.send(
        f"✅ Weekly reset: {weekday.name} at {t.hour:02d}:{t.minute:02d} Eastern. "
        "(Changing this mid-week can start a new cycle right away.)",
        ephemeral=True,
    )


@tree.command(name="setdonuts", description="Set the dates Donut Buddies runs")
@app_commands.describe(
    start="First date, YYYY-MM-DD", end="Last date, YYYY-MM-DD (inclusive)"
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def setdonuts(interaction: discord.Interaction, start: str, end: str):
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
    gid = interaction.guild_id
    await interaction.response.defer(ephemeral=True)
    set_cfg(gid, "donut_start", start.strip())
    set_cfg(gid, "donut_end", end.strip())
    await maybe_reset(interaction.guild)
    await update_guild(gid)
    await interaction.followup.send(
        f"✅ Donut Buddies runs {start.strip()} → {end.strip()}. Groups form at each weekly reset "
        "that falls in that range (and right now if the current week is inside it).",
        ephemeral=True,
    )


@tree.command(name="setpoints", description="Set how many primogems sniping someone is worth")
@app_commands.describe(user="The person being sniped", points="Primogems the sniper earns (default 1)")
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
        f"✅ Sniping {user.mention} is now worth **{points}** {gem(interaction.guild_id)}.",
        ephemeral=True,
    )
    await update_guild(interaction.guild_id)


@tree.command(name="pointvalues", description="Show anyone worth more or less than 1 primogem")
@app_commands.guild_only()
async def pointvalues(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, points FROM point_values WHERE guild_id=? ORDER BY points DESC",
        (interaction.guild_id,),
    ).fetchall()
    g = gem(interaction.guild_id)
    text = "\n".join(f"<@{r['user_id']}> — {r['points']} {g}" for r in rows)
    await interaction.response.send_message(
        text or f"Everyone is worth the default 1 {g}.", ephemeral=True
    )


@tree.command(name="addsocial", description="Mark a channel as a socials attendance channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def addsocial(interaction: discord.Interaction, channel: discord.TextChannel):
    db.execute(
        "INSERT OR IGNORE INTO socials (guild_id, channel_id) VALUES (?, ?)",
        (interaction.guild_id, channel.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ {channel.mention} is now a socials channel. Post a photo tagging attendees "
        f"to give each of them **{SOCIAL_GEMS}** {gem(interaction.guild_id)} (once per channel).",
        ephemeral=True,
    )


@tree.command(name="removesocial", description="Stop treating a channel as a socials channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def removesocial(interaction: discord.Interaction, channel: discord.TextChannel):
    db.execute(
        "DELETE FROM socials WHERE guild_id=? AND channel_id=?",
        (interaction.guild_id, channel.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ {channel.mention} is no longer a socials channel. Existing awards are kept.",
        ephemeral=True,
    )


@tree.command(name="addtheme", description="Add a Photo of the Week theme (used before the built-in ones)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def addtheme(interaction: discord.Interaction, theme: str):
    db.execute(
        "INSERT INTO themes (guild_id, text) VALUES (?, ?)", (interaction.guild_id, theme.strip())
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ Added theme: *{theme.strip()}*. Custom themes are used in the order added.",
        ephemeral=True,
    )


@tree.command(name="givegems", description="Manually add or remove primogems (corrections)")
@app_commands.describe(user="Who", amount="Positive or negative", reason="Why (for your records)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def givegems(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: app_commands.Range[int, -1000, 1000],
    reason: str = "",
):
    add_award(
        interaction.guild_id, user.id, "manual", uuid.uuid4().hex, amount,
        datetime.now(timezone.utc),
    )
    g = gem(interaction.guild_id)
    await interaction.response.send_message(
        f"✅ {amount:+d} {g} for {user.mention}." + (f" ({reason})" if reason else ""),
        ephemeral=True,
    )
    await update_guild(interaction.guild_id)


@tree.command(name="mystats", description="See your (or someone's) Adventurer card")
@app_commands.guild_only()
async def mystats(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    await interaction.response.send_message(
        embed=build_card(interaction.guild_id, target), ephemeral=True
    )


@tree.command(name="sync", description="Rescan channels, run any due weekly reset, refresh the Guild")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only
async def sync_cmd(interaction: discord.Interaction):
    if not get_cfg(interaction.guild_id, "snipes_channel"):
        await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    scanned, message_url = await sync_guild(interaction.guild)
    if message_url:
        message = (
            f"✅ Scanned {scanned} messages and refreshed the Adventurer's Guild: "
            f"{message_url}"
        )
    else:
        message = (
            f"⚠️ Scanned {scanned} messages, but could not update the Adventurer's Guild. "
            "Check the bot console for Discord's error and required channel permissions."
        )
    await interaction.followup.send(message, ephemeral=True)


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