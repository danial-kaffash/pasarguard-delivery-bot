# pasarguard-delivery-bot

A **multi-tenant** Telegram bot that delivers free trial xray configs from your PasarGuard panels. Manage multiple panels and channels from a single bot instance.

- Manages **multiple PasarGuard panels** and **multiple Telegram channels**
- Posts a **pinned promo message** (per-channel, owner-editable, silent) in each channel
- When a user taps the button and presses **Start**, offers **server/location choices** (curated per-channel, with friendly labels like "🇳🇱 هلند")
- When a user **requests to join** the channel, delivers a trial config via DM first, then approves after a configurable delay
- Creates a **trial account** on the correct panel and delivers the **subscription URL**
- **Role-based access**: superadmins control everything, admins manage their assigned channels
- **Inline management panel** (`/panel`) — button-based UI for all settings
- Tracks channel joins/leaves and exposes everything through commands

## Quick start

### 1. Environment variables

```bash
cp .env.example .env
```

Edit `.env` — only these are required:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
OWNER_TG_IDS=your_telegram_id
DB_ENCRYPTION_KEY=                    # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Everything else (panels, channels, trial settings) is managed through the bot at runtime.

### 2. Channel setup (one-time, in Telegram)

For **each** channel you want to manage:
1. Add the bot as **admin** with these rights:
   - *Post Messages*
   - *Pin Messages*
   - *Invite Users via Link* (for approving join requests)
2. Enable **"Approve New Members"** in channel settings (Privacy → who can send requests)

### 3. Panel setup

Create a dedicated admin on each PasarGuard panel (e.g. `greet-bot`) with permissions: **users: create/read** and **groups: read**.

### 4. Run

```bash
# Docker
docker compose up -d --build

# Development
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m bot.main
```

### 5. Configure via the bot

After starting, DM the bot:

```
/addpanel NL https://nl.example.com admin password123
/addpanel TR https://tr.example.com admin password456
/addchannel -1001234567890
/addchannel -1009876543210
/setoffer -1001234567890 1 2 🇳🇱 هلند
/setoffer -1001234567890 2 5 🇹🇷 ترکیه
```

Channel titles are fetched from Telegram automatically. If a channel has no title (e.g. migrated from old config), run `/refreshchannels` or restart the bot.

Done. Users can now get trials from your channel.

---

## Inline management panel (`/panel`)

Type `/panel` in DM to the bot. Shows a button-based UI:

```
📺 Channel list → tap a channel
├─ ⏸ Pause / ▶️ Resume
├─ 📢 Promo → edit text, interval, toggle pin/silent, post now
├─ 🎁 Trials → edit data limit, days, grace, regrant, max age, reset user
├─ 🔗 Join Requests → toggle pause, edit delay
├─ 🌐 Offer Groups → view, add (wizard), remove, clear all
└─ 📊 Stats

🖥 Panels → manage all panels (superadmin)
💾 Backup → download database or export config (superadmin)
```

Every edit: tap button → type value → saved. No need to remember command syntax.

---

## Superadmin commands

| Command | Description |
|---------|-------------|
| `/panel` | Open the inline management panel |
| `/addpanel <name> <url> <user> <pass>` | Register a PasarGuard panel |
| `/panels` | List all panels |
| `/editpanel <id> <field> <value>` | Edit a panel field (name, base_url, admin_password, verify_ssl, timeout_seconds, protocols, auto_delete_days) |
| `/removepanel <id>` | Soft-delete a panel |
| `/addchannel <tg_id>` | Register a Telegram channel (title fetched from Telegram) |
| `/channels` | List all channels |
| `/refreshchannels` | Re-fetch channel titles from Telegram for channels with empty titles |
| `/editchannel <tg_id> <field> <value>` | Edit a channel field (title, trial_data_limit_gb, trial_days, on_hold_grace_days, allow_regrant_after_days, trial_max_member_age_days, join_approval_delay_seconds, promo_interval_hours, promo_pin, promo_silent, post_delete_previous) |
| `/removechannel <tg_id>` | Soft-delete a channel |
| `/assign <user_id> <tg_id>` | Assign an admin to a channel |
| `/unassign <user_id> <tg_id>` | Remove an admin from a channel |
| `/promote <user_id> <role>` | Promote user to admin/superadmin |
| `/demote <user_id>` | Demote user to regular user |
| `/users` | List all users with roles |
| `/sysstats` | System-wide stats |
| `/backup` | Send the SQLite database file as a document |
| `/restore` | Reply to a .db backup file with `/restore` — replaces database and restarts bot |
| `/export` | Export configuration as portable JSON (panels, channels, users, offer groups) |
| `/import` | Reply to a JSON export file with `/import` to restore configuration |

## Channel-scoped commands

These work **in the channel** (context auto-detected) or **in DM** with explicit channel ID.

| Command | In channel | In DM |
|---------|-----------|-------|
| `/pause` | `/pause` | `/pause <tg_id>` |
| `/resume` | `/resume` | `/resume <tg_id>` |
| `/pausejoins` | `/pausejoins` | `/pausejoins <tg_id>` |
| `/resumejoins` | `/resumejoins` | `/resumejoins <tg_id>` |
| `/setpromo` | `/setpromo <text>` | `/setpromo <tg_id> <text>` |
| `/setinterval` | `/setinterval <hours>` | `/setinterval <tg_id> <hours>` |
| `/promonow` | `/promonow` | `/promonow <tg_id>` |
| `/getpromo` | `/getpromo` | `/getpromo <tg_id>` |
| `/settrial` | `/settrial <field> <value>` | `/settrial <tg_id> <field> <value>` |
| `/setjoindelay` | `/setjoindelay <seconds>` | `/setjoindelay <tg_id> <seconds>` |
| `/setmaxage` | `/setmaxage <days>` | `/setmaxage <tg_id> <days>` |
| `/groups` | `/groups` | `/groups <tg_id>` |
| `/offergroups` | `/offergroups` | `/offergroups <tg_id>` |
| `/setoffer` | `/setoffer <panel_id> <group_id> <label>` | `/setoffer <tg_id> <panel_id> <group_id> <label>` |
| `/deloffer` | `/deloffer <panel_id> <group_id>` | `/deloffer <tg_id> <panel_id> <group_id>` |
| `/reorder` | `/reorder <p>:<g>,<p>:<g>,...` | `/reorder <tg_id> <p>:<g>,...` |
| `/clearoffers` | `/clearoffers` | `/clearoffers <tg_id>` |
| `/reset` | `/reset <user_id>` | `/reset <tg_id> <user_id>` |
| `/stats` | `/stats` | `/stats <tg_id>` |
| `/joinstats` | `/joinstats` | `/joinstats <tg_id>` |
| `/newpost` | `/newpost` | `/newpost <tg_id>` |
| `/posts` | `/posts` | `/posts <tg_id>` |
| `/checkpremium` | — | `/checkpremium` |

Trial setting fields for `/settrial`: `data_limit_gb`, `days`, `grace`, `regrant`

### Channel posts (`/newpost`, `/posts`)

Publish arbitrary posts through the bot — immediate, scheduled, or recurring
(daily/weekly at HH:MM, Tehran time). The `/newpost` wizard walks through
content (text / photo / video / animation / forward — formatting and premium
emojis preserved), inline buttons (URL / no-op / copy-to-clipboard, native
green/red/blue colors, premium-emoji icons), layout, options
(delete-previous, pin, silent, link preview, ephemeral auto-delete 1–24h),
schedule and a live preview before confirming. One post can fan out to
several channels at once. `/posts` lists a channel's posts and can send now,
cancel, reschedule, edit the published text in place, copy as new, or delete.
Design details: `docs/channel-posts-plan.md`. `/checkpremium` checks whether
this bot can send premium emojis (requires a Fragment username).

---

## Architecture

```
bot/
  handlers/
    admin.py          # slash commands (superadmin + channel-scoped)
    backup.py         # /backup, /export, /import
    panel.py          # inline management panel (/panel)
    posts.py          # /newpost wizard, /posts management, /checkpremium
    trial.py          # /start → group select → trial delivery
    join_request.py   # channel join-request → trial → approve
    member_events.py  # join/leave tracking
  promo.py            # multi-channel promo scheduler
  pause.py            # pause switches (global + per-channel)
  config.py           # pydantic settings from .env
panel/
  client.py           # PasarGuard API client (httpx, auto re-auth)
  manager.py          # multi-panel client manager (lazy cache)
  models.py           # pydantic models
services/
  trial.py            # eligibility, trial creation, group validation
  channel_settings.py # Channel+Panel → settings adapter
  posts.py            # channel-posts logic + 30s scheduler (send/edit/recurrence/expiry)
storage/
  db.py               # SQLite schema + CRUD (panels, channels, users, grants, ...)
  crypto.py           # Fernet encryption for panel passwords
```

---

## How it works

```
Superadmin adds panels and channels via /addpanel, /addchannel, /assign.
Offer groups are linked via /setoffer — each group maps to a panel.

Channel promo runs automatically (per-channel interval):
  message pinned silently with deep-link button

User taps button → /start:
  bot resolves channel from deep-link
  shows offer group buttons (from all linked panels, friendly labels)
  user picks one → trial created on that panel → sub URL delivered

User sends join request:
  bot resolves channel from DB
  creates trial → DMs sub link → approves after delay
  user ID recorded — no duplicate trials
```

## Two independent pause switches

- **`/pause` / `/resume`** — stops promo posts **and** trial delivery for a channel
- **`/pausejoins` / `/resumejoins`** — when paused, join requests are approved immediately without a trial

## Join-request behavior

| Situation | What happens |
|-----------|-------------|
| Eligible (no prior trial) | Trial created → DM → approve after delay |
| Already has an active trial | Approve immediately → DM with existing sub link |
| In cooldown | Approve immediately → DM mentioning cooldown |
| No offer groups | Approve immediately |
| Panel error | Approve immediately → DM "try again later" |
| DM fails | Still approves — user is never blocked |

## Roles

| Role | Controls |
|------|----------|
| **superadmin** | Everything — panels, channels, users, all commands |
| **admin** | Assigned channels — full control |
| **user** | End users — /start and join-requests only |

---

## Development

```bash
scripts/devenv.sh setup       # one-time: create .venv + install all deps
scripts/devenv.sh check       # verify everything is green (lint, format, tests, coverage)
python -m bot.main            # run the bot
```

`devenv.sh check` runs in any fresh session and fails loudly if anything is
red: python ≥ 3.11, `.venv`, dependencies, `pip check`, `.env` (warn-only),
`ruff check`, `ruff format --check`, and `pytest` with a **coverage gate
(≥ 80%)** — the suite fails if coverage drops below the threshold configured
in `pyproject.toml`.

Individual commands:

```bash
scripts/devenv.sh test        # pytest with coverage summary
scripts/devenv.sh lint        # ruff check + format check
scripts/git-safety.sh install # enforce git policy (no rebase/squash/force-push)
pytest -q                     # 389 tests, ~85% coverage
ruff check .                  # lint
```

CI is intentionally not used — run `scripts/devenv.sh check` locally before
every commit; it enforces the same gate (lint, format, tests, coverage).

Project docs: [CHANGELOG.md](CHANGELOG.md) for history,
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) for session-to-session context.
