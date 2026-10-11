import asyncio
import getpass
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands

BOT_NAME = "ourhope"
DB_PATH = os.getenv("DB_PATH", "ourhope.sqlite3")
BLACKLIST_ROLE = "Blacklisted"
SUPPORT_ROLE = "Support Team"
LINK_RE = re.compile(r"(?:https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/|t\.me/|bit\.ly/)", re.I)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_time(ts: float) -> str:
    return f"<t:{int(ts)}:F> · <t:{int(ts)}:R>"


def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def guild_settings(guild_id: int) -> dict:
    try:
        with db_connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}')")
            row = con.execute("SELECT settings FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
        return json.loads(row["settings"]) if row else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}


def init_db():
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS giveaways (
            message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL, prize TEXT NOT NULL, ends_at REAL NOT NULL,
            winners_count INTEGER NOT NULL DEFAULT 1, ended INTEGER NOT NULL DEFAULT 0,
            entrants TEXT NOT NULL DEFAULT '[]')""")
        con.execute("""CREATE TABLE IF NOT EXISTS tickets (
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, opener_id INTEGER NOT NULL,
            claimed_by INTEGER, closed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS blacklist_role_backups (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_ids TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS mod_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)""")


class OurhopeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned_or("?", ","), intents=intents, help_command=None)
        self.spam_cache: dict[tuple[int, int], list[tuple[str, float]]] = {}
        self.last_message_hash: dict[tuple[int, int], tuple[str, int, float]] = {}

    async def setup_hook(self):
        init_db()
        self.add_view(GiveawayView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash commands.")
        except Exception as exc:
            print(f"Slash command sync failed: {exc}")
        giveaway_watcher.start()

    async def on_ready(self):
        print(f"{BOT_NAME} online as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over the community"))

    async def on_guild_channel_create(self, channel):
        # A blacklist deny must apply to channels created after a member was blacklisted.
        role = discord.utils.get(channel.guild.roles, name=BLACKLIST_ROLE)
        if role:
            try:
                await channel.set_permissions(role, view_channel=False, reason="Keep blacklisted members out of new channels")
            except discord.HTTPException:
                pass

    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return

        # A blacklisted member should not be able to use bot commands.
        blacklisted_role = discord.utils.get(message.guild.roles, name=BLACKLIST_ROLE)
        if blacklisted_role and blacklisted_role in member.roles:
            return

        content = message.content.strip()
        settings = guild_settings(message.guild.id)
        # Dashboard toggles are per server; secure defaults apply when no setting is saved.
        # Anti-link: timeout for one hour and remove the offending message.
        if settings.get("anti_link", "true") != "false" and LINK_RE.search(content):
            await self.apply_timeout(member, message, "Posting a link (anti-raid rule)")
            await self.process_commands(message)
            return

        # More than five identical messages within 30 seconds = six or more copies.
        key = (message.guild.id, member.id)
        now = time.time()
        normalized = re.sub(r"\s+", " ", content).casefold()
        if normalized and settings.get("anti_spam", "true") != "false":
            previous_hash, count, last_at = self.last_message_hash.get(key, ("", 0, 0.0))
            if normalized == previous_hash and now - last_at <= 30:
                count += 1
            else:
                count = 1
            self.last_message_hash[key] = (normalized, count, now)
            if count > 5:
                await self.apply_timeout(member, message, "Sending more than 5 identical messages (anti-raid rule)")
                await self.process_commands(message)
                return

        await self.process_commands(message)

    async def apply_timeout(self, member: discord.Member, message: discord.Message, reason: str):
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await member.timeout(timedelta(hours=1), reason=reason)
            await send_mod_log(message.guild, "Anti-raid timeout", member, reason)
        except discord.Forbidden:
            try:
                await message.channel.send(f"{member.mention}, I detected a rule violation but couldn't timeout you. Please contact staff.", delete_after=8)
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            pass

bot = OurhopeBot()


def is_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


def staff_check():
    async def predicate(ctx: commands.Context):
        if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
            await ctx.reply("You need **Manage Server** or **Administrator** to use this command.", mention_author=False)
            return False
        return True
    return commands.check(predicate)


def app_staff_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member) and is_staff(member):
            return True
        raise app_commands.CheckFailure("You need Manage Server or Administrator to use this command.")
    return app_commands.check(predicate)


async def ensure_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel:
    found = discord.utils.get(guild.text_channels, name=name)
    if found:
        return found
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True),
    }
    return await guild.create_text_channel(name, overwrites=overwrites, reason=f"{BOT_NAME}: create moderation log channel")


async def send_mod_log(guild: discord.Guild, title: str, member: discord.Member, reason: str):
    channel = await ensure_text_channel(guild, "blacklist" if title == "Member Blacklisted" else "unblacklist" if title == "Member Unblacklisted" else "mod-logs")
    embed = discord.Embed(title=title, color=discord.Color.red() if "Blacklisted" in title or "timeout" in title.lower() else discord.Color.blurple(), timestamp=utc_now())
    embed.add_field(name="Member", value=f"{member.mention}\n`{member}` · `{member.id}`", inline=False)
    embed.add_field(name="Action by", value="Automatic anti-raid system" if title == "Anti-raid timeout" else "Unknown", inline=False)
    embed.add_field(name="Reason", value=reason[:1024] if reason else "No reason provided", inline=False)
    embed.add_field(name="Time", value=discord.utils.format_dt(utc_now(), style="F"), inline=False)
    await channel.send(embed=embed)


async def ensure_blacklist_channels(guild: discord.Guild):
    # Public read-only channels; only staff and bot can post. They are created only if absent.
    for name in ("blacklist", "unblacklist"):
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True),
            }
            for role in guild.roles:
                if role.permissions.administrator or role.permissions.manage_guild:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            await guild.create_text_channel(name, overwrites=overwrites, reason=f"{BOT_NAME}: blacklist audit channel")


async def get_or_create_blacklisted_role(guild: discord.Guild) -> discord.Role:
    # Keep this role neutral/no-colour. Creating or adding it must never replace
    # any of the member's existing roles.
    role = discord.utils.get(guild.roles, name=BLACKLIST_ROLE)
    if role:
        if role.color != discord.Color.default():
            try:
                await role.edit(color=discord.Color.default(), reason=f"{BOT_NAME}: keep blacklist role colour neutral")
            except discord.HTTPException:
                pass
        return role
    return await guild.create_role(name=BLACKLIST_ROLE, color=discord.Color.default(), permissions=discord.Permissions.none(), reason=f"{BOT_NAME}: blacklist system")


async def deny_blacklist_role_everywhere(guild: discord.Guild, role: discord.Role):
    for channel in guild.channels:
        try:
            await channel.set_permissions(role, view_channel=False, reason="Blacklist privacy enforcement")
        except (discord.Forbidden, discord.HTTPException):
            continue


def parse_reason(raw: str) -> tuple[str, str]:
    match = re.search(r"(?:^|\s)\?r(?:eason)?\s+(.+)$", raw, re.I)
    if not match:
        return raw.strip(), "No reason provided"
    target = raw[:match.start()].strip()
    reason = match.group(1).strip()
    return target, reason or "No reason provided"


async def resolve_member(guild: discord.Guild, target: str) -> tuple[Optional[discord.Member], Optional[int]]:
    target = target.strip()
    mention = re.fullmatch(r"<@!?(\d{15,22})>", target)
    digits = re.fullmatch(r"\d{15,22}", target)
    user_id = int(mention.group(1)) if mention else int(digits.group(0)) if digits else None
    if user_id is None:
        return None, None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    return member, user_id


async def do_blacklist(guild: discord.Guild, actor: discord.Member, raw: str, add: bool, ctx=None):
    target_text, reason = parse_reason(raw)
    member, user_id = await resolve_member(guild, target_text)
    if user_id is None:
        msg = "Give me a member mention or Discord user ID. Example: `?bl 123456789012345678 ?r naughty`"
        if ctx: await ctx.reply(msg, mention_author=False)
        return
    role = await get_or_create_blacklisted_role(guild)
    await ensure_blacklist_channels(guild)
    await deny_blacklist_role_everywhere(guild, role)

    if add:
        if member is None:
            msg = "I couldn't find that member in this server. They must be in the server for me to assign the role."
            if ctx: await ctx.reply(msg, mention_author=False)
            return
        if member.id == actor.id:
            if ctx: await ctx.reply("You can't blacklist yourself.", mention_author=False)
            return
        # Save restorable roles once, then remove all roles the bot is allowed to manage.
        # @everyone cannot be removed; managed/integration roles and roles at/above
        # the bot's top role cannot be changed by Discord and are left untouched.
        if role >= guild.me.top_role:
            if ctx: await ctx.reply("I can't assign the Blacklisted role because it is at or above my highest role. Move ourhope's bot role above Blacklisted in Server Settings → Roles.", mention_author=False)
            return
        if role not in member.roles:
            removable_roles = [r for r in member.roles if r != guild.default_role and not r.managed and r < guild.me.top_role and r != role]
            with db_connect() as con:
                existing = con.execute("SELECT role_ids FROM blacklist_role_backups WHERE guild_id=? AND user_id=?", (guild.id, member.id)).fetchone()
                if existing is None:
                    con.execute("INSERT INTO blacklist_role_backups (guild_id, user_id, role_ids) VALUES (?, ?, ?)",
                                (guild.id, member.id, json.dumps([r.id for r in removable_roles])))
            try:
                if removable_roles:
                    await member.remove_roles(*removable_roles, reason=f"Roles removed during blacklist by {actor} ({actor.id})", atomic=True)
                await member.add_roles(role, reason=f"Blacklisted by {actor} ({actor.id}): {reason}", atomic=True)
            except discord.Forbidden:
                if ctx: await ctx.reply("I couldn't change the member's roles. Move ourhope's highest role above the roles it needs to remove and above Blacklisted.", mention_author=False)
                return
        channel = discord.utils.get(guild.text_channels, name="blacklist")
        title = "Member Blacklisted"
    else:
        if member is not None and role in member.roles:
            if role >= guild.me.top_role:
                if ctx: await ctx.reply("I can't remove the Blacklisted role because it is at or above my highest role. Move ourhope's bot role above Blacklisted in Server Settings → Roles.", mention_author=False)
                return
            await member.remove_roles(role, reason=f"Unblacklisted by {actor} ({actor.id}): {reason}", atomic=True)
            # Restore the roles that were present before blacklisting, when they
            # still exist and are below the bot's highest role.
            with db_connect() as con:
                backup = con.execute("SELECT role_ids FROM blacklist_role_backups WHERE guild_id=? AND user_id=?", (guild.id, member.id)).fetchone()
            if backup:
                saved_ids = set(json.loads(backup["role_ids"]))
                restore = [r for r in guild.roles if r.id in saved_ids and not r.managed and r < guild.me.top_role and r != guild.default_role]
                if restore:
                    try:
                        await member.add_roles(*[r for r in restore if r not in member.roles], reason=f"Restore roles after unblacklist by {actor} ({actor.id})", atomic=True)
                    except discord.Forbidden:
                        if ctx: await ctx.reply("The blacklist role was removed, but I couldn't restore some previous roles. Check my role hierarchy.", mention_author=False)
                with db_connect() as con:
                    con.execute("DELETE FROM blacklist_role_backups WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        channel = discord.utils.get(guild.text_channels, name="unblacklist")
        title = "Member Unblacklisted"

    if channel is None:
        channel = await ensure_text_channel(guild, "blacklist" if add else "unblacklist")
    embed = discord.Embed(title=title, color=discord.Color.red() if add else discord.Color.green(), timestamp=utc_now())
    embed.add_field(name="Member", value=(f"{member.mention}\n`{member}` · `{user_id}`" if member else f"`User ID: {user_id}` (not currently in server)"), inline=False)
    embed.add_field(name="Action by", value=f"{actor.mention}\n`{actor}` · `{actor.id}`", inline=False)
    embed.add_field(name="Reason", value=reason[:1024] if reason else "No reason provided", inline=False)
    embed.add_field(name="Time", value=discord.utils.format_dt(utc_now(), style="F"), inline=False)
    await channel.send(embed=embed)
    if ctx:
        await ctx.reply(f"{'Blacklisted' if add else 'Unblacklisted'} — logged in {channel.mention}.", mention_author=False)


@bot.command(name="blacklist", aliases=["bl"])
@staff_check()
async def blacklist_command(ctx: commands.Context, *, target_and_reason: str = ""):
    await do_blacklist(ctx.guild, ctx.author, target_and_reason, True, ctx)


@bot.command(name="unblacklist", aliases=["unbl"])
@staff_check()
async def unblacklist_command(ctx: commands.Context, *, target_and_reason: str = ""):
    await do_blacklist(ctx.guild, ctx.author, target_and_reason, False, ctx)


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.success, emoji="🎉", custom_id="ourhope:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message("This giveaway can't be entered here.", ephemeral=True)
        with db_connect() as con:
            row = con.execute("SELECT * FROM giveaways WHERE message_id=?", (interaction.message.id,)).fetchone()
            if not row or row["ended"] or row["ends_at"] <= time.time():
                return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            entrants = set(json.loads(row["entrants"]))
            if interaction.user.id in entrants:
                entrants.remove(interaction.user.id)
                con.execute("UPDATE giveaways SET entrants=? WHERE message_id=?", (json.dumps(sorted(entrants)), interaction.message.id))
                text = "You left the giveaway."
            else:
                entrants.add(interaction.user.id)
                con.execute("UPDATE giveaways SET entrants=? WHERE message_id=?", (json.dumps(sorted(entrants)), interaction.message.id))
                text = "You're entered! Good luck."
        await interaction.response.send_message(text, ephemeral=True)
        await refresh_giveaway_message(interaction.client, interaction.message.id)


async def refresh_giveaway_message(client, message_id: int):
    with db_connect() as con:
        row = con.execute("SELECT * FROM giveaways WHERE message_id=?", (message_id,)).fetchone()
    if not row or row["ended"]:
        return
    channel = client.get_channel(row["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        msg = await channel.fetch_message(message_id)
        embed = giveaway_embed(row)
        await msg.edit(embed=embed, view=GiveawayView())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def giveaway_embed(row) -> discord.Embed:
    entrants = json.loads(row["entrants"])
    embed = discord.Embed(title="🎉 GIVEAWAY", description=f"**Prize:** {row['prize']}\n**Winners:** {row['winners_count']}\n**Ends:** {fmt_time(row['ends_at'])}\n\nPress **Enter Giveaway** to join.", color=discord.Color.blurple())
    embed.add_field(name="Entries", value=str(len(entrants)), inline=True)
    embed.add_field(name="Hosted by", value=f"<@{row['host_id']}>", inline=True)
    embed.set_footer(text=f"ourhope giveaway • ID {row['message_id']}")
    return embed


@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.describe(prize="What is being given away", minutes="How many minutes it runs", winners="Number of winners")
@app_staff_check()
async def giveaway_slash(interaction: discord.Interaction, prize: str, minutes: app_commands.Range[int, 1, 10080], winners: app_commands.Range[int, 1, 20] = 1):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
    ends_at = time.time() + minutes * 60
    embed = discord.Embed(title="🎉 GIVEAWAY", description=f"**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** {fmt_time(ends_at)}\n\nPress **Enter Giveaway** to join.", color=discord.Color.blurple())
    embed.add_field(name="Entries", value="0", inline=True)
    embed.add_field(name="Hosted by", value=interaction.user.mention, inline=True)
    await interaction.response.send_message("Giveaway created!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed, view=GiveawayView())
    with db_connect() as con:
        con.execute("INSERT OR REPLACE INTO giveaways(message_id,guild_id,channel_id,host_id,prize,ends_at,winners_count,ended,entrants) VALUES(?,?,?,?,?,?,?,0,'[]')", (msg.id, interaction.guild.id, interaction.channel.id, interaction.user.id, prize, ends_at, winners))
    await refresh_giveaway_message(bot, msg.id)


@bot.tree.command(name="reroll", description="Reroll a completed giveaway winner")
@app_commands.describe(message_id="Message ID of the giveaway")
@app_staff_check()
async def reroll_slash(interaction: discord.Interaction, message_id: str):
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Give me the giveaway message ID.", ephemeral=True)
    with db_connect() as con:
        row = con.execute("SELECT * FROM giveaways WHERE message_id=? AND ended=1", (mid,)).fetchone()
    if not row:
        return await interaction.response.send_message("I couldn't find a completed giveaway with that message ID.", ephemeral=True)
    entrants = json.loads(row["entrants"])
    if not entrants:
        return await interaction.response.send_message("That giveaway had no entrants.", ephemeral=True)
    winner = random.choice(entrants)
    channel = interaction.client.get_channel(row["channel_id"])
    if isinstance(channel, discord.TextChannel):
        await channel.send(f"🎉 **Giveaway reroll!** New winner for **{row['prize']}**: <@{winner}>!")
    await interaction.response.send_message(f"Rerolled winner: <@{winner}>", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, button_text: str = "Open Ticket", button_emoji: str = "🎫"):
        super().__init__(timeout=None)
        # Customize the button shown on each posted panel while keeping a stable
        # custom_id so button clicks continue working after bot restarts.
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "ourhope:ticket:open":
                item.label = (button_text or "Open Ticket")[:80]
                item.emoji = button_emoji.strip() or None

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ourhope:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Open tickets from inside the server.", ephemeral=True)
        with db_connect() as con:
            row = con.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND closed=0", (guild.id, interaction.user.id)).fetchone()
        if row:
            existing = guild.get_channel(row["channel_id"])
            if existing:
                return await interaction.response.send_message(f"You already have an open ticket: {existing.mention}", ephemeral=True)
        # Put each ticket in the same category as the panel channel. If the panel
        # was posted outside a category, create the ticket outside a category too.
        # Never create a new category automatically.
        panel_channel = interaction.channel
        category = panel_channel.category if isinstance(panel_channel, discord.TextChannel) else None
        configured_category = guild_settings(guild.id).get("ticket_category", "").strip()
        if configured_category.isdigit():
            candidate = guild.get_channel(int(configured_category))
            if isinstance(candidate, discord.CategoryChannel): category = candidate
        support = discord.utils.get(guild.roles, name=SUPPORT_ROLE)
        configured_support = guild_settings(guild.id).get("support_role", "").strip()
        if configured_support.isdigit():
            candidate = guild.get_role(int(configured_support))
            if candidate: support = candidate
        if support is None:
            support = await guild.create_role(name=SUPPORT_ROLE, reason=f"{BOT_NAME}: ticket staff role")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
            support: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True),
        }
        # Use a readable, sanitized username instead of the random-looking user ID.
        base = re.sub(r"[^a-z0-9-]+", "-", interaction.user.name.lower()).strip("-") or "member"
        base = f"ticket-{base}"[:90].rstrip("-")
        channel_name = base
        suffix = 2
        while discord.utils.get(guild.text_channels, name=channel_name) is not None:
            channel_name = f"{base[:85]}-{suffix}"
            suffix += 1
        channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites, topic=f"Ticket opened by {interaction.user} ({interaction.user.id})")
        with db_connect() as con:
            con.execute("INSERT OR REPLACE INTO tickets(channel_id,guild_id,opener_id,claimed_by,closed,created_at) VALUES(?,?,?,NULL,0,?)", (channel.id, guild.id, interaction.user.id, time.time()))
        embed = discord.Embed(title="Support ticket", description=f"Hello {interaction.user.mention}! Tell the staff what you need help with.\n\nA staff member can claim this ticket. Use the buttons below when needed.", color=discord.Color.blurple())
        await channel.send(content=f"{interaction.user.mention} <@&{support.id}>", embed=embed, view=TicketControlsView())
        await interaction.response.send_message(f"Your ticket is ready: {channel.mention}", ephemeral=True)


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋", custom_id="ourhope:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("This button only works inside a ticket.", ephemeral=True)
        with db_connect() as con:
            row = con.execute("SELECT * FROM tickets WHERE channel_id=? AND closed=0", (interaction.channel.id,)).fetchone()
            if not row:
                return await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
            support = discord.utils.get(interaction.guild.roles, name=SUPPORT_ROLE)
            if not is_staff(interaction.user) and (support is None or support not in interaction.user.roles):
                return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            con.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (interaction.user.id, interaction.channel.id))
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ourhope:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("This button only works inside a ticket.", ephemeral=True)
        with db_connect() as con:
            row = con.execute("SELECT * FROM tickets WHERE channel_id=? AND closed=0", (interaction.channel.id,)).fetchone()
            if not row:
                return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
            support = discord.utils.get(interaction.guild.roles, name=SUPPORT_ROLE)
            if interaction.user.id != row["opener_id"] and not is_staff(interaction.user) and (support is None or support not in interaction.user.roles):
                return await interaction.response.send_message("Only the ticket opener or staff can close this ticket.", ephemeral=True)
            con.execute("UPDATE tickets SET closed=1 WHERE channel_id=?", (interaction.channel.id,))
        await interaction.response.send_message(f"Ticket closed by {interaction.user.mention}. This channel will be deleted in 10 seconds.")
        await asyncio.sleep(10)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


@bot.tree.command(name="ticketpanel", description="Customize and post the support ticket panel")
@app_commands.describe(
    title="Panel title",
    description="Text shown in the panel",
    emoji="Button emoji, for example 🎫 (leave blank for none)",
    button="Button label (defaults to Open Ticket)",
    footer="Small text at the bottom of the panel",
)
@app_staff_check()
async def ticketpanel_slash(
    interaction: discord.Interaction,
    title: str = "Need help?",
    description: str = "Need help? Open a private support ticket and our staff will assist you. Please open one ticket at a time.",
    emoji: str = "🎫",
    button: str = "Open Ticket",
    footer: str = "ourhope support",
):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
    embed = discord.Embed(
        title=title[:256] or "Need help?",
        description=description[:4000] or "Open a private support ticket and our staff will assist you.",
        color=discord.Color.from_rgb(35, 38, 48),
    )
    if footer.strip():
        embed.set_footer(text=footer[:2048])
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.channel.send(embed=embed, view=TicketPanelView(button_text=button, button_emoji=emoji))
    await interaction.followup.send("Customized ticket panel posted.", ephemeral=True)


@bot.tree.command(name="tickettranscript", description="Create a text transcript of the current ticket")
@app_staff_check()
async def tickettranscript_slash(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
    with db_connect() as con:
        row = con.execute("SELECT * FROM tickets WHERE channel_id=?", (channel.id,)).fetchone()
    if not row:
        return await interaction.response.send_message("This channel isn't registered as a ticket.", ephemeral=True)
    lines = [f"Transcript: #{channel.name}", f"Guild: {interaction.guild.name}", f"Created: {datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat()}", "=" * 60]
    async for msg in channel.history(limit=None, oldest_first=True):
        body = msg.content.replace("\n", " ")
        if msg.attachments:
            body += " " + " ".join(a.url for a in msg.attachments)
        lines.append(f"[{msg.created_at.isoformat()}] {msg.author} ({msg.author.id}): {body}")
    import io
    file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"transcript-{channel.id}.txt")
    await interaction.response.send_message("Ticket transcript:", file=file, ephemeral=True)


@giveaway_slash.error
@reroll_slash.error
@ticketpanel_slash.error
@tickettranscript_slash.error
async def slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = str(error) if isinstance(error, app_commands.CheckFailure) else "Something went wrong. Check the bot's permissions and console."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@tasks.loop(seconds=10)
async def giveaway_watcher():
    now = time.time()
    with db_connect() as con:
        rows = con.execute("SELECT * FROM giveaways WHERE ended=0 AND ends_at<=?", (now,)).fetchall()
    for row in rows:
        entrants = json.loads(row["entrants"])
        winners = random.sample(entrants, min(len(entrants), row["winners_count"])) if entrants else []
        with db_connect() as con:
            con.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (row["message_id"],))
        channel = bot.get_channel(row["channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(row["message_id"])
                embed = discord.Embed(title="🎉 GIVEAWAY ENDED", description=f"**Prize:** {row['prize']}\n**Entries:** {len(entrants)}\n**Ended:** {fmt_time(now)}", color=discord.Color.dark_grey())
                embed.add_field(name="Winner(s)", value=", ".join(f"<@{uid}>" for uid in winners) if winners else "No valid entries", inline=False)
                await msg.edit(embed=embed, view=None)
                if winners:
                    await channel.send(f"🎉 Congratulations {', '.join(f'<@{uid}>' for uid in winners)}! You won **{row['prize']}**!")
                else:
                    await channel.send(f"The giveaway for **{row['prize']}** ended without any entries.")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


@giveaway_watcher.before_loop
async def before_giveaway_watcher():
    await bot.wait_until_ready()



# Prefix moderation shortcuts requested by the server owner.
def record_mod_action(guild_id: int, moderator_id: int, target_id: int, action: str, reason: str):
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS mod_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)""")
        con.execute("INSERT INTO mod_actions(guild_id,moderator_id,target_id,action,reason,created_at) VALUES(?,?,?,?,?,?)",
                    (guild_id, moderator_id, target_id, action, (reason or "No reason provided")[:1000], time.time()))


def can_moderate(ctx: commands.Context, member: discord.Member) -> bool:
    if not isinstance(ctx.author, discord.Member) or not ctx.guild or not ctx.guild.me:
        return False
    if member.id == ctx.author.id or member.id == ctx.guild.owner_id:
        return False
    if member.top_role >= ctx.guild.me.top_role or member.guild_permissions.administrator:
        return False
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        return False
    return True


def parse_reason_marker(text: str, default: str = "No reason provided") -> str:
    """Accept the Bleed-style '?r reason' marker, while tolerating plain reasons."""
    text = (text or "").strip()
    if not text:
        return default
    import re
    match = re.search(r"(?:^|\s)\?r\s+(.+)$", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip() or default
    return text


def parse_timeout_duration(value: str) -> int:
    """Parse 30m, 2h, 1d; Discord timeout maximum is 28 days."""
    import re
    match = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", value.lower())
    if not match:
        raise ValueError("Use a duration like `30m`, `2h`, or `1d`.")
    amount, unit = int(match.group(1)), match.group(2)
    minutes = amount * {"m": 1, "h": 60, "d": 1440}[unit]
    if minutes < 1 or minutes > 28 * 24 * 60:
        raise ValueError("Timeout must be between 1 minute and 28 days.")
    return minutes


@bot.command(name="m")
@commands.guild_only()
async def mute_shortcut(ctx: commands.Context, member: discord.Member, duration: str = "10m", *, reason: str = ""):
    """Usage: ,m @member 30m ?r reason (maximum 28 days)."""
    try:
        minutes = parse_timeout_duration(duration)
    except ValueError as exc:
        return await ctx.reply(str(exc), mention_author=False)
    reason = parse_reason_marker(reason)
    if not ctx.author.guild_permissions.moderate_members:
        return await ctx.reply("You need **Timeout Members** permission.", mention_author=False)
    if not can_moderate(ctx, member):
        return await ctx.reply("I can't moderate that member because of role hierarchy or permissions.", mention_author=False)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
        record_mod_action(ctx.guild.id, ctx.author.id, member.id, "Mute/timeout", reason)
        duration_text = f"{minutes} minutes" if minutes < 60 else (f"{minutes // 60} hours" if minutes < 1440 else f"{minutes // 1440} days")
        await ctx.reply(f"🔇 Timed out {member.mention} for **{duration_text}**. Reason: {reason}", mention_author=False)
        await send_mod_log(ctx.guild, "Manual timeout", member, f"By {ctx.author} · {duration_text} · {reason}")
    except discord.Forbidden:
        await ctx.reply("I need **Moderate Members** permission and a higher role than that member.", mention_author=False)


@bot.command(name="k")
@commands.guild_only()
async def kick_shortcut(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    """Usage: ,k @member ?r reason"""
    reason = parse_reason_marker(reason)
    if not ctx.author.guild_permissions.kick_members:
        return await ctx.reply("You need **Kick Members** permission.", mention_author=False)
    if not can_moderate(ctx, member):
        return await ctx.reply("I can't moderate that member because of role hierarchy or permissions.", mention_author=False)
    try:
        await member.kick(reason=f"{ctx.author}: {reason}")
        record_mod_action(ctx.guild.id, ctx.author.id, member.id, "Kick", reason)
        await ctx.reply(f"👢 Kicked **{member}**. Reason: {reason}", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("I need **Kick Members** permission and a higher role than that member.", mention_author=False)


@bot.command(name="b")
@commands.guild_only()
async def ban_shortcut(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    """Usage: ,b @member ?r reason"""
    reason = parse_reason_marker(reason)
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.reply("You need **Ban Members** permission.", mention_author=False)
    if not can_moderate(ctx, member):
        return await ctx.reply("I can't moderate that member because of role hierarchy or permissions.", mention_author=False)
    try:
        await member.ban(reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
        record_mod_action(ctx.guild.id, ctx.author.id, member.id, "Ban", reason)
        await ctx.reply(f"🔨 Banned **{member}**. Reason: {reason}", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("I need **Ban Members** permission and a higher role than that member.", mention_author=False)


@bot.command(name="hb")
@commands.guild_only()
async def hardban_shortcut(ctx: commands.Context, user_id: int, *, reason: str = "Hard ban"):
    """Usage: ,hb USER_ID ?r reason. Deletes the last 7 days of messages."""
    reason = parse_reason_marker(reason, "Hard ban")
    if not ctx.author.guild_permissions.ban_members:
        return await ctx.reply("You need **Ban Members** permission.", mention_author=False)
    if ctx.author.id == user_id or user_id == ctx.guild.owner_id:
        return await ctx.reply("You can't hard-ban yourself or the server owner.", mention_author=False)
    try:
        await ctx.guild.fetch_ban(discord.Object(id=user_id))
        return await ctx.reply("That user is already banned.", mention_author=False)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass
    try:
        await ctx.guild.ban(discord.Object(id=user_id), reason=f"Hard ban by {ctx.author}: {reason}", delete_message_seconds=604800)
        record_mod_action(ctx.guild.id, ctx.author.id, user_id, "Hard ban (7-day message deletion)", reason)
        await ctx.reply(f"⛔ Hard-banned user ID `{user_id}` and deleted up to 7 days of their messages. Reason: {reason}", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("I need **Ban Members** permission to hard-ban users.", mention_author=False)
    except discord.HTTPException as exc:
        await ctx.reply(f"Discord rejected the hard ban: {exc}", mention_author=False)


@bot.command(name="ms")
@commands.guild_only()
async def modstats_shortcut(ctx: commands.Context, moderator: Optional[discord.Member] = None):
    """Usage: ,ms [@moderator]. Shows recent actions performed by you, or the mentioned moderator (staff only)."""
    moderator = moderator or ctx.author
    if moderator.id != ctx.author.id and not is_staff(ctx.author):
        return await ctx.reply("You can only view your own mod stats unless you're server staff.", mention_author=False)
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS mod_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)""")
        rows = con.execute("SELECT action,target_id,reason,created_at FROM mod_actions WHERE guild_id=? AND moderator_id=? ORDER BY id DESC LIMIT 10",
                           (ctx.guild.id, moderator.id)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM mod_actions WHERE guild_id=? AND moderator_id=?", (ctx.guild.id, moderator.id)).fetchone()[0]
    embed = discord.Embed(title=f"Mod stats · {moderator.display_name}", description=f"Recorded actions: **{total}** · Showing latest {len(rows)}", color=discord.Color.dark_grey())
    if not rows:
        embed.description = "No recorded moderation actions yet. New `,m`, `,k`, `,b`, and `,hb` actions will appear here."
    for i, row in enumerate(rows, 1):
        embed.add_field(name=f"{i}. {row['action']} · <t:{int(row['created_at'])}:R>",
                        value=f"Target: <@{row['target_id']}> (`{row['target_id']}`)\nReason: {row['reason'][:500]}", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


# Additional moderation and community commands in the clean rebuild.
@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason")
@app_staff_check()
async def timeout_slash(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    if member.guild_permissions.administrator or member.top_role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("I can't timeout that member because of Discord permission or role hierarchy.", ephemeral=True)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(f"Timed out {member.mention} for {minutes} minute(s). Reason: {reason[:500]}")
        await send_mod_log(interaction.guild, "Manual timeout", member, reason)
    except discord.Forbidden:
        await interaction.response.send_message("I lack permission to timeout that member.", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
@app_staff_check()
async def kick_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.guild.me.top_role or member.guild_permissions.administrator:
        return await interaction.response.send_message("I can't kick that member due to role hierarchy or administrator protection.", ephemeral=True)
    try:
        await member.kick(reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(f"Kicked **{member}**. Reason: {reason[:500]}")
    except discord.Forbidden:
        await interaction.response.send_message("I lack permission to kick that member.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason", delete_days="Days of message history to delete (0-7)")
@app_staff_check()
async def ban_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0):
    if member.top_role >= interaction.guild.me.top_role or member.guild_permissions.administrator:
        return await interaction.response.send_message("I can't ban that member due to role hierarchy or administrator protection.", ephemeral=True)
    try:
        await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
        await interaction.response.send_message(f"Banned **{member}**. Reason: {reason[:500]}")
    except discord.Forbidden:
        await interaction.response.send_message("I lack permission to ban that member.", ephemeral=True)

@bot.tree.command(name="purge", description="Delete recent messages from this channel")
@app_commands.describe(amount="Number of messages to delete, up to 100")
@app_staff_check()
async def purge_slash(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted=await interaction.channel.purge(limit=amount, reason=f"Purge by {interaction.user} ({interaction.user.id})")
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I need Manage Messages permission.", ephemeral=True)

@bot.tree.command(name="warn", description="Record a moderation warning for a member")
@app_commands.describe(member="Member to warn", reason="Reason for the warning")
@app_staff_check()
async def warn_slash(interaction: discord.Interaction, member: discord.Member, reason: str):
    with db_connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)")
        con.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)",(interaction.guild.id,member.id,interaction.user.id,reason[:1000],time.time()))
    try: await member.send(f"You were warned in **{interaction.guild.name}**. Reason: {reason[:1000]}")
    except discord.HTTPException: pass
    await interaction.response.send_message(f"Recorded a warning for {member.mention}. Reason: {reason[:500]}", ephemeral=True)

@bot.tree.command(name="warnings", description="View a member's recorded warnings")
@app_commands.describe(member="Member whose warnings to view")
@app_staff_check()
async def warnings_slash(interaction: discord.Interaction, member: discord.Member):
    with db_connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)")
        rows=con.execute("SELECT moderator_id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",(interaction.guild.id,member.id)).fetchall()
    if not rows: return await interaction.response.send_message(f"No recorded warnings for {member.mention}.",ephemeral=True)
    embed=discord.Embed(title=f"Warnings for {member}",color=discord.Color.orange())
    for i,row in enumerate(rows,1): embed.add_field(name=f"{i}. <t:{int(row['created_at'])}:R> · Moderator {row['moderator_id']}",value=row['reason'][:1024],inline=False)
    await interaction.response.send_message(embed=embed,ephemeral=True)

@bot.tree.command(name="suggest", description="Submit a server suggestion")
@app_commands.describe(suggestion="Your suggestion")
async def suggest_slash(interaction: discord.Interaction, suggestion: str):
    if not interaction.guild: return await interaction.response.send_message("Use this in a server.",ephemeral=True)
    channel=discord.utils.get(interaction.guild.text_channels,name="suggestions")
    if channel is None:
        overwrites={interaction.guild.default_role:discord.PermissionOverwrite(view_channel=True,send_messages=False,read_message_history=True), interaction.guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,add_reactions=True)}
        channel=await interaction.guild.create_text_channel("suggestions",overwrites=overwrites,reason="ourhope suggestions")
    embed=discord.Embed(title="New suggestion",description=suggestion[:4000],color=discord.Color.blurple(),timestamp=utc_now())
    embed.set_author(name=str(interaction.user),icon_url=interaction.user.display_avatar.url)
    msg=await channel.send(embed=embed)
    for emoji in ("👍","👎"): 
        try: await msg.add_reaction(emoji)
        except discord.HTTPException: pass
    await interaction.response.send_message(f"Suggestion submitted to {channel.mention}.",ephemeral=True)

@bot.tree.command(name="serverinfo", description="Show information about this server")
async def serverinfo_slash(interaction: discord.Interaction):
    g=interaction.guild
    if not g: return await interaction.response.send_message("Use this in a server.",ephemeral=True)
    e=discord.Embed(title=g.name,color=discord.Color.blurple(),timestamp=utc_now())
    e.add_field(name="Members",value=str(g.member_count or "unknown")); e.add_field(name="Channels",value=str(len(g.channels))); e.add_field(name="Roles",value=str(len(g.roles)))
    e.add_field(name="Created",value=discord.utils.format_dt(g.created_at,style="D"),inline=False)
    if g.icon: e.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=e)


async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print(f"Welcome to {BOT_NAME} setup.")
        print("Paste your bot token below. It is not written to a file.")
        token = getpass.getpass("Discord bot token: ").strip()
    if not token:
        print("No token entered. Exiting.")
        return
    await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nourhope stopped.")
