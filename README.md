# pasarguard-greet-bot

A Telegram bot that markets a **free 5 GB test** for your PasarGuard xray panel:

- posts a **pinned promo message** (owner-editable, every N hours, silent — no pings,
  no per-join posts, no mentions) into your Telegram channel;
- when a user taps the button and presses **Start**, offers a **multi-select of
  panel groups** (curated by the owner, with custom Persian labels);
- creates a **5 GB on-hold test account** on the panel via its REST API and replies
  with the **subscription URL**;
- tracks channel joins/leaves and exposes everything through **owner commands**.

Full design: see **[PLAN.md](PLAN.md)**.

## Status — all milestones done ✅

| Milestone | Description | State |
|---|---|---|
| M1 | Skeleton: config, logging, Docker | ✅ |
| M2 | PasarGuard API client (typed, auto re-auth) + tests | ✅ |
| M3 | Channel promo scheduler (pinned post) | ✅ |
| M4 | Trial flow: /start → group select → 5 GB account → sub URL | ✅ |
| M5 | Owner commands, join/leave stats, global error handling, setup guide | ✅ |

## How it works

```
Every PROMO_INTERVAL_HOURS (default 6h):
  channel post replaced → your predefined Persian message pinned silently,
  with a "🎁 دریافت تست ۵ گیگ" button → https://t.me/<bot>?start=join

User taps button → /start in bot:
  eligibility check (one trial per user, 30-day re-grant cooldown)
  → multi-select keyboard of your curated groups (✅ toggles)
  → confirm → POST /api/user on the panel (5 GB, on-hold: 3-day usage from
    first connection, 7 days to activate, vless, auto-delete day 11)
  → bot replies with the subscription URL + trial facts
```

## Layout

```
bot/            # Telegram bot: config, logging, entrypoint, promo scheduler
  handlers/     # trial flow (M4), admin commands (M5), chat_member tracking (M5)
panel/          # PasarGuardAPI client: models, exceptions, async client
services/       # trial business logic (eligibility, payload builder, caching)
storage/        # SQLite (aiosqlite): settings, promo state, offer groups,
                # trial grants, chat members, member events
texts/          # default promo message (Persian)
tests/          # 68 tests (respx-mocked HTTP, FakeBot/FakePanel, real SQLite)
data/           # runtime data (SQLite DB — git-ignored; offer_groups.json seed)
```

## Setup (one-time)

1. **@BotFather** → `/newbot`, copy the token into `.env` (`TELEGRAM_BOT_TOKEN`).
2. **Your channel** → add the bot as **admin** with at least:
   - *Post Messages* (promo posts)
   - *Pin Messages* (pinned CTA)
   - *Add New Admins* **off**; the bot also needs membership visibility, which
     admin status provides (used for `chat_member` join/leave tracking).
   Put the channel id (e.g. `-1001234567890`) into `CHANNEL_ID`.
3. **PasarGuard panel** → create a dedicated admin (e.g. `greet-bot`) with the
   permissions **users: create/read** and **groups: read**; put its credentials
   into `PANEL_ADMIN_USERNAME` / `PANEL_ADMIN_PASSWORD`.
4. Fill the rest of `.env` from `.env.example` (owner Telegram ids, trial knobs).
5. Configure the groups you want to offer:
   - quick way: edit `data/offer_groups.json` before first start —
     `[{"id": 2, "label": "🇳🇱 هلند"}, {"id": 5, "label": "🇹🇷 ترکیه"}]`
     (`id` = panel group id, find them with `/groups` after start), or
   - at runtime with `/setoffer` (see below).

## Run

```bash
cp .env.example .env          # fill in real values
docker compose up -d --build  # VPS long-polling — no public HTTPS needed
```

Development:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m bot.main            # long-polling run
pytest -q                     # 68 tests
```

## Owner commands (restricted to `OWNER_TG_IDS`)

| Command | What it does |
|---|---|
| `/setpromo <text>` | Change the channel promo message (HTML allowed), no restart |
| `/setinterval <hours>` | How often the promo post is refreshed (e.g. `6`) |
| `/promonow` | Publish + pin the promo post immediately |
| `/getpromo` | Show current text, interval, next run time |
| `/groups` | List **all panel groups** with ids (from the panel, live) |
| `/offergroups` | Show your curated offer list + warn about deleted panel groups |
| `/setoffer <id> <label>` | Add/update an offered group, e.g. `/setoffer 2 🇳🇱 هلند` |
| `/deloffer <id>` | Remove a group from the offer list |
| `/reorder <id1>,<id2>,…` | Set the button display order |
| `/clearoffers` | Empty the list (pauses trials with a polite message) |
| `/reset <tg_user_id>` | Revoke a user's trial so they can claim again |
| `/stats` | Channel members, joins/leaves (24 h), grants total/active, next promo |

An **empty offer list pauses trials** — the bot replies "در حال حاضر تست رایگان
موجود نیست 🙏" instead of offering all panel groups. You are always in control.

## Trial settings (`.env`)

- `TRIAL_DATA_LIMIT_GB=5` — test size
- `TRIAL_DAYS=3` — usage window **after first connection** (on-hold)
- `ON_HOLD_GRACE_DAYS=7` — deadline for the first connection
- `TRIAL_PROTOCOLS=vless` — comma-separated protocols
- `AUTO_DELETE_DAYS=11` — panel-side cleanup of finished trials
- `ALLOW_REGRANT_AFTER_DAYS=30` — cooldown before a user may re-claim
- `PANEL_VERIFY_SSL=true` — set `false` if the panel uses a self-signed cert
