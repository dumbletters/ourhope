ourhope V2 — Discord-only Railway build

Deploy this folder's contents to the EXISTING GitHub repository connected to Railway.
Railway start command: python ourhope.py
Required Railway variable: DISCORD_BOT_TOKEN (newly reset token)

Commands:
/giveaway, /reroll
/ticketpanel, /tickettranscript
/timeout, /kick, /ban, /purge, /warn, /warnings
/suggest, /serverinfo, /automod, /help
?bl or ?blacklist; ?unbl or ?unblacklist

IMPORTANT:
- Enable Server Members Intent and Message Content Intent in Discord Developer Portal > Bot.
- Give the bot required permissions and put its role above roles it must manage.
- Use Railway persistent storage mounted at /data and set DB_PATH=/data/ourhope.sqlite3 if available; otherwise SQLite data can be lost on redeploy.
- This package retains the prior ticket/blacklist implementation; it is not a literal Bleed clone.
- Syntax checked locally, but live Discord/Railway behavior still needs testing.
