# Rhino Bot

Python Discord bot for moderation, verification, modmail, private support tickets, staff applications, anti-raid protection, server activity logs, auto-reactions, reaction roles, no-link channels, AFK statuses, configurable prefixes, and QOTD posting.

## Features

- Slash commands: `help`, `warn`, `mute`, `unmute`, `kick`, `ban`, `unban`, `addrole`, `removerole`, `role add`, `role remove`, `clear`, `modlogs`, `afk`, `prefix ...`, `verificationpanel`, `serverinfo post/edit`, `staffapplypanel` (`post` and `disable`), `ticket ...`, `qotd`, `embed`, `autoreact ...`, `reactionrole ...`, `nolink ...`, and `antiraid ...`
- Prefix commands: `help`, `afk`, and `prefix` with per-server `set`, `show`, and `reset`
- DM-based modmail with an `Open Modmail` button
- Persistent Northeast Esports verification panel that assigns the `Verified` role
- Modern server-info hub preset with a banner, rules, links, verification CTA, level-role overview, and a modal editor for live posts; link buttons follow the edited Links section
- `/serverinfo edit` shows a dedicated Button URLs box for the Links section. Use `Invite | https://...`, `Instagram | https://...`, and `YouTube | https://...` so the buttons and visible fields match.
- Forum-thread modmail relay between moderators and users
- Persistent ticket panel with private per-member channels, staff claiming, participant management, and duplicate-ticket prevention
- HTML ticket transcripts available on demand and automatically sent to the log channel and ticket opener when a ticket closes
- Administrator-selected transcript channel through `/ticket setlog`, persisted across bot restarts
- Moderation log history for `/modlogs`, stored in PostgreSQL when `DATABASE_URL` is configured
- Server activity logs for message deletes and edits, bulk deletes, invites, moderator commands, member updates, role changes, channel changes, emoji changes, voice joins, leaves and moves, and ban or unban events
- Staff application panel with a 2-page modal workflow
- QOTD posting that pings the QOTD role and opens a public reply thread automatically
- Reaction role panels and existing-message bindings that add roles on reaction add and remove them on reaction remove
- AFK statuses with mention replies and automatic clearing when the member sends a message
- No-link channel protection with per-channel activate and deactivate commands
- Anti-raid detection for join bursts with temporary raid mode and auto-timeout for suspicious fresh accounts
- PostgreSQL-backed persistence for modlogs, auto-reactions, reaction roles, no-link channels, AFK statuses, command prefixes, and ticket settings when `DATABASE_URL` is configured

## Project Structure

```text
Rhino-Bot/
|-- bot.py
|-- config.py
|-- requirements.txt
|-- .env
|-- .env.example
|-- PRIVACY_POLICY.md
|-- TERMS_OF_SERVICE.md
`-- README.md
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Fill in `.env`.
4. Start the bot:

```powershell
python bot.py
```

## Discord Developer Portal

Enable these intents for the bot:

- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

## Notes

- `MODMAIL_FORUM_ID` must point to a forum channel.
- Anti-raid settings can be adjusted through `.env` without editing code.
- Use `/antiraid status` to check whether raid mode is active.
- Set `SERVER_LOG_CHANNEL_ID` if you want server activity logs in a dedicated text channel. If it is not set, the bot falls back to `MOD_LOG_CHANNEL_ID`.
- Set `SERVER_STATS_CHANNEL_ID` if you want the bot to rename a dedicated channel with live stats. Use `SERVER_STATS_CHANNEL_FORMAT` to control the name template, with placeholders `{guild}`, `{online}`, `{total}`, and `{boosters}`.
- Set `ALL_MEMBERS_STATS_CHANNEL_ID`, `MEMBERS_STATS_CHANNEL_ID`, `BOTS_STATS_CHANNEL_ID`, `BOOSTS_STATS_CHANNEL_ID`, and `ONLINE_MEMBERS_STATS_CHANNEL_ID` if you want the bot to rename separate stat channels like `all-members-823`, `members-800`, `bots-23`, `boosts-4`, and `online-members-107`.
- Set `INVITE_LOG_CHANNEL_ID` if you want invite create and delete events in a dedicated text channel. If it is not set, invite logs fall back to `SERVER_LOG_CHANNEL_ID`, then `MOD_LOG_CHANNEL_ID`.
- Set `VERIFICATION_LOG_CHANNEL_ID` if you want successful verification logs in a dedicated text channel. If it is not set, verification logs fall back to `SERVER_LOG_CHANNEL_ID`, then `MOD_LOG_CHANNEL_ID`.
- Set `WELCOME_CHANNEL_ID` if you want automatic welcome messages for new members in a dedicated text channel.
- Set `TICKET_CATEGORY_ID` to place private ticket channels in a specific category. When omitted, the bot uses or creates a category named `Tickets`.
- Administrators can run `/ticket setlog channel:#your-private-log` to choose where closed-ticket transcripts are saved. This server setting overrides `TICKET_TRANSCRIPT_CHANNEL_ID` and persists in PostgreSQL or `ticket_config.json`.
- `TICKET_TRANSCRIPT_CHANNEL_ID` is the fallback transcript channel. When omitted, transcripts fall back to `MOD_LOG_CHANNEL_ID`.
- Set `VERIFIED_ROLE_ID` if you want the verification button to target a specific role ID. If it is not set, the bot falls back to a role named `Verified`.
- Set `DATABASE_URL` if you want persistent PostgreSQL storage for moderation logs, auto-reaction rules, reaction roles, no-link channels, AFK statuses, command prefixes, and ticket settings.
- Without `DATABASE_URL`, auto-reaction rules are stored in `autoreact_data.json`, reaction roles are stored in `reaction_roles.json`, no-link channel rules are stored in `no_link_channels.json`, AFK statuses are stored in `afk_data.json`, command prefixes are stored in `prefix_data.json`, ticket settings are stored in `ticket_config.json`, and moderation logs stay in memory until restart.
- With `DATABASE_URL`, the bot seeds PostgreSQL from those local JSON files when the database tables are empty.

## Legal

- Terms of Service: `TERMS_OF_SERVICE.md`
- Privacy Policy: `PRIVACY_POLICY.md`
