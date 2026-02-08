# dc-red-role-bot

Discord Bot cog (compatible with **Red - DiscordBot**). Cog name: **user_handle**. It gives each member a role matching their display name (or a custom handle) and keeps those role names in sync when users change their server nickname.

## Purpose

This cog enables **tagging across language and character sets**, and supports **parallel language / translation channels** where each member is only in the channel for their native language.

In that setup you can’t @mention someone by name if they’re not in your channel. By giving everyone a role that matches their name (or a chosen handle), you can tag them via that role from any channel. Members who use a different alphabet can set a custom English (or other) handle so they can still be tagged easily.

## Features

- **Per-user role**: Each member gets a role whose name matches their **server display name** (nickname if set, otherwise username).
- **Custom handles**: Members can add one or more custom handle roles via `set`; each is tracked so they can remove only those with `remove`. Only roles created by the bot are tracked (existing server roles are never adopted).
- **Blacklist**: Admins can reserve role names so the bot never creates or tracks handles with those names (e.g. for restriction or other-bot roles).
- **Background sync**: A task runs every 5 minutes and updates display-name role names to match current nicknames (and re-applies roles if needed). Custom handles are left unchanged.

## Setup

1. Install [Red - DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).
2. Load the cog **from GitHub** (recommended) or [from a local path](#loading-from-a-local-path).
3. Ensure the bot’s role in each server is **above** the roles it creates, and that it has **Manage Roles**.

---

## Loading from GitHub (recommended)

Use Red’s **Downloader** cog to install **user_handle** from this repo. No local clone required.

**Prerequisite:** This repository must be on GitHub (your fork or the original). Replace `YOUR_USERNAME` below with the GitHub username that owns the repo.

### 1. Load Downloader

In Discord (with your bot’s prefix, e.g. `!`):

```
[p]load downloader
```

(Skip if Downloader is already loaded.)

### 2. Add this repo

```
[p]repo add dc-red-role-bot https://github.com/YOUR_USERNAME/dc-red-role-bot
```

Use the real repo URL. Examples:

- Your fork: `!repo add dc-red-role-bot https://github.com/liam/dc-red-role-bot`
- Default branch is used; you can add a branch name as a third argument if needed.

### 3. Install the cog

```
[p]cog install dc-red-role-bot user_handle
```

### 4. Load the cog

After install, load the cog (Red may not load it automatically):

```
[p]load user_handle
```

Then confirm:

```
[p]userhandle
```

(You should see the userhandle command help.)

### 5. Bot role and permissions (per server)

In **Server settings → Roles**:

- The **bot’s role** must be **above** any role the cog creates.
- The bot needs **Manage Roles**.

### 6. Give existing members roles (optional)

To create handle roles for **all current members** in a server, an admin runs once:

```
[p]userhandle sync
```

New members get a role automatically when they join.

**If sync says "0 non-bot members":** (1) Enable **SERVER MEMBERS INTENT** in the [Discord Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → **Privileged Gateway Intents**, then restart Red. (2) The cog now requests the full member list (chunking) when you run sync; update to the latest version (`[p]cog update user_handle`), then run `[p]userhandle sync` again.

### Updating the cog later

After you update the repo on GitHub:

```
[p]repo update dc-red-role-bot
[p]cog update user_handle
```

---

## Loading from a local path

If you prefer to run from a clone on the same machine as the bot:

1. **Path:** Use the folder that **contains** the `user_handle` folder (repo root), e.g. `/home/you/dc-red-role-bot` or `C:\Users\You\dc-red-role-bot`.
2. **Add path:** `[p]addpath /full/path/to/dc-red-role-bot`
3. **Load cog:** `[p]load user_handle`
4. Then set bot role and permissions as in step 4 above, and optionally run `[p]userhandle sync` for existing members.

## Commands

| Command | Description |
|--------|-------------|
| `[p]userhandle` | Show command list. |
| `[p]userhandle help` | Show a usage guide. If you have admin or Manage Roles, the guide includes admin-only commands. |
| `[p]userhandle set <name>` | Add a custom handle (role) for yourself. Only adds; does not remove other handles. You can have multiple. |
| `[p]userhandle remove <name>` | Remove one custom handle. Only removes roles that were added by this bot via `set` (tracked handles). |
| `[p]userhandle clear` | Remove **all** your custom handles (tracked by this bot). Your display-name role is kept and keeps syncing. |
| `[p]userhandle sync` | **(Admin)** Ensure every member has a display-name role and names are in sync. Run once after enabling the cog for existing members. |
| `[p]userhandle logdm` | **(Admin)** Toggle DM logging. When on, you get a DM for set, clear, remove, sync, and background sync. |
| `[p]userhandle logchannel [#channel]` | **(Admin)** Send logs to a channel instead of DMs. Run with no channel to turn channel logging off. |
| `[p]userhandle blacklist` | **(Admin)** List role names that the bot must never create or track (e.g. restriction/special roles). |
| `[p]userhandle blacklist add <name>` | **(Admin)** Add a role name to the blacklist. The bot will not create or track handles with this name. |
| `[p]userhandle blacklist remove <name>` | **(Admin)** Remove a role name from the blacklist. |

## Behaviour

- **New members**: On join, a role is created with their display name and assigned to them.
- **Display name**: In each server, “display name” means the member’s nickname if set, otherwise their global username.
- **Uniqueness**: If two members would have the same role name, the second gets a suffix like ` (2)` so role names stay unique in the guild.
- **Tracked handles**: Only roles **created** by the bot via `set` are tracked. The `remove` command can only remove those. Existing server roles (e.g. permission roles) are never added to the tracked list.
- **Blacklist**: Admins can blacklist role names. The bot will not create or track a handle with a blacklisted name, so you can reserve names used for restrictions or other bots.
- **Cron**: Every 5 minutes the cog updates display-name (sync) role names to match current nicknames and re-applies roles if needed. Custom handles are not renamed by the background task.

## License

See [LICENSE](LICENSE).
