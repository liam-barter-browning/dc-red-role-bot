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

__version__ = "1.8"
log = logging.getLogger("red.cog.user_handle")


def _normalize_info(info: dict) -> dict:
    """Normalize stored info to sync_role_id, custom_role_id, custom_name. Handles old format (role_id + custom_name)."""
    out = {
        "sync_role_id": info.get("sync_role_id"),
        "custom_role_id": info.get("custom_role_id"),
        "custom_name": info.get("custom_name"),
    }
    legacy_role_id = info.get("role_id")
    if legacy_role_id is not None and out["sync_role_id"] is None and out["custom_role_id"] is None:
        if out["custom_name"]:
            out["custom_role_id"] = legacy_role_id
            out["sync_role_id"] = None
        else:
            out["sync_role_id"] = legacy_role_id
            out["custom_role_id"] = None
    return out


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
        self.config.register_guild(role_assignments={})  # user_id -> {"role_id": int, "custom_name": str | None}
        self._sync_lock = asyncio.Lock()
        self._last_sync_error: Optional[str] = None  # for reporting when sync creates 0 roles

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
                except Exception as e:
                    log.exception("UserHandle sync failed for guild %s: %s", guild.id, e)
                await asyncio.sleep(0.5)  # avoid hammering the API

    @sync_role_names.before_loop
    async def before_sync_role_names(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_guild_roles(self, guild: discord.Guild) -> None:
        """Background: only update sync (display-name) roles. Never touch custom roles or roles we didn't create."""
        data = await self.config.guild(guild).role_assignments()
        if not data:
            return
        try:
            await guild.chunk()
        except discord.HTTPException:
            pass
        existing_names = {r.name for r in guild.roles}
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
                        if a.get("custom_role_id") or a.get("custom_name"):
                            assignments[user_id_str] = {k: a.get(k) for k in ("sync_role_id", "custom_role_id", "custom_name")}
                        else:
                            assignments.pop(user_id_str, None)
                    continue
                member = guild.get_member(int(user_id_str))
                if not member:
                    continue
                desired_name = (_display_name(member) or member.name).strip() or member.name
                if role.name == desired_name:
                    if not member.get_role(sync_role_id):
                        try:
                            await member.add_roles(role, reason="UserHandle: re-add sync role")
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                    continue
                unique_name = self._unique_role_name(guild, desired_name, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: sync display name")
                    existing_names.discard(role.name)
                    existing_names.add(unique_name)
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
                assignments[user_id_str] = {k: entry.get(k) for k in ("sync_role_id", "custom_role_id", "custom_name")}
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
        """Create or update only the custom handle role. Does not touch the sync role."""
        custom_name = (custom_name or "").strip() or member.name
        user_id_str = str(member.id)
        async with self.config.guild(guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            custom_role_id = info.get("custom_role_id")
        existing_names = {r.name for r in guild.roles}
        role = guild.get_role(custom_role_id) if custom_role_id else None
        if role is None:
            unique_name = self._unique_role_name(guild, custom_name, existing_names)
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
            existing_names.add(role.name)
        else:
            if role.name != custom_name:
                unique_name = self._unique_role_name(guild, custom_name, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: update custom handle")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        async with self.config.guild(guild).role_assignments() as assignments:
            entry = _normalize_info(assignments.get(user_id_str) or {})
            entry["custom_role_id"] = role.id
            entry["custom_name"] = custom_name
            assignments[user_id_str] = {k: entry.get(k) for k in ("sync_role_id", "custom_role_id", "custom_name")}
        try:
            if not member.get_role(role.id):
                await member.add_roles(role, reason="UserHandle: assign custom handle")
        except (discord.Forbidden, discord.HTTPException) as e:
            if self._last_sync_error is None:
                self._last_sync_error = str(e)
        return role

    @commands.group(name="userhandle", invoke_without_command=True)
    async def userhandle(self, ctx: commands.Context) -> None:
        """User handles: a role per member matching their display name (or custom handle) for easier tagging."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

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
        custom_role = await self._ensure_custom_role(ctx.guild, ctx.author, name)
        if custom_role is None:
            await ctx.send("I couldn't create or update your custom handle. Check permissions.")
            return
        await ctx.send(f"You now have **{sync_role.name}** (from your display name) and **{custom_role.name}** (custom handle).")

    @userhandle.command(name="clear")
    async def userhandle_clear(self, ctx: commands.Context) -> None:
        """Remove your custom handle. Your display-name role is kept and will stay in sync."""
        user_id_str = str(ctx.author.id)
        async with self.config.guild(ctx.guild).role_assignments() as assignments:
            info = _normalize_info(assignments.get(user_id_str) or {})
            custom_role_id = info.get("custom_role_id")
            if not custom_role_id and not info.get("custom_name"):
                await ctx.send("You don't have a custom handle set in this server.")
                return
            role = ctx.guild.get_role(custom_role_id) if custom_role_id else None
            if role and ctx.author.get_role(custom_role_id):
                try:
                    await ctx.author.remove_roles(role, reason="UserHandle: clear custom handle")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            entry = _normalize_info(assignments.get(user_id_str) or {})
            entry["custom_role_id"] = None
            entry["custom_name"] = None
            if entry.get("sync_role_id") is None:
                assignments.pop(user_id_str, None)
            else:
                assignments[user_id_str] = {k: v for k, v in entry.items() if k in ("sync_role_id", "custom_role_id", "custom_name")}
        await ctx.send("Custom handle removed. Your display-name role is unchanged and will keep syncing.")

    @userhandle.command(name="sync")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_sync(self, ctx: commands.Context) -> None:
        """[Admin] Ensure every member has a tag role and names are in sync. Run this after enabling the cog on a server with existing members."""
        self._last_sync_error = None
        await ctx.send(f"Syncing tag roles for all members… This may take a while. (cog v{__version__})")
        # Try cache first (chunk so gateway sends member list)
        try:
            await ctx.guild.chunk()
        except discord.HTTPException:
            pass
        members_list = [m for m in ctx.guild.members if not m.bot]
        rest_used = False
        # If cache is empty (e.g. Red with strict member cache), fetch via REST
        if not members_list:
            await ctx.send(f"Cache empty; fetching members via API… (cog v{__version__})")
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
        # Don't use _sync_lock here so we don't block waiting on the background task (which can hold it for a while).
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
        if members_list and created == 0:
            msg += " — No roles were created: check that the bot has **Manage Roles** and its role is **above** the roles it creates in Server settings → Roles."
            if self._last_sync_error:
                msg += f" Discord error: `{self._last_sync_error}`"
                self._last_sync_error = None
        await ctx.send(msg)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Give new members their display-name sync role only."""
        if member.bot:
            return
        await self._ensure_sync_role(member.guild, member)
