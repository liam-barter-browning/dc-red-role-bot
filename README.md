# dc-red-role-bot

Discord Bot cog (compatible with **Red - DiscordBot**) that gives each member a role matching their display name (or a custom handle). Cog name: **user_handle** for easier tagging across language/character sets, and keeps those role names in sync when users change their server nickname.

## Features

- **Per-user role**: Each member gets a role whose name matches their **server display name** (nickname if set, otherwise username).
- **Custom handles**: Members can set a custom role name (e.g. an English handle) so they can be tagged easily even if their username uses another alphabet.
- **Background sync**: A task runs every 5 minutes and updates bot-managed role names to match current display names (and re-applies roles if needed). When a user changes their server nickname, their role name is updated automatically.

## Setup

1. Install [Red - DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).
2. Add the path that contains the `user_handle` folder (e.g. the repo root):
   ```
   [p]addpath /path/to/dc-red-role-bot
   [p]load user_handle
   ```
3. Ensure the bot’s role in each server is **above** the roles it creates, and that it has **Manage Roles**.

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
