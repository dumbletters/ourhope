# ourhope — Bleed-inspired community bot + dashboard

A new deployment package based on discord.py, FastAPI, Discord OAuth2 and SQLite. It is inspired by the general category of all-in-one Discord bots; it does not copy Bleed's private code, branding, or paid-only assets.

## Included

- Moderation slash commands: `/ban`, `/kick`, `/timeout`, `/purge`, `/warn`, `/warnings`, `/serverinfo`
- Security defaults: link detection and one-hour timeout, six repeated identical messages within 30 seconds trigger a one-hour timeout, blacklist role/channel privacy enforcement
- Blacklist legacy commands: `?bl @member ?r reason`, `?blacklist ID`, `?unbl ID`, `?unblacklist ID`; restorable role backup where Discord hierarchy permits
- Giveaways: `/giveaway`, `/reroll`, persistent entries and winner timer
- Tickets: `/ticketpanel`, `/tickettranscript`, persistent open/claim/close buttons; no auto-created ticket category; uses panel category unless a category ID is set in dashboard
- Suggestions: `/suggest`; creates a read-only suggestions channel if missing
- OAuth2 dashboard: per-server security toggles, log channel field, support role and ticket category IDs, prefix field
- SQLite persistence for tickets, giveaways, warnings, blacklist role backups, and dashboard settings

## Local setup

1. Install Python 3.11+.
2. `python -m venv .venv` then activate it.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set all values in your shell/host environment. Do not commit secrets.
5. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal. Invite with bot + applications.commands scopes and required permissions.
6. `python main.py` starts the bot and dashboard on `PORT` (default 8080).

## Discord OAuth dashboard setup

In the Discord Developer Portal, add an OAuth2 redirect URI that exactly matches `DISCORD_REDIRECT_URI` (e.g. `https://your-domain.example/callback`). Set `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, and a long random `DASHBOARD_SECRET`. Use HTTPS in production. Dashboard login requires the user to have Manage Server or Administrator for a guild. Dashboard controls currently save anti-link/anti-spam toggles, log-channel ID, support-role ID, ticket-category ID and a legacy prefix value. The log-channel and prefix values are stored as preferences; they are not yet used by every command.

## Railway

- Push the *contents* of this folder into a private GitHub repository.
- Connect it to Railway.
- Set `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `DASHBOARD_SECRET`, and `DB_PATH=/data/ourhope.sqlite3` in Railway Variables.
- Add a Railway Volume mounted at `/data`.
- Keep one replica only; this app runs the Discord gateway and web server together.
- Add a custom domain (or use your Railway public domain) and set the exact callback URL in both Railway and Discord Developer Portal.
- Deploy and check logs. Visit the dashboard root URL and test slash commands in a private test server first.

## Important limitations and safety

- Discord permissions and role hierarchy always apply. The bot cannot manage roles above its own top role or override administrator permissions.
- This is a validated starter release, not a claim of full feature parity with Bleed. Music streaming, anti-nuke bulk-action detection, configurable automod rule builder, premium systems, and full transcripts-to-storage are not included yet.
- The dashboard stores OAuth access tokens in a signed session cookie. Keep `DASHBOARD_SECRET` private, serve only over HTTPS, and rotate it if exposed. For a public multi-admin production deployment, use a dedicated session store and refresh-token lifecycle.
- Never commit `.env`, database files, or the bot token. If a token leaks, reset it immediately in the Discord Developer Portal.
