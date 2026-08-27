# Channel Posts — Manual & Scheduled Posting Plan

> **Status: IMPLEMENTED (v1) — shipped on branch `arena/01a042b5-pasarguard-delivery-bot`**
> **Decisions locked (2026-08-27):** single media + caption · native button
> colors · one-shot + recurring schedules · premium emojis with auto-fallback.
> **Scope widened same day (operator call):** the six §8 backlog items below
> were promoted into v1 — ephemeral posts, edit-published-in-place, recurring
> schedules, templates, multi-channel send, copy-last-post.

---

## 1. Goal

Superadmins and channel admins publish arbitrary posts to their managed
channels through the bot — immediate or scheduled — with:

- rich inline buttons: deep-links (URL), no-op ("normal text") buttons,
  copy-to-clipboard buttons
- native button colors: green / red / blue / default
- premium emojis in caption **and** button icons (with graceful fallback)
- optional deletion of the previous post before sending (channel-level
  default + per-post override)
- pin, silent, and link-preview options
- single media (photo / video / animation) with caption
- scheduling: send now, one-shot at a datetime, or recurring daily/weekly
  at HH:MM (Asia/Tehran), all manageable after creation
- **ephemeral posts** — auto-delete N hours (1/6/12/24) after sending
- **edit published posts in place** (text/caption + buttons, media stays)
- **multi-channel send** — one post fanned out to N channels in one go
  (shared `group_id` ties the copies together)
- **templates** — save any confirmed post as a reusable template
- **copy-last-post** — clone any previous post as the draft for a new one

Persian admin UX consistent with the rest of the bot.

---

## 2. Reality checks (Telegram platform constraints) ⚠️

| Constraint | Consequence |
|---|---|
| `InlineKeyboardButton.style` accepts `'success'` (green), `'danger'` (red), `'primary'` (blue), or omitted. Requires current Bot API + **aiogram ≥ 3.31** (requirements currently allow 3.13 → **floor must be raised**). | Native colors — no emoji-prefix workaround needed. |
| `InlineKeyboardButton.icon_custom_emoji_id` (premium button icons) is allowed for bots that purchased a username on **Fragment**, or in **private/group/supergroup** messages if the bot's **owner has Premium**. **Channel posts only qualify via Fragment.** | Icons are attempted; on rejection the whole send falls back (icons dropped). |
| `custom_emoji` entities in text: same Fragment-family restriction. | Attempt with entities → on `TelegramBadRequest` resend with custom-emoji entities/icons stripped (the underlying plain emoji character remains visible) → warn the admin once in Persian + `/checkpremium` diagnostic. |
| `disabled` field makes a button do nothing — native support for "plain text" buttons. | No fake-link hack needed. |
| Callback buttons in **channel** posts only reach the bot for users who already started it. | Channel posts use URL / disabled / copy buttons only. |
| Bots cannot read channel-post view counts. | No analytics feature. |
| Pin needs *Pin Messages* right, delete needs admin — both already required by the channel setup checklist. | OK. |
| Iran abolished DST (2022) — fixed **+03:30** year-round. | No DST handling; store UTC, display Tehran. |
| Message limit 4096 chars; button label ≤ 64 chars; practical row width ≤ 8. | Wizard validates and reports in Persian. |

---

## 3. End-to-end journey

### 3.1 Composing (`/newpost`, or «🆕 پست جدید» in the channel's `/panel` menu)

1. **Channel picker** — multi-select, only channels the caller manages
   (existing role scoping: superadmin sees all, admins see assigned).
   `/newpost <tg_id>` preselects one and jumps straight to content.
2. **Content** — the admin sends the post to the bot:
   - text message, or photo/video/animation **with caption**, or a forward
     (copied, forward header stripped);
   - the message's `entities` (bold/links/…) including **custom-emoji spans
     are preserved verbatim** — the post renders exactly like the preview.
3. **Button builder** (loop until done):
   - label (may itself contain a premium emoji: we extract its
     `custom_emoji_id` as the button icon and use the plain text as label);
   - action: **deep-link/URL** · **no-op** (disabled) · **copy text**;
   - color: 🟢 سبز / 🔴 قرمز / 🔵 آبی / بدون رنگ;
   - "another button?" → repeat.
4. **Layout** — row spec, e.g. `2,1` = two buttons in the first row, one in
   the second (default: one per row).
5. **Options** — delete-previous (default = channel's default) · pin ·
   silent · link preview · **ephemeral** (off → 1h → 6h → 12h → 24h → off).
6. **Schedule** — «ارسال فوری» / one-shot datetime / recurring
   «هر روز»/«هر هفته» at HH:MM, plus a **💾 save-as-template** toggle.
7. **Preview** — the bot renders the exact post to the admin privately
   (buttons included; premium emojis render privately even without Fragment
   if the bot owner has Premium). Confirm → sent/scheduled to every selected
   channel (one `channel_posts` row per channel, shared `group_id`).
   Cancel → discarded.

### 3.2 Managing (`/posts <tg_id>` + «📝 پست‌ها» in the panel)

- List: scheduled / recurring / last published posts of the channel.
- Scheduled/recurring/failed/sent: **send now** (recurring keeps its cadence,
  next run recomputed drift-free from the send time).
- Scheduled: reschedule, cancel.
- Published: **edit in place** — new text/caption (entities preserved) +
  buttons re-rendered via `editMessageText` / `editMessageCaption`; media
  itself is not swapped (Telegram limitation).
- **Copy as new** — any previous post becomes a preloaded draft, preview
  shown immediately.
- Delete (removes the channel message too when it still exists).

### 3.3 Channels default (`/editchannel <tg_id> post_delete_previous 1|0`)

The delete-previous default for that channel; the wizard preselects it and
every post can override. Also a toggle button in the channel's panel menu.

---

## 4. Data model

```sql
-- channels gains:
post_delete_previous INTEGER NOT NULL DEFAULT 0

-- new table:
CREATE TABLE channel_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  created_by INTEGER NOT NULL,          -- tg user id
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,

  -- content
  text TEXT NOT NULL DEFAULT '',        -- message text or media caption
  entities_json TEXT,                   -- preserved MessageEntity list (incl. custom_emoji)
  media_type TEXT,                      -- photo|video|animation|NULL
  media_file_id TEXT,                   -- Telegram file id (reusable)

  -- buttons: [{"label","action":{"type":"url|disabled|copy","url","text"},
  --            "style":"success|danger|primary|null,"icon":"<emoji_id>","row":N}]
  buttons_json TEXT NOT NULL DEFAULT '[]',

  -- options
  delete_previous INTEGER NOT NULL DEFAULT 0,
  pin INTEGER NOT NULL DEFAULT 0,
  silent INTEGER NOT NULL DEFAULT 0,
  link_preview INTEGER NOT NULL DEFAULT 1,

  group_id TEXT,                        -- shared across a multi-channel fan-out

  -- scheduling
  status TEXT NOT NULL DEFAULT 'draft',
  -- draft|scheduled|recurring|sent|failed|cancelled|expired
  scheduled_at TEXT,                    -- UTC; one-shot time, or NEXT run for recurring
  recurrence TEXT,                      -- none|daily|weekly
  recur_at TEXT,                        -- 'HH:MM' Tehran local
  last_sent_at TEXT,

  -- ephemeral
  ephemeral_hours REAL,
  expires_at TEXT,                      -- UTC, set at send time; scan deletes the message

  -- delivery
  sent_at TEXT,
  tg_message_id INTEGER,
  error TEXT
);

-- new table (templates):
CREATE TABLE post_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  entities_json TEXT,
  media_type TEXT,
  media_file_id TEXT,
  buttons_json TEXT NOT NULL DEFAULT '[]',
  delete_previous INTEGER NOT NULL DEFAULT 0,
  pin INTEGER NOT NULL DEFAULT 0,
  silent INTEGER NOT NULL DEFAULT 0,
  link_preview INTEGER NOT NULL DEFAULT 1,
  ephemeral_hours REAL
);
```

A scheduler tick (`services.posts.dispatch_due_posts`, every 30 s from a
dedicated task in `bot/main.py`) does one scan: due one-shots → send,
due recurring (scheduled_at ≤ now) → send + reschedule `next_occurrence`,
expired ephemerals → delete message + mark `expired`.

Schema evolution follows the existing pattern in `storage/db.py`
(create-at-boot + additive `ALTER TABLE` guarded by column checks).

---

## 5. Module layout

```
bot/handlers/posts.py     /newpost wizard (FSM), /posts management,
                          /checkpremium, PostsCB callbacks
bot/handlers/panel.py     «📝 پست‌ها» entry in the channel menu → posts view
bot/handlers/admin.py     /editchannel gains post_delete_previous
services/posts.py         pure-ish logic, fully unit-tested:
                          - build_keyboard(buttons) → InlineKeyboardMarkup
                            (style/action/icon mapping, row grouping)
                          - build_send_kwargs(post) → text/caption + entities
                            (no parse_mode; LinkPreviewOptions)
                          - extract_button_icon(text, entities) → UTF-16-safe
                            premium-emoji extraction from button labels
                          - send_post(bot, db, post, channel) → delete-previous,
                            send, premium-fallback retry, pin, expiry stamp
                          - edit_published_post(bot, post, channel)
                          - next_occurrence(post, after) → Tehran +03:30 math
                          - dispatch_due_posts / run_posts_scheduler (30 s tick)
                          - send_and_record / send_preview helpers
storage/db.py             channel_posts + post_templates CRUD,
                          channels.post_delete_previous migration,
                          create_channel/update_channel support
bot/main.py               register posts router + posts-scheduler task
requirements.txt          aiogram floor → >=3.31
```

---

## 6. Edge cases

- **Premium rejection in channel** (no Fragment username): resend stripped,
  Persian warning once per session, `/checkpremium` explains which paths
  work (private vs channel) for this bot.
- **Delete-previous fails** (already deleted / too old): log, still send.
- **Scheduled time in the past**: send immediately, tell the admin.
- **Forwarded media without caption**: fine — text-only buttons still work.
- **4096/64-char limits**: validated in the wizard with Persian errors.
- **URL validation**: only `http(s)://` and `tg://` accepted for URL buttons.
- **Bot lost admin rights in a channel**: send fails → post marked `failed`
  with the Telegram error, surfaced in `/posts`.
- **Restart recovery**: due-scan re-discovers scheduled posts from the DB;
  nothing lives only in memory. Partially-sent recurring posts re-send only
  after `last_sent_at` (idempotent per occurrence).
- **Ephemeral on recurring posts**: expiry deletes the *last* message; the
  post stays `recurring` and keeps its cadence (`tg_message_id`/`expires_at`
  cleared until the next send).
- **Multi-channel partial failure**: each channel's copy is independent —
  one failing marks only that row `failed`; the result summary reports
  per-channel outcomes.
- **Edit published with premium emoji**: same fallback as send (strip +
  retry); edit of the media itself is a Telegram limitation.

---

## 7. Testing plan

- `tests/test_posts_service.py` — keyboard builder (all styles × actions,
  rows, icon extraction from label entities), send kwargs, strip-fallback,
  recurrence math (daily/weekly crossing midnight UTC, Tehran offset),
  due-scan, delete-previous ordering.
- `tests/test_posts_handlers.py` — wizard FSM step by step with the fake
  army (channel picker, content capture incl. entity preservation, button
  loop, layout, options, schedule, preview, confirm), `/posts` management
  callbacks, `/checkpremium`.
- Every behavior fix gets a regression test demonstrated against the old
  code; coverage gate stays ≥ 80%.

---

## 8. Backlog (post-v1 ideas)

Promoted into v1 on 2026-08-27 (operator decision): ~~ephemeral posts~~ ·
~~edit published in place~~ · ~~recurring schedules~~ · ~~templates /
reuse-last-post~~ · ~~multi-channel send~~ · ~~copy-last-post~~.

Still deferred:

- **Keep-last-N retention** per channel.
- Media groups (albums).
- Button `pay` / Stars.
- Template *browsing/picking* UI (templates are currently only written by
  the wizard toggle; a "start from template" picker is future work).
- Editing published **media** (swap photo/video) — needs delete + repost.

---

## 9. Rollout

- No external migration (SQLite schema auto-creates; channels table gains a
  column additively).
- `docker compose up -d --build` (aiogram 3.31 floor included).
- Smoke: `/checkpremium` → `/newpost` a colored-button post with a premium
  emoji to a test channel → verify colors + fallback behavior → schedule a
  post one minute out → confirm it fires.
