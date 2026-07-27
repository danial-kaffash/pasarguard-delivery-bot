# pasarguard-greet-bot

A Telegram bot that markets a **free 5 GB test** for your PasarGuard xray panel:

- posts a **pinned promo message** (owner-editable, every N hours, silent — no pings,
  no per-join posts, no mentions) into your Telegram channel;
- when a user taps the button and presses **Start**, offers a **multi-select of
  panel groups** (curated by the owner, with custom Persian labels);
- creates a **5 GB on-hold test account** on the panel via its REST API and replies
  with the **subscription URL**.

Full design: see **[PLAN.md](PLAN.md)**.

## Status

| Milestone | Description | State |
|---|---|---|
| M1 | Skeleton: config, logging, Docker | ✅ done |
| M2 | PasarGuard API client (typed, auto re-auth) + tests | ✅ done |
| M3 | Channel promo scheduler (pinned post) | ✅ done |
| M4 | Trial flow: /start → group select → 5 GB account → sub URL | ⏳ next |
| M5 | Owner commands, join/leave stats, polish, setup guide | ⏳ |

## Layout

```
bot/            # Telegram bot: config, logging, entrypoint, promo scheduler
  handlers/     # aiogram routers (/start placeholder until M4)
panel/          # PasarGuardAPI client: models, exceptions, async client
storage/        # SQLite (aiosqlite): settings, promo state, offer groups
texts/          # default promo message (Persian)
tests/          # pytest suite (HTTP mocked with respx; FakeBot for Telegram)
data/           # runtime data (SQLite DB — git-ignored; offer_groups.json seed)
```

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env          # fill in panel credentials & bot token
python -m bot.main            # M1: validate config and report
pytest -q                     # run the test suite
```

## Deployment (VPS + Docker, long-polling)

```bash
cp .env.example .env          # fill in real values
docker compose up -d --build
```

One-time setup checklist (details in PLAN.md §9):

1. **@BotFather** → create the bot, copy its token into `.env`.
2. **Channel** → add the bot as **admin** with *Post Messages*.
3. **Panel** → create a dedicated admin (e.g. `greet-bot`) with
   `users: create/read` + `groups: read`.

## Offered groups

`data/offer_groups.json` seeds the curated offer list (runtime edits via
`/setoffer` live in the DB from M4/M5). Format:

```json
[
  {"id": 2, "label": "🇳🇱 هلند"},
  {"id": 5, "label": "🇹🇷 ترکیه"}
]
```

`id` = panel group id (see `/groups` owner command once M5 is in).
An empty list pauses trials with a polite "not available right now" message.
