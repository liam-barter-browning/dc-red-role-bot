"""
UserHandle cog for Red - DiscordBot.
Gives each member a role matching their display name (or custom handle) and keeps it in sync.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import Config, commands

try:
    from discord.http import Route
except ImportError:
    Route = None

__version__ = "2.5"
log = logging.getLogger("red.cog.user_handle")


def _normalize_info(info: dict) -> dict:
    """Normalize stored info to sync_role_id and custom_roles (list of {role_id, name}). Handles old format."""
    custom_roles = list(info.get("custom_roles") or [])
    # Migrate from single custom_role_id + custom_name
    old_id, old_name = info.get("custom_role_id"), info.get("custom_name")
    if old_id is not None and not any(c.get("role_id") == old_id for c in custom_roles):
        custom_roles.append({"role_id": old_id, "name": (old_name or "custom")})
    sync_role_id = info.get("sync_role_id")
    legacy_role_id = info.get("role_id")
    if legacy_role_id is not None and sync_role_id is None and not custom_roles:
        if old_name:
            custom_roles = [{"role_id": legacy_role_id, "name": old_name}]
        else:
            sync_role_id = legacy_role_id
    return {"sync_role_id": sync_role_id, "custom_roles": custom_roles}


def _display_name(member: discord.Member) -> str:
    """Server display name: nickname if set, else username."""
    return member.display_name or member.name


async def _fetch_guild_members_via_rest(bot, guild: discord.Guild):
    """Fetch all guild members via REST API. Use when cache is empty (e.g. Red with strict member cache)."""
    if Route is None:
        return None
    state = getattr(bot, "_connection", None) or getattr(bot, "connection", None)
    if not state or not hasattr(bot, "http"):
        return None
    members = []
    after = 0
    try:
        while True:
            # Discord API: GET /guilds/{id}/members?limit=1000&after={after}
            route = Route("GET", "/guilds/{guild_id}/members", guild_id=guild.id)
            params = {"limit": 1000, "after": after}
            data = await bot.http.request(route, params=params)
            if not data:
                break
            for mdata in data:
                try:
                    member = discord.Member(data=mdata, guild=guild, state=state)
                    if not member.bot:
                        members.append(member)
                except Exception:
                    continue
            if len(data) < 1000:
                break
            after = int(data[-1]["user"]["id"])
            await asyncio.sleep(0.5)
    except (discord.Forbidden, discord.HTTPException, AttributeError, KeyError) as e:
        log.warning("UserHandle: REST fetch_members failed for guild %s: %s", guild.id, e)
        return None
    return members


class UserHandle(commands.Cog):
    """Per-user role tags synced to display name or custom handle."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x726F6C655F746167, force_registration=True)
        self.config.register_guild(
            role_assignments={},
            log_dm_user_id=None,  # user id to DM (toggle via !userhandle logdm); cleared when log channel is set
            log_channel_id=None,  # channel to send logs to (set via !userhandle logchannel); cleared when logdm is set
            role_blacklist=[],  # role names the bot must never create or add to tracked handles
        )
        self._sync_lock = asyncio.Lock()
        self._last_sync_error: Optional[str] = None  # for reporting when sync creates 0 roles

    async def _send_log_dm(self, guild: discord.Guild, message: str) -> None:
        """Send admin log to configured target: channel if set, otherwise DM user. Swallows errors."""
        header = f"**UserHandle** — **Server:** {guild.name} (`{guild.id}`)"
        full = f"{header}\n{message}"
        log_channel_id = await self.config.guild(guild).log_channel_id()
        if log_channel_id:
            channel = guild.get_channel(log_channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(log_channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning("UserHandle: log channel %s not found or inaccessible in guild %s: %s", log_channel_id, guild.id, e)
                    return
            if channel is not None and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(full)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("UserHandle: could not send log to channel %s in guild %s: %s", log_channel_id, guild.id, e)
            elif channel is not None:
                log.warning("UserHandle: log channel %s in guild %s is not a text channel", log_channel_id, guild.id)
            return
        log_dm_id = await self.config.guild(guild).log_dm_user_id()
        if not log_dm_id:
            return
        user = self.bot.get_user(log_dm_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(log_dm_id)
            except (discord.NotFound, discord.HTTPException):
                return
        if user is None:
            return
        try:
            await user.send(full)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _is_role_name_blacklisted(self, guild: discord.Guild, name: str) -> bool:
        """True if this role name is blacklisted (case-insensitive). Protects special/restriction roles."""
        if not (name or "").strip():
            return False
        blacklist = await self.config.guild(guild).role_blacklist()
        n = (name or "").strip().lower()
        return any((b or "").strip().lower() == n for b in (blacklist or []))

    async def _is_handle_name_taken_by_another(
        self, guild: discord.Guild, current_user_id: int, name: str
    ) -> bool:
        """True if another user in this guild already has this handle name in storage (case-insensitive)."""
        if not (name or "").strip():
            return False
        want = (name or "").strip().lower()
        assignments = await self.config.guild(guild).role_assignments()
        for user_id_str, data in (assignments or {}).items():
            if user_id_str == str(current_user_id):
                continue
            info = _normalize_info(data or {})
            for c in info.get("custom_roles") or []:
                if (c.get("name") or "").strip().lower() == want:
                    return True
        return False

    async def cog_load(self) -> None:
        self.sync_role_names.start()

    def cog_unload(self) -> None:
        self.sync_role_names.cancel()

    @tasks.loop(minutes=5.0)
    async def sync_role_names(self) -> None:
        """Background task: update bot-managed role names to match current display names."""
        await self.bot.wait_until_ready()
        async with self._sync_lock:
            for guild in self.bot.guilds:
                try:
                    await self._sync_guild_roles(guild)
                    # No logging for background sync (auto-generated display-name roles) to avoid noise
                except Exception as e:
                    log.exception("UserHandle sync failed for guild %s: %s", guild.id, e)
                await asyncio.sleep(0.5)  # avoid hammering the API

    @sync_role_names.before_loop
    async def before_sync_role_names(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild_roles(
        self, guild: discord.Guild
    ) -> Optional[tuple[int, list[tuple[str, str, str]]]]:
        """Background: only update sync (display-name) roles. Returns (updated_count, [(display_name, username, change_text), ...]) or None if skipped."""
        data = await self.config.guild(guild).role_assignments()
        if not data:
            return None
        try:
            await asyncio.wait_for(guild.chunk(), timeout=10.0)
        except (discord.HTTPException, asyncio.TimeoutError):
            pass
        existing_names = {r.name for r in guild.roles}
        updated = 0
        details: list[tuple[str, str, str]] = []  # (display_name, username, change_text)
        for user_id_str, info in list(data.items()):
            try:
                info = _normalize_info(info)
                sync_role_id = info.get("sync_role_id")
                if not sync_role_id:
                    continue
                role = guild.get_role(sync_role_id)
                if not role:
                    async with self.config.guild(guild).role_assignments() as assignments:
                        a = _normalize_info(assignments.get(user_id_str) or {})
                        a["sync_role_id"] = None
                        if a.get("custom_roles"):
                            assignments[user_id_str] = {"sync_role_id": a.get("sync_role_id"), "custom_roles": a.get("custom_roles")}
                        else:
                            assignments.pop(user_id_str, None)
                    continue
                member = guild.get_member(int(user_id_str))
                if not member:
                    continue
                desired_name = (_display_name(member) or member.name).strip() or member.name
                dname, uname = member.display_name, member.name
                if role.name == desired_name:
                    if not member.get_role(sync_role_id):
                        try:
                            await member.add_roles(role, reason="UserHandle: re-add sync role")
                            updated += 1
                            details.append((dname, uname, "sync role re-added to member"))
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                    continue
                unique_name = self._unique_role_name(guild, desired_name, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: sync display name")
                    existing_names.discard(role.name)
                    existing_names.add(unique_name)
                    updated += 1
                    details.append((dname, uname, f"sync role renamed to **{unique_name}**"))
                except (discord.Forbidden, discord.HTTPException):
                    pass
                if not member.get_role(sync_role_id):
                    try:
                        await member.add_roles(role, reason="UserHandle: re-add sync role")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            except (ValueError, KeyError):
                pass
            await asyncio.sleep(0.2)
        return (updated, details)

    def _unique_role_name(
        self,
        guild: discord.Guild,
        base_name: str,
        existing_names: Optional[set[str]] = None,
        *,
        exclude_role: Optional[discord.Role] = None,
    ) -> str:
        """Return a role name that does not already exist in the guild."""
        if existing_names is None:
            existing_names = {r.name for r in guild.roles}
        if exclude_role and exclude_role.name in existing_names:
            existing_names = existing_names - {exclude_role.name}
        if base_name not in existing_names:
            return base_name
        i = 2
        while f"{base_name} ({i})" in existing_names:
            i += 1
        return f"{base_name} ({i})"

    async def _ensure_sync_role(self, guild: discord.Guild, member: discord.Member) -> Optional[discord.Role]:
        """Create or update only the sync (display-name) role for this member. Only touches roles we created (in config)."""
        user_id_str = str(member.id)
        async with self.config.guild(guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            sync_role_id = info.get("sync_role_id")
        name_to_use = (_display_name(member) or member.name).strip() or member.name
        existing_names = {r.name for r in guild.roles}
        role = guild.get_role(sync_role_id) if sync_role_id else None
        if role is None:
            unique_name = self._unique_role_name(guild, name_to_use, existing_names)
            try:
                role = await guild.create_role(
                    name=unique_name,
                    reason="UserHandle: create sync role",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("UserHandle: could not create sync role in guild %s: %s", guild.id, e)
                if self._last_sync_error is None:
                    self._last_sync_error = str(e)
                return None
            existing_names.add(role.name)
            async with self.config.guild(guild).role_assignments() as assignments:
                entry = _normalize_info(assignments.get(user_id_str) or {})
                entry["sync_role_id"] = role.id
                assignments[user_id_str] = {"sync_role_id": entry.get("sync_role_id"), "custom_roles": entry.get("custom_roles", [])}
        else:
            if role.name != name_to_use:
                unique_name = self._unique_role_name(guild, name_to_use, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: sync display name")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        try:
            if not member.get_role(role.id):
                await member.add_roles(role, reason="UserHandle: assign sync role")
        except (discord.Forbidden, discord.HTTPException) as e:
            if self._last_sync_error is None:
                self._last_sync_error = str(e)
            return role
        return role

    async def _ensure_custom_role(
        self, guild: discord.Guild, member: discord.Member, custom_name: str
    ) -> Optional[discord.Role]:
        """Add a new custom handle role. Only roles we CREATE are tracked; we never adopt existing server roles (e.g. restriction roles)."""
        custom_name = (custom_name or "").strip() or member.name
        if await self._is_role_name_blacklisted(guild, custom_name):
            return None  # Caller should check and send a friendly message
        user_id_str = str(member.id)
        async with self.config.guild(guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            custom_roles = list(info.get("custom_roles") or [])
        # Only roles in custom_roles were created by us; if user already has one with this name, ensure they have it
        for c in custom_roles:
            rid = c.get("role_id")
            if rid and c.get("name") == custom_name:
                role = guild.get_role(rid)
                if role and not member.get_role(rid):
                    try:
                        await member.add_roles(role, reason="UserHandle: assign custom handle")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        if self._last_sync_error is None:
                            self._last_sync_error = str(e)
                return role
        # Never create a role when the requested name is blacklisted (e.g. don't create "Moderator (2)" if "Moderator" is blacklisted)
        existing_names = {r.name for r in guild.roles}
        unique_name = self._unique_role_name(guild, custom_name, existing_names)
        if await self._is_role_name_blacklisted(guild, unique_name):
            return None
        # Base name (before any " (2)" suffix) must also be blacklist-checked so we never create blacklisted-name (2)
        base_name = unique_name.split(" (")[0].strip() if " (" in unique_name else unique_name
        if await self._is_role_name_blacklisted(guild, base_name):
            return None
        # Create new role and add to tracked list only when we create it (never adopt existing server roles)
        try:
            role = await guild.create_role(
                name=unique_name,
                reason="UserHandle: create custom handle",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("UserHandle: could not create custom role in guild %s: %s", guild.id, e)
            if self._last_sync_error is None:
                self._last_sync_error = str(e)
            return None
        custom_roles.append({"role_id": role.id, "name": role.name})
        async with self.config.guild(guild).role_assignments() as assignments:
            entry = _normalize_info(assignments.get(user_id_str) or {})
            entry["custom_roles"] = custom_roles
            assignments[user_id_str] = {"sync_role_id": entry.get("sync_role_id"), "custom_roles": entry["custom_roles"]}
        try:
            if not member.get_role(role.id):
                await member.add_roles(role, reason="UserHandle: assign custom handle")
        except (discord.Forbidden, discord.HTTPException) as e:
            if self._last_sync_error is None:
                self._last_sync_error = str(e)
        return role

    def _is_admin_or_manage_roles(self, ctx: commands.Context) -> bool:
        """True if the author has Administrator or Manage Roles in this guild."""
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_roles

    @commands.group(name="userhandle", invoke_without_command=True)
    async def userhandle(self, ctx: commands.Context) -> None:
        """User handles: a role per member matching their display name (or custom handle) for easier tagging."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @userhandle.command(name="help")
    async def userhandle_help(self, ctx: commands.Context) -> None:
        """Show a guide on how to use UserHandle. Admins see admin-only commands too."""
        p = ctx.clean_prefix
        embed = discord.Embed(
            title="UserHandle — Usage guide",
            description=(
                "This cog gives you a **display-name role** (synced with your server nickname) "
                "and lets you add **custom handle** roles so others can @mention you by those names from any channel."
            ),
            color=0x5865F2,  # Discord blurple
        )
        embed.add_field(
            name="Your roles",
            value=(
                "• **Display-name role** — Created automatically; its name matches your server nickname (or username). "
                "It updates when you change your nickname.\n"
                "• **Custom handles** — Roles you add with the commands below. You can have multiple; only roles *created by this bot* via `set` are tracked."
            ),
            inline=False,
        )
        embed.add_field(
            name="Commands (everyone)",
            value=(
                f"**{p}userhandle set <name>** — Add a custom handle. Only adds; does not remove others.\n"
                f"**{p}userhandle remove <name>** — Remove one custom handle (only those you added with `set`).\n"
                f"**{p}userhandle clear** — Remove all your custom handles. Display-name role is kept."
            ),
            inline=False,
        )
        if self._is_admin_or_manage_roles(ctx):
            embed.add_field(
                name="Commands (admin)",
                value=(
                    f"**{p}userhandle sync** — Ensure every member has a display-name role (run once for existing members).\n"
                    f"**{p}userhandle logdm** — Toggle DMs for set/clear/remove (not for auto display-name sync).\n"
                    f"**{p}userhandle logchannel [#channel]** — Send logs to a channel instead of DMs (no channel = off).\n"
                    f"**{p}userhandle blacklist** — List reserved role names the bot will never create.\n"
                    f"**{p}userhandle blacklist add <name>** — Reserve a role name.\n"
                    f"**{p}userhandle blacklist remove <name>** — Unreserve a role name."
                ),
                inline=False,
            )
        embed.set_footer(text=f"UserHandle v{__version__} • Use {p}help userhandle for command list.")
        await ctx.send(embed=embed)

    @userhandle.command(name="set")
    async def userhandle_set(self, ctx: commands.Context, *, name: str) -> None:
        """Add a custom handle (role) for yourself. You keep your display-name role and gain this one too."""
        name = name.strip()
        if not name:
            await ctx.send("Please provide a non-empty name.")
            return
        if len(name) > 100:
            await ctx.send("Name must be 100 characters or fewer.")
            return
        sync_role = await self._ensure_sync_role(ctx.guild, ctx.author)
        if sync_role is None:
            await ctx.send("I couldn't create or update your display-name role. Check that my role is above the roles I create and I have *Manage Roles*.")
            return
        if await self._is_role_name_blacklisted(ctx.guild, name):
            await ctx.send("That handle is blacklisted by server admins and can't be used.")
            return
        if await self._is_handle_name_taken_by_another(ctx.guild, ctx.author.id, name):
            await ctx.send("That handle is already in use by another member.")
            return
        custom_role = await self._ensure_custom_role(ctx.guild, ctx.author, name)
        if custom_role is None:
            # Distinguish blacklist (don't create Name (2)) from other failures
            if await self._is_role_name_blacklisted(ctx.guild, name):
                await ctx.send("That handle is blacklisted by server admins and can't be used.")
            else:
                await ctx.send("I couldn't create or update your custom handle. Check permissions.")
            return
        info = _normalize_info((await self.config.guild(ctx.guild).role_assignments()).get(str(ctx.author.id)) or {})
        custom_names = [c.get("name") for c in (info.get("custom_roles") or []) if c.get("name")]
        custom_txt = ", ".join(f"**{n}**" for n in custom_names) if custom_names else custom_role.name
        await ctx.send(f"Added **{custom_role.name}**. You now have: **{sync_role.name}** (display name) and {custom_txt} (custom handle(s)).")
        await self._send_log_dm(
            ctx.guild,
            f"**Success (set)** — Custom handle added (no other tags removed).\n"
            f"• **User affected:** {ctx.author.display_name} (username: `{ctx.author.name}`, id: `{ctx.author.id}`)\n"
            f"• **Changes applied:** Assigned new custom handle role **{custom_role.name}**. All existing tags kept. Custom handles now: {custom_txt}."
        )

    @userhandle.command(name="clear")
    async def userhandle_clear(self, ctx: commands.Context) -> None:
        """Remove all your custom handle roles. Your display-name role is kept and will stay in sync."""
        user_id_str = str(ctx.author.id)
        async with self.config.guild(ctx.guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            custom_roles = list(info.get("custom_roles") or [])
            if not custom_roles:
                await ctx.send("You don't have any custom handles set in this server.")
                return
            removed_names = []
            for c in custom_roles:
                rid = c.get("role_id")
                name = c.get("name", "?")
                if rid:
                    role = ctx.guild.get_role(rid)
                    if role and ctx.author.get_role(rid):
                        try:
                            await ctx.author.remove_roles(role, reason="UserHandle: clear custom handle")
                            removed_names.append(name)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            entry = _normalize_info(assignments.get(user_id_str) or {})
            entry["custom_roles"] = []
            if entry.get("sync_role_id") is None:
                assignments.pop(user_id_str, None)
            else:
                assignments[user_id_str] = {"sync_role_id": entry["sync_role_id"], "custom_roles": []}
        n = len(removed_names)
        await ctx.send(f"Custom handle(s) removed ({n} role(s)). Your display-name role is unchanged and will keep syncing.")
        names_txt = ", ".join(f"**{x}**" for x in removed_names) if removed_names else "—"
        await self._send_log_dm(
            ctx.guild,
            f"**Success (clear)** — Custom handle(s) removed.\n"
            f"• **User affected:** {ctx.author.display_name} (username: `{ctx.author.name}`, id: `{ctx.author.id}`)\n"
            f"• **Changes applied:** Removed custom handle role(s): {names_txt}. Sync role left unchanged."
        )

    @userhandle.command(name="remove")
    async def userhandle_remove(self, ctx: commands.Context, *, name: str) -> None:
        """Remove one custom handle (only handles added by this bot via set). Your display-name role is unchanged."""
        name = (name or "").strip()
        if not name:
            await ctx.send("Please provide the name of the custom handle to remove.")
            return
        user_id_str = str(ctx.author.id)
        async with self.config.guild(ctx.guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            custom_roles = list(info.get("custom_roles") or [])
            # Only remove if this role is in our tracked list (created by set)
            match_idx = None
            for i, c in enumerate(custom_roles):
                if (c.get("name") or "").strip() == name:
                    match_idx = i
                    break
            if match_idx is None:
                tracked = [c.get("name") for c in custom_roles if c.get("name")]
                if not tracked:
                    await ctx.send("You don't have any custom handles from this bot. Use `!userhandle set <name>` to add one.")
                else:
                    await ctx.send(
                        f"**{name}** isn't in your tracked handles. You can only remove handles you added with `!userhandle set`. "
                        f"Your tracked handles: {', '.join(f'**{n}**' for n in tracked)}."
                    )
                return
            rid = custom_roles[match_idx].get("role_id")
            role = ctx.guild.get_role(rid) if rid else None
            if role and ctx.author.get_role(rid):
                try:
                    await ctx.author.remove_roles(role, reason="UserHandle: remove custom handle")
                except (discord.Forbidden, discord.HTTPException):
                    await ctx.send("I don't have permission to remove that role from you.")
                    return
            new_list = [c for i, c in enumerate(custom_roles) if i != match_idx]
            entry = _normalize_info(assignments.get(user_id_str) or {})
            entry["custom_roles"] = new_list
            if not new_list and entry.get("sync_role_id") is None:
                assignments.pop(user_id_str, None)
            else:
                assignments[user_id_str] = {"sync_role_id": entry.get("sync_role_id"), "custom_roles": new_list}
        await ctx.send(f"Removed custom handle **{name}**. Your display-name role is unchanged.")
        await self._send_log_dm(
            ctx.guild,
            f"**Success (remove)** — One custom handle removed.\n"
            f"• **User affected:** {ctx.author.display_name} (username: `{ctx.author.name}`, id: `{ctx.author.id}`)\n"
            f"• **Changes applied:** Removed tracked handle **{name}** (only roles added via set can be removed)."
        )

    @userhandle.group(name="blacklist", invoke_without_command=True)
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_blacklist(self, ctx: commands.Context) -> None:
        """[Admin] List role names that this bot must never create or add to tracked handles (e.g. restriction/special roles)."""
        if ctx.invoked_subcommand is not None:
            return
        names = await self.config.guild(ctx.guild).role_blacklist()
        names = [n for n in (names or []) if (n or "").strip()]
        if not names:
            await ctx.send("Role blacklist is empty. Use `!userhandle blacklist add <name>` to add reserved role names.")
            return
        await ctx.send(f"**Reserved role names** (bot will not create or track these): {', '.join(f'**{n}**' for n in names)}.")

    @userhandle_blacklist.command(name="add")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_blacklist_add(self, ctx: commands.Context, *, name: str) -> None:
        """[Admin] Add a role name to the blacklist. The bot will never create or track a handle with this name."""
        name = (name or "").strip()
        if not name:
            await ctx.send("Please provide a role name to blacklist.")
            return
        bl = list(await self.config.guild(ctx.guild).role_blacklist() or [])
        if name.lower() in [b.strip().lower() for b in bl if b]:
            await ctx.send(f"**{name}** is already on the blacklist.")
            return
        bl.append(name)
        await self.config.guild(ctx.guild).role_blacklist.set(bl)
        await ctx.send(f"**{name}** is now reserved. The bot will not create or track custom handles with this name.")

    @userhandle_blacklist.command(name="remove")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_blacklist_remove(self, ctx: commands.Context, *, name: str) -> None:
        """[Admin] Remove a role name from the blacklist."""
        name = (name or "").strip()
        if not name:
            await ctx.send("Please provide a role name to remove from the blacklist.")
            return
        bl = list(await self.config.guild(ctx.guild).role_blacklist() or [])
        new_bl = [b for b in bl if (b or "").strip().lower() != name.lower()]
        if len(new_bl) == len(bl):
            await ctx.send(f"**{name}** was not on the blacklist.")
            return
        await self.config.guild(ctx.guild).role_blacklist.set(new_bl)
        await ctx.send(f"**{name}** removed from the blacklist.")

    @userhandle.command(name="logdm")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_logdm(self, ctx: commands.Context) -> None:
        """[Admin] Toggle DM logging. When on, you get a DM for set/clear/remove/sync/chron. Clears channel logging if set."""
        current = await self.config.guild(ctx.guild).log_dm_user_id()
        if current == ctx.author.id:
            await self.config.guild(ctx.guild).log_dm_user_id.set(None)
            await ctx.send("DM logging is now **off** for this server. You will not receive DMs for UserHandle actions.")
        else:
            await self.config.guild(ctx.guild).log_channel_id.set(None)  # switch from channel to DM
            await self.config.guild(ctx.guild).log_dm_user_id.set(ctx.author.id)
            await ctx.send(
                "DM logging is now **on** for this server. You'll receive a DM for:\n"
                "• **set** – who set a custom handle and the role names\n"
                "• **clear** / **remove** – who cleared or removed handles\n"
                "(Auto-generated display-name sync is not logged to avoid noise.)"
            )

    @userhandle.command(name="logchannel")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_logchannel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """[Admin] Send UserHandle logs to a channel instead of DMs. Pass a channel or leave empty to turn off channel logging."""
        if channel is None:
            current = await self.config.guild(ctx.guild).log_channel_id()
            await self.config.guild(ctx.guild).log_channel_id.set(None)
            if current:
                await ctx.send("Channel logging is now **off**. Use `!userhandle logdm` to get logs in DMs, or set a channel again with `!userhandle logchannel #channel`.")
            else:
                await ctx.send(
                    "No log channel is set. To send logs to a channel, run: `!userhandle logchannel #channel`. "
                    "To get logs in DMs instead, run: `!userhandle logdm`."
                )
            return
        await self.config.guild(ctx.guild).log_dm_user_id.set(None)  # switch from DM to channel
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(
            f"UserHandle logs will now be sent to {channel.mention}. You'll see set/clear/remove there (auto display-name sync is not logged). "
            "Use `!userhandle logchannel` with no channel to turn this off, or `!userhandle logdm` to switch to DMs."
        )

    @userhandle.command(name="sync")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_sync(self, ctx: commands.Context) -> None:
        """[Admin] Ensure every member has a tag role and names are in sync. Run this after enabling the cog on a server with existing members."""
        self._last_sync_error = None
        await ctx.send(f"Syncing tag roles for all members… This may take a while. (cog v{__version__})")
        # Try cache first (chunk with timeout so we don't hang on slow gateway)
        try:
            await asyncio.wait_for(ctx.guild.chunk(), timeout=15.0)
        except (discord.HTTPException, asyncio.TimeoutError):
            pass
        members_list = [m for m in ctx.guild.members if not m.bot]
        rest_used = False
        if not members_list:
            await ctx.send(f"Cache empty or chunk timed out; fetching members via API… (cog v{__version__})")
            rest_members = await _fetch_guild_members_via_rest(self.bot, ctx.guild)
            if rest_members:
                members_list = rest_members
                rest_used = True
        if not members_list:
            total = ctx.guild.member_count or 0
            await ctx.send(
                f"Could not get the member list (cache and REST both failed or returned 0). "
                f"Server reports {total} total members. (cog v{__version__}) "
                "Ensure **Server Members Intent** is enabled in the Developer Portal (Bot → Privileged Gateway Intents), then **restart Red** fully. "
                "If the problem persists, push the latest cog code to GitHub and run `!repo update dc-red-role-bot` then `!cog update user_handle`."
            )
            return
        # Only create/update sync (display-name) roles. Custom handles are never touched.
        created = 0
        for member in members_list:
            try:
                role = await self._ensure_sync_role(ctx.guild, member)
                if role is not None:
                    created += 1
            except Exception as e:
                log.exception("UserHandle: sync failed for member %s: %s", member.id, e)
                if self._last_sync_error is None:
                    self._last_sync_error = str(e)
            await asyncio.sleep(0.3)
        msg = f"Sync complete. Display-name roles ensured for {created} non-bot members (custom handles left unchanged). (cog v{__version__})"
        if rest_used:
            msg += " (used API fallback)"
        sync_error = self._last_sync_error
        if members_list and created == 0:
            msg += " — No roles were created: check that the bot has **Manage Roles** and its role is **above** the roles it creates in Server settings → Roles."
            if sync_error:
                msg += f" Discord error: `{sync_error}`"
                self._last_sync_error = None
        await ctx.send(msg)
        # No logging for manual sync (auto-generated display-name roles) to avoid noise

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Give new members their display-name sync role only."""
        if member.bot:
            return
        await self._ensure_sync_role(member.guild, member)
