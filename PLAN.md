# PasarGuard Greet Bot — Detailed Plan

> Status: **FINAL — decisions locked (see section 9)**
> Repo: `pasarguard-greet-bot` · Branch: `arena/019fa280-pasarguard-greet-bot`
> Panel: PasarGuardAPI v5.0.3 @ `https://panelnet2.paqet.ir:8000` (Marzban-style API)

---

## 1. Goal

A Telegram bot that:

1. **Publishes a pinned promo post** in your Telegram **channel** every N hours (customizable): a predefined, owner-editable message with a "🎁 دریافت تست ۵ گیگ" deep-link button. No per-join posts, no mentions, subscribers never pinged.
2. **Greets the user personally inside the bot's private chat** the moment they tap the button and press Start (Telegram only allows DMs after the user starts the bot).
3. Lets the user **pick one or more panel "groups"** (server groups defined in PasarGuard) via an inline multi-select keyboard.
4. **Creates a 5 GB test account** for them on the PasarGuard xray panel via the panel's REST API, scoped to the groups they selected (on-hold: activates on first connection).
5. **Delivers the subscription URL** back to the user in the bot chat.
6. (Side benefit) Tracks channel joins/leaves for an owner-only `/stats` command.

---

## 2. Reality checks (Telegram platform constraints) ⚠️

These shape the whole design:

| Constraint | Consequence |
|---|---|
| Bots receive **no `new_chat_members` events in channels** (that only exists for groups). | For a channel we must use **`chat_member` updates**: the bot must be a **channel admin** and we register the webhook/long-poll with `allowed_updates = ["chat_member", "message"]`. A join shows up as `old_chat_member.status ∈ {left, kicked}` → `new_chat_member.status ∈ {member, administrator}`. |
| **A bot cannot DM a user first.** The user must press **Start** in the bot at least once. | The channel welcome post includes a **deep-link button** → `https://t.me/<BotUsername>?start=join`. The 5 GB flow happens in the private chat *after* the user taps Start. |
| Bots can't post in a channel without **Post Messages** admin right; can't read member lists without **admin** status. | Bot must be channel admin with at least: Post Messages (+ Edit/Delete own posts). |
| Flood limits (~20 msg/min to the same chat, ~30 msg/s global). | Queue + throttle outgoing messages; dedupe join events (Telegram may resend updates). |
| `chat_member` updates are only delivered for the **exact chats** the bot is admin in, and (for webhook mode) only if `allowed_updates` includes them. | Configure updates explicitly at startup. |

---

## 3. End-user journey

```
Channel presence (independent of joins):
   Every PROMO_INTERVAL_HOURS (configurable, default 6h) the bot:
     • deletes/unpins the previous promo post,
     • posts the predefined promo message (owner-editable) with a
       "🎁 دریافت تست ۵ گیگ" deep-link button → t.me/<bot>?start=join_<tg_id is NOT used here; plain "join">,
     • pins it silently (disable_notification=true — subscribers are never pinged).
   → The channel ALWAYS has exactly one pinned CTA post. No per-join posts,
     no @mentions, no feed noise.

User taps the button
      │
      ▼
Bot opens in private chat with /start (deep-link payload "join")
      │
      ▼
Bot greets the user personally in the private chat (allowed — they tapped Start first),
then: "گروه‌های موردنظرت رو برای تست ۵ گیگ انتخاب کن:" — inline MULTI-SELECT keyboard built
from the owner-curated offer list (custom Persian labels; panel groups list cached ~5 min
only to validate the ids still exist)
      │
      ▼
User selects group(s) → "✅ تأیید" button
      │
      ▼
Bot calls panel API:
   1. POST /api/admin/token  (form: username/password) → bearer token (cached, re-auth on 401)
   2. POST /api/user with UserCreate payload (see §5)
      │
      ▼
Bot replies (in Persian) with:
   • Subscription URL (from UserResponse.subscription_url) + one-tap "Add to app" hints
   • Trial facts: 5 GB, on-hold activation (must connect within 7 days), selected groups
   (Ready-made config files deliberately out of scope for v1 — sub URL only.)
      │
      ▼
Record kept in SQLite → one trial per Telegram user, cleanup handled by the panel
(auto_delete_in_days) + one-per-user rule in our DB.
```

Notes:
- `chat_member` updates are still consumed, but only for **stats/join-tracking** (e.g. `/stats` admin command) and optional eligibility gating — never for posting.
- If the user never taps the button, nothing happens — by design (Telegram forbids unsolicited DMs anyway).

### 3.1 Channel presence: periodic pinned promo post

| Aspect | Behavior |
|---|---|
| Trigger | Async scheduler (aiogram task), every `PROMO_INTERVAL_HOURS` (default **6**, owner-adjustable at runtime via `/setinterval`) |
| Message | Owner-editable predefined text (`/setpromo` command, persisted in SQLite; seed default in `texts/promo_fa.txt`). Supports Telegram HTML formatting. |
| Button | Inline URL button "🎁 دریافت تست ۵ گیگ" → `https://t.me/<BotUsername>?start=join` |
| Pinning | Each cycle: delete previous promo post (id persisted in DB) → send new → `pinChatMessage(disable_notification=true)` → save new message id |
| Noise | Subscribers are **never** pinged; channel always shows exactly one pinned promo post |
| Restart-safety | Last promo message id + next run time persisted in SQLite; scheduler re-syncs on startup |
| Owner tools | `/setpromo <text>`, `/setinterval <hours>`, `/promonow` (force immediate post), `/getpromo` (show current text/interval) |

---

Single Python service (async), no external services beyond SQLite.

```
pasarguard-greet-bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # entrypoint: wiring, webhook/long-poll switch
│   ├── config.py            # pydantic-settings, loads .env
│   ├── handlers/
│   │   ├── member_events.py # chat_member join detection + welcome post
│   │   ├── start.py         # /start + deep-link payload handling
│   │   ├── trial.py         # group selection FSM, confirm, delivery
│   │   └── admin.py         # owner-only commands (/stats, /reset <user>, /groups, /broadcast)
│   ├── keyboards.py         # inline keyboard builders (groups, confirm)
│   └── texts.py             # all UI strings (FA/EN) in one place
├── panel/
│   ├── client.py            # PasarGuardApiClient (httpx.AsyncClient wrapper)
│   ├── models.py            # pydantic mirrors of GroupSimple, UserCreate, UserResponse, Token
│   └── exceptions.py
├── storage/
│   ├── db.py                # SQLite (aiosqlite) — schema + repo functions
│   └── models.py
├── services/
│   └── trial.py             # business logic: dedupe, eligibility, create trial user
├── tests/
│   ├── test_panel_client.py # against recorded/mocked responses
│   └── test_trial_flow.py
├── .env.example
├── docker-compose.yml       # optional deployment
├── Dockerfile
├── requirements.txt
└── README.md
```

**Library choices**

- **aiogram 3.x** — modern async Telegram framework, first-class `chat_member` update support, FSM for the group-selection conversation.
- **httpx** (async) for the panel API.
- **aiosqlite** + tiny repository layer (no ORM — overkill here).
- **pydantic v2 / pydantic-settings** for config + API models.
- Python **3.11+**.

---

## 5. PasarGuard API integration (verified against the live OpenAPI spec)

Base URL: `https://panelnet2.paqet.ir:8000` (TLS cert is self-signed / non-public-CA → httpx client will need an **optional `verify=False` flag** controlled by env `PANEL_VERIFY_SSL=true|false`, default true).

### 5.1 Auth
```
POST /api/admin/token
Content-Type: application/x-www-form-urlencoded
Body: grant_type=password&username=<PANEL_ADMIN_USER>&password=<PANEL_ADMIN_PASS>
→ 200 {"access_token": "...", "token_type": "bearer"}
```
- Store token in memory; on any 401 → re-login once and retry.
- The admin account needs permissions: **users:create/read**, **groups:read** (a scoped role is fine; recommend a dedicated `greet-bot` admin, not the owner).

### 5.2 List groups (for the selection keyboard)
```
GET /api/groups/simple  → {"groups": [{"id": int, "name": str, ...}], "total": int}
```
Cache 300 s in memory. The keyboard is **not** built from all groups — it's built from the **owner-curated offer list** (see §5.2.1); the API call is only used to resolve/validate IDs and to power the `/groups` lookup command.

#### 5.2.1 Owner-curated offer list (custom labels, runtime-editable)
An ordered mapping of **panel group id → Persian button label**, e.g.:

```json
[
  {"id": 2, "label": "🇳🇱 هلند"},
  {"id": 5, "label": "🇹🇷 ترکیه"},
  {"id": 9, "label": "🇩🇪 آلمان"}
]
```

- **Storage**: SQLite `offer_groups(id INTEGER PRIMARY KEY, label TEXT, sort_order INTEGER, updated_at TEXT)`, seeded on first start from `data/offer_groups.json`.
- **Keyboard**: shown in `sort_order`; each button's callback carries the numeric group id; labels are exactly what the owner typed.
- **Validation**: on keyboard build, ids missing from the panel (deleted groups) are skipped and logged; owner warned via `/offergroups`.
- **Empty list**: if the offer list is empty, the bot tells the user "در حال حاضر تست رایگان موجود نیست 🙏" instead of falling back to all groups (owner is in full control).
- **Owner commands** (runtime, no restart):
  - `/groups` — list ALL panel groups with ids (lookup helper)
  - `/offergroups` — show the current offer list (order, labels, ids)
  - `/setoffer <id> <label>` — add/update an entry (label = rest of the message, Persian/emoji OK); new entries append to the end
  - `/reorder <id1>,<id2>,...` — set the display order explicitly
  - `/deloffer <id>` — remove an entry
  - `/clearoffers` — empty the list (disables trials until repopulated)

### 5.3 Create the 5 GB test user
```
POST /api/user
Authorization: Bearer <token>
Content-Type: application/json
```
Payload (`UserCreate` schema, verified):
```json
{
  "username": "t<telegram_id>_<6-char-random>",
  "data_limit": 5368709120,
  "data_limit_reset_strategy": "no_reset",
  "status": "on_hold",
  "on_hold_expire_duration": 259200,
  "on_hold_timeout": "2026-08-03T12:00:00Z",
  "group_ids": [<selected group ids, multi-select>],
  "proxy_settings": { "vless": {} },
  "note": "telegram-greet-bot tg_id=<telegram_id>",
  "auto_delete_in_days": 11
}
```
Notes:
- `data_limit` is **bytes** → 5 GB = `5368709120` (GiB). Configurable via env.
- **Chosen mode: `on_hold`** (activates when the user first connects):
  - `status = "on_hold"`
  - `on_hold_expire_duration = TRIAL_DAYS * 86400` → the 5 GB window runs for `TRIAL_DAYS` (default **3**) **starting at first connection**.
  - `on_hold_timeout = now + ON_HOLD_GRACE_DAYS` (default **7 days**) → if they never connect within the grace window, the account expires unused.
  - (`fixed` mode stays available behind config for later, but on-hold is the default.)
- `proxy_settings`: `ProxyTable` supports `vless / vmess / trojan / shadowsocks / wireguard / hysteria`; `{}` lets the panel auto-generate credentials. Default **vless-only**; configurable via env `TRIAL_PROTOCOLS=vless,trojan`.
- `auto_delete_in_days` keeps the panel tidy after the trial.
- On `409 Conflict` (username exists) → regenerate suffix and retry once.

Response (`UserResponse`) gives us everything: `id`, `username`, `status`, `subscription_url`, `used_traffic`, `expire`, …

### 5.4 Deliver configs
- Primary: send `subscription_url` (works with any sub-aware app).
- Nice-to-have: `GET /api/user/{user_id}/subscription/{client_type}` to fetch ready configs (client types per panel: `xray`, `sing-box`, `clash-meta`, `outline`, …) and send as files with quick-start captions.
- Also include the panel's own subscription page link if `SUB_URL_PREFIX` is set.

### 5.5 What we deliberately do **not** use
- The panel's **Notifications/Webhook settings** (the docs anchor you linked) are for *panel→admin* alerts (user expired, etc.). Our bot talks to the Users/Groups API directly, so we don't need them. We *could* optionally enable the panel's webhook to auto-disable/delete accounts, but `auto_delete_in_days` + panel-side expiry already cover cleanup.

---

## 6. Data model (SQLite)

```sql
CREATE TABLE trial_grants (
  tg_user_id      INTEGER PRIMARY KEY,
  tg_username     TEXT,
  panel_username  TEXT NOT NULL,
  panel_user_id   INTEGER,
  group_ids       TEXT,            -- json list
  data_limit      INTEGER,
  expire_at       TEXT,
  created_at      TEXT NOT NULL,
  source_chat_id  INTEGER,         -- which channel/group they joined
  revoked         INTEGER DEFAULT 0
);

CREATE TABLE offer_groups (        -- owner-curated groups offered to users
  id INTEGER PRIMARY KEY,          -- panel group id
  label TEXT NOT NULL,             -- Persian button label (emoji OK)
  sort_order INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE settings (            -- runtime-editable owner settings (promo text, interval, …)
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);

CREATE TABLE seen_updates (        -- dedupe Telegram updates
  update_id INTEGER PRIMARY KEY, seen_at TEXT
);

CREATE TABLE chat_members (        -- optional: track join/leave for /stats
  chat_id INTEGER, tg_user_id INTEGER, status TEXT, at TEXT,
  PRIMARY KEY (chat_id, tg_user_id)
);
```

Eligibility rule: **one active trial per Telegram user** (lookup by `tg_user_id` where `revoked=0` and not expired). Admins can `/reset <tg_id>` to allow a re-grant.

---

## 7. Configuration (`.env`)

```
# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...
CHANNEL_ID=-1001234567890              # channel where the promo post is published
STATS_CHAT_IDS=                        # optional extra chats for join/leave tracking
UPDATE_MODE=long-polling               # (webhook mode possible but not needed for the Docker/VPS setup)

# Channel promo post (FINAL: periodic pinned post, no per-join messages, no mentions)
PROMO_INTERVAL_HOURS=6                 # how often to (re)post — runtime-changeable via /setinterval
PROMO_PIN=true                         # keep the latest promo post pinned
PROMO_SILENT=true                      # pin/post without notifying subscribers
# The promo TEXT itself is owner-editable at runtime via /setpromo;
# default seeded from texts/promo_fa.txt (HTML formatting allowed).

# PasarGuard panel
PANEL_BASE_URL=https://panelnet2.paqet.ir:8000
PANEL_ADMIN_USERNAME=greet-bot
PANEL_ADMIN_PASSWORD=***
PANEL_VERIFY_SSL=true                  # set false for self-signed certs

# Trial (FINAL: on-hold mode, multi-select all groups)
TRIAL_DATA_LIMIT_GB=5
TRIAL_DAYS=3                           # usage window after first connection
ON_HOLD_GRACE_DAYS=7                   # must connect within this many days
TRIAL_PROTOCOLS=vless
OFFER_GROUPS_FILE=data/offer_groups.json  # seed file for the curated offer list (runtime edits live in DB)
AUTO_DELETE_DAYS=11                    # grace + usage + 1
ALLOW_REGRANT_AFTER_DAYS=30

# Owner
OWNER_TG_IDS=12345678                  # comma-separated, for /admin commands
DEFAULT_LANG=fa                        # fa | en
```

---

## 8. Anti-abuse & robustness

- **One trial per Telegram user id** (DB-enforced) + optional cooldown window.
- **Deep-link payload**: the button uses a plain `?start=join` marker (no user-specific payload — the button is in a public channel post). The grant is keyed to the *starter's* Telegram id. Optional stricter mode (config `REQUIRE_CHANNEL_MEMBERSHIP=true`): only users currently subscribed to the channel get the offer — the bot can check via `getChatMember` since it's a channel admin.
- **Join-event dedupe**: `seen_updates` table + in-memory LRU (Telegram retries webhooks).
- **Rate limiting** on outbound messages (aiogram throttler middleware).
- **Username collision** retry; **401 re-auth**; **timeout + retry with backoff** for panel calls; all errors surfaced to user as friendly "try again later" + logged.
- No secrets in logs; `.env` git-ignored (already covered by repo `.gitignore`).
- Channel stays clean by design: exactly one pinned promo post at a time; each cycle deletes the previous one before re-posting.

---

## 9. Decisions (locked 2026-07-27)

| # | Question | Decision |
|---|---|---|
| 1 | Channel vs group | **Channel** → bot added as channel admin, joins detected via `chat_member` updates |
| 2 | Trial activation | **On-hold**: 3-day usage window starts at first connection; 7-day grace to connect; auto-delete after 11 days |
| 3 | Group selection | **Multi-select** from an **owner-curated offer list** (group id → custom Persian label, ordered); runtime-editable via `/setoffer`, `/deloffer`, `/reorder`, `/offergroups`; panel lookup via `/groups`; empty list ⇒ trials paused |
| 4 | Language / delivery | **Persian UI**, deliver **subscription URL only** (no config files in v1) |
| 5 | Deployment | **VPS + Docker, long-polling** (no webhook/public HTTPS needed) |
| 6 | Channel presence | **Periodic pinned promo post** every 6 h (configurable via `/setinterval`), owner-editable text (`/setpromo`), silent pinning, old post replaced each cycle — **no per-join posts, no mentions** |

Setup checklist for you (one-time):
1. **BotFather** → create bot, disable privacy mode isn't required (service updates are always delivered), note the token.
2. **Channel**: add the bot as **admin** with *Post Messages* right.
3. **Panel**: create a dedicated admin (e.g. `greet-bot`) with `users: create/read` + `groups: read`, give it its password.
4. Fill `.env` from `.env.example`, then `docker compose up -d`.

---

## 10. Implementation milestones (all complete ✅)

1. **M1 — Skeleton**: repo layout, config loader, logging, `.env.example`, Dockerfile + docker-compose.
2. **M2 — Panel client**: auth w/ auto-refresh, `list_groups`, `create_user`, `get_user`, typed models; unit tests with mocked httpx.
3. **M3 — Channel promo scheduler**: periodic post task (interval from config/DB), replace-old → send → silent-pin, message-id persistence, restart re-sync, default Persian text in `texts/promo_fa.txt`.
4. **M4 — Trial flow**: /start (deep-link payload) handling with Persian greeting, multi-select keyboard from the curated offer list (DB + validation against `GET /api/groups/simple`, cached 5 min), confirm step, on-hold user creation, deliver subscription URL, SQLite grant record, one-per-user rule + re-grant cooldown; seed `data/offer_groups.json`.
5. **M5 — Admin & polish**: owner commands — promo (`/setpromo`, `/setinterval`, `/promonow`, `/getpromo`), offer groups (`/groups`, `/offergroups`, `/setoffer`, `/deloffer`, `/reorder`, `/clearoffers`), grants (`/reset`, `/stats`); join/leave tracking via `chat_member`; error handling; README with setup guide (BotFather steps, channel-admin checklist, panel role checklist).
6. **Hardening** (post-M5): HTML-escaping of user names, per-user rate-limit middleware, real-HTTP integration tests (local mock panel), `python -m bot.smoke` pre-flight checker, ready-made CI workflow (`docs/github-ci.yml` — copy into `.github/workflows/` with a `workflows`-scoped token to enable).
