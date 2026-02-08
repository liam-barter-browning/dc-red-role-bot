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

log = logging.getLogger("red.cog.user_handle")


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
        data = await self.config.guild(guild).role_assignments()
        if not data:
            return
        # Ensure member cache is populated so get_member() can find members
        try:
            await guild.chunk()
        except discord.HTTPException:
            pass  # Skip this guild this cycle
        existing_names = {r.name for r in guild.roles}
        for user_id_str, info in list(data.items()):
            try:
                role_id = info.get("role_id")
                custom_name = info.get("custom_name")
                if not role_id:
                    continue
                role = guild.get_role(role_id)
                if not role:
                    # Role was deleted; remove from config and skip
                    async with self.config.guild(guild).role_assignments() as assignments:
                        assignments.pop(user_id_str, None)
                    continue
                member = guild.get_member(int(user_id_str))
                if not member:
                    # Member left; leave role/config as-is (they may rejoin)
                    continue
                if custom_name:
                    desired_name = custom_name
                else:
                    desired_name = _display_name(member)
                desired_name = desired_name.strip() or member.name
                if role.name == desired_name:
                    continue
                # Ensure name is unique in guild
                unique_name = self._unique_role_name(guild, desired_name, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: sync display name")
                    existing_names.discard(role.name)
                    existing_names.add(unique_name)
                except discord.Forbidden:
                    log.warning("UserHandle: no permission to edit role %s in guild %s", role.id, guild.id)
                except discord.HTTPException as e:
                    log.warning("UserHandle: failed to edit role %s: %s", role.id, e)
                if not member.get_role(role_id):
                    try:
                        await member.add_roles(role, reason="UserHandle: re-add role after sync")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        log.warning("UserHandle: could not add role to %s: %s", member.id, e)
            except (ValueError, KeyError) as e:
                log.debug("UserHandle: skip user %s in guild %s: %s", user_id_str, guild.id, e)
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

    async def _ensure_member_role(
        self,
        guild: discord.Guild,
        member: discord.Member,
        custom_name: Optional[str] = None,
    ) -> Optional[discord.Role]:
        """Create or get the member's tag role, assign it, and return the role. Returns None on failure."""
        async with self.config.guild(guild).role_assignments() as assignments:
            user_id_str = str(member.id)
            info = assignments.get(user_id_str) or {}
            role_id = info.get("role_id")
            stored_custom = info.get("custom_name")
            # If we're being called with a new custom name, use it; else use stored
            name_to_use = custom_name if custom_name is not None else stored_custom
            if name_to_use is None:
                name_to_use = _display_name(member)
            name_to_use = (name_to_use or member.name).strip() or member.name

        existing_names = {r.name for r in guild.roles}
        role = guild.get_role(role_id) if role_id else None

        if role is None:
            unique_name = self._unique_role_name(guild, name_to_use, existing_names)
            try:
                role = await guild.create_role(
                    name=unique_name,
                    reason="UserHandle: create role for member",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("UserHandle: could not create role in guild %s: %s", guild.id, e)
                return None
            existing_names.add(role.name)
            async with self.config.guild(guild).role_assignments() as assignments:
                assignments[user_id_str] = {"role_id": role.id, "custom_name": custom_name if custom_name is not None else stored_custom}
        else:
            if role.name != name_to_use:
                unique_name = self._unique_role_name(guild, name_to_use, existing_names, exclude_role=role)
                try:
                    await role.edit(name=unique_name, reason="UserHandle: update role name")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("UserHandle: could not edit role %s: %s", role.id, e)

        try:
            if not member.get_role(role.id):
                await member.add_roles(role, reason="UserHandle: assign tag role")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("UserHandle: could not add role to member %s: %s", member.id, e)
            return role
        return role

    @commands.group(name="userhandle", invoke_without_command=True)
    async def userhandle(self, ctx: commands.Context) -> None:
        """User handles: a role per member matching their display name (or custom handle) for easier tagging."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @userhandle.command(name="set")
    async def userhandle_set(self, ctx: commands.Context, *, name: str) -> None:
        """Set a custom role name (handle) for yourself in this server.
        Use this if your name uses a different alphabet and you want an English (or other) handle for tagging.
        """
        name = name.strip()
        if not name:
            await ctx.send("Please provide a non-empty name.")
            return
        if len(name) > 100:
            await ctx.send("Name must be 100 characters or fewer.")
            return
        role = await self._ensure_member_role(ctx.guild, ctx.author, custom_name=name)
        if role is None:
            await ctx.send("I couldn't create or update your role. Check that my role is above the role I create and I have *Manage Roles*.")
            return
        async with self.config.guild(ctx.guild).role_assignments() as assignments:
            assignments[str(ctx.author.id)] = {"role_id": role.id, "custom_name": name}
        await ctx.send(f"Your tag role is now **{role.name}**.")

    @userhandle.command(name="clear")
    async def userhandle_clear(self, ctx: commands.Context) -> None:
        """Clear your custom role name and sync the role to your current display name."""
        async with self.config.guild(ctx.guild).role_assignments() as assignments:
            user_id_str = str(ctx.author.id)
            if user_id_str not in assignments:
                await ctx.send("You don't have a tag role set in this server.")
                return
            info = assignments[user_id_str]
            info["custom_name"] = None
        role = await self._ensure_member_role(ctx.guild, ctx.author, custom_name=None)
        if role is None:
            await ctx.send("I couldn't update your role. Check permissions.")
            return
        await ctx.send(f"Custom name cleared. Your tag role is now **{role.name}** (synced to your display name).")

    @userhandle.command(name="sync")
    @commands.admin_or_permissions(manage_roles=True)
    async def userhandle_sync(self, ctx: commands.Context) -> None:
        """[Admin] Ensure every member has a tag role and names are in sync. Run this after enabling the cog on a server with existing members."""
        await ctx.send("Syncing tag roles for all members… This may take a while.")
        # Try cache first (chunk so gateway sends member list)
        try:
            await ctx.guild.chunk()
        except discord.HTTPException:
            pass
        members_list = [m for m in ctx.guild.members if not m.bot]
        # If cache is empty (e.g. Red with strict member cache), fetch via REST
        if not members_list:
            rest_members = await _fetch_guild_members_via_rest(self.bot, ctx.guild)
            if rest_members:
                members_list = rest_members
        if not members_list:
            total = ctx.guild.member_count or 0
            await ctx.send(
                f"Could not get the member list (cache and REST both failed or returned 0). "
                f"Server reports {total} total members. "
                "Ensure **Server Members Intent** is enabled in the Developer Portal (Bot → Privileged Gateway Intents), then **restart Red** fully. "
                "If the problem persists, push the latest cog code to GitHub and run `!repo update dc-red-role-bot` then `!cog update user_handle`."
            )
            return
        async with self._sync_lock:
            created = 0
            for member in members_list:
                role = await self._ensure_member_role(ctx.guild, member, custom_name=None)
                if role is not None:
                    created += 1
                await asyncio.sleep(0.3)
        await ctx.send(f"Sync complete. Tag roles ensured for {created} non-bot members.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Give new members their tag role (display name or existing custom name)."""
        if member.bot:
            return
        await self._ensure_member_role(member.guild, member, custom_name=None)
