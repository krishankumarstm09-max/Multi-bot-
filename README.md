# KrishManager Pro Max

A PostgreSQL-backed multi-bot Telegram manager for Railway.

## Included
- Multi-bot connect/manage panel
- Persistent encrypted bot-token storage (Fernet)
- BIGINT Telegram IDs throughout
- Per-bot Start message + media + inline URL/callback buttons
- Force Join with private-channel Join Request detection; the manager never auto-approves/declines requests
- Auto-DM rules for Join Request and New Member events, with delay/media/variables
- Broadcast queue with delivery logs
- Support Inbox with reply routing and media copy where Telegram permits
- Users: search, profile, ban/unban
- Channels: add, enable/disable, delete
- Membership/Premium plans: create, grant, extend by plan duration, revoke, expiry tracking, events
- Watermark controls and membership-based suppression
- Bot settings: support, news, language, privacy
- Statistics
- Tutorial Center
- Audit log
- Feature flag table for future module-level controls
- No DROP/TRUNCATE in initialization; tables use foreign keys, unique constraints and indexes

## Railway variables
- `DATABASE_URL` = Railway PostgreSQL connection string
- `MANAGER_BOT_TOKEN` = token for the manager bot
- `ADMIN_ID` = master admin Telegram numeric ID
- `ADMIN_USERNAME` = admin username without @
- `MANAGER_NAME` = optional branding name
- `TOKEN_ENCRYPTION_KEY` = persistent Fernet key (strongly recommended)

Generate a persistent key once with:
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## Start command
`python main.py`

## Important
The connected bot token is accepted through the manager chat and is deleted immediately after receipt, then stored encrypted in PostgreSQL. Still use a private 1-to-1 manager chat and never paste bot tokens into groups.

Telegram channel Force Join requires the managed bot to have enough channel permissions to inspect membership and receive join-request updates. Join Requests remain pending; this application does not approve or decline them automatically.

This package is designed as a strong production-style runtime foundation. Telegram API limits, hosting limits, and individual channel permissions still apply.
