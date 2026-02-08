# dc-red-role-bot

Discord Bot cog (compatible with **Red - DiscordBot**). Cog name: **user_handle**. It gives each member a role matching their display name (or a custom handle) and keeps those role names in sync when users change their server nickname.

## Purpose

This cog enables **tagging across language and character sets**, and supports **parallel language / translation channels** where each member is only in the channel for their native language.

In that setup you can’t @mention someone by name if they’re not in your channel. By giving everyone a role that matches their name (or a chosen handle), you can tag them via that role from any channel. Members who use a different alphabet can set a custom English (or other) handle so they can still be tagged easily.

## Features

- **Per-user role**: Each member gets a role whose name matches their **server display name** (nickname if set, otherwise username).
- **Custom handles**: Members can set a custom role name (e.g. an English handle) so they can be tagged easily even if their username uses another alphabet.
- **Background sync**: A task runs every 5 minutes and updates bot-managed role names to match current display names (and re-applies roles if needed). When a user changes their server nickname, their role name is updated automatically.

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
| `[p]userhandle` | Show help. |
| `[p]userhandle set <name>` | Set your custom role name (handle) in this server. |
| `[p]userhandle clear` | Clear your custom name; your role will sync to your current display name. |
| `[p]userhandle sync` | **(Admin)** Ensure every member has a tag role and names are in sync. Run once after enabling the cog if you want existing members to get roles. |

## Behaviour

- **New members**: On join, a role is created with their display name and assigned to them.
- **Display name**: In each server, “display name” means the member’s nickname if set, otherwise their global username.
- **Uniqueness**: If two members would have the same role name, the second gets a suffix like ` (2)` so role names stay unique in the guild.
- **Cron**: Every 5 minutes the cog checks all stored user→role mappings; if a member has no custom handle and their role name differs from their current display name, the role name is updated.

## License

See [LICENSE](LICENSE).
