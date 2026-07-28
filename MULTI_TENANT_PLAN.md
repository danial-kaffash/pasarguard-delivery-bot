# Multi-Tenant Architecture Plan

> **Status: DRAFT — awaiting review before implementation**
> **Goal:** Turn the single-channel bot into a multi-channel, multi-panel system with role-based access control and an inline-button management panel.

---

## 1. What changes and what doesn't

### Stays global (env var / bot-level)
| Setting | Why |
|---------|-----|
| `TELEGRAM_BOT_TOKEN` | One bot instance |
| `DB_PATH` | One database for everything |
| `LOG_LEVEL` | Global |
| `DEFAULT_LANG` | Global |
| `RATE_LIMIT_PER_MINUTE` | Global flood protection |

### Becomes per-panel (stored in DB)
| Current env var | Notes |
|-----------------|-------|
| `PANEL_BASE_URL` | Each panel is a separate PasarGuard instance |
| `PANEL_ADMIN_USERNAME` | Panel admin credentials |
| `PANEL_ADMIN_PASSWORD` | |
| `PANEL_VERIFY_SSL` | |
| `PANEL_TIMEOUT_SECONDS` | |
| `TRIAL_PROTOCOLS` | vless, trojan, etc. |
| `AUTO_DELETE_DAYS` | Panel-side cleanup |

### Becomes per-channel (stored in DB)
| Current env var | Notes |
|-----------------|-------|
| `CHANNEL_ID` | The Telegram channel |
| `PROMO_INTERVAL_HOURS` | Promo posting schedule |
| `PROMO_PIN` / `PROMO_SILENT` | Promo behavior |
| `TRIAL_DATA_LIMIT_GB` | Could vary per channel |
| `TRIAL_DAYS` | Usage window |
| `ON_HOLD_GRACE_DAYS` | Grace period |
| `ALLOW_REGRANT_AFTER_DAYS` | Cooldown |
| `TRIAL_MAX_MEMBER_AGE_DAYS` | New-member gate |
| `JOIN_APPROVAL_DELAY_SECONDS` | Join-request delay |
| Offer groups | Per-channel curated list |

---

## 2. New data model (SQLite)

### New tables

**Key relationship change:** A channel is **not** bound to a single panel.
Instead, each offer group is linked to both a channel and a panel.
This allows one channel to offer groups from multiple PasarGuard panels.

```sql
-- Panels: PasarGuard instances
CREATE TABLE panels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,              -- human label, e.g. "NL Panel"
    base_url        TEXT NOT NULL,
    admin_username  TEXT NOT NULL,
    admin_password  TEXT NOT NULL,              -- Fernet-encrypted (DB_ENCRYPTION_KEY env var)
    verify_ssl      INTEGER NOT NULL DEFAULT 1,
    timeout_seconds REAL NOT NULL DEFAULT 15.0,
    protocols       TEXT NOT NULL DEFAULT 'vless',   -- comma-separated
    auto_delete_days INTEGER NOT NULL DEFAULT 11,
    active          INTEGER NOT NULL DEFAULT 1,      -- soft-delete flag
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Channels: Telegram channels managed by the bot
CREATE TABLE channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_channel_id   INTEGER NOT NULL UNIQUE,    -- Telegram channel id, e.g. -1001234567890
    title           TEXT NOT NULL DEFAULT '',
    -- Trial settings
    trial_data_limit_gb   REAL NOT NULL DEFAULT 5.0,
    trial_days            INTEGER NOT NULL DEFAULT 3,
    on_hold_grace_days    INTEGER NOT NULL DEFAULT 7,
    allow_regrant_after_days INTEGER NOT NULL DEFAULT 30,
    trial_max_member_age_days REAL NOT NULL DEFAULT 0,
    -- Join-request settings
    join_approval_delay_seconds INTEGER NOT NULL DEFAULT 10,
    -- Promo settings
    promo_interval_hours  REAL NOT NULL DEFAULT 6.0,
    promo_pin             INTEGER NOT NULL DEFAULT 1,
    promo_silent          INTEGER NOT NULL DEFAULT 1,
    -- State
    active          INTEGER NOT NULL DEFAULT 1,      -- soft-delete / pause flag
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Users: role-based access control
CREATE TABLE users (
    tg_user_id      INTEGER PRIMARY KEY,
    username        TEXT,
    role            TEXT NOT NULL DEFAULT 'user',   -- 'superadmin' | 'admin' | 'user'
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Channel assignments: which admin can manage which channel
CREATE TABLE channel_admins (
    tg_user_id      INTEGER NOT NULL REFERENCES users(tg_user_id),
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tg_user_id, channel_id)
);

-- Channel offer groups: groups from any panel, linked to a channel
-- panel_id tells the bot which panel to create the trial on when this group is chosen
CREATE TABLE channel_offer_groups (
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    panel_id        INTEGER NOT NULL REFERENCES panels(id),
    group_id        INTEGER NOT NULL,           -- panel group id (unique within a panel)
    label           TEXT NOT NULL,
    sort_order      INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (channel_id, panel_id, group_id)
);
```

### Modified tables

```sql
-- Promo state: now per-channel (id is no longer constrained to 1)
CREATE TABLE promo_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    message_id      INTEGER NOT NULL,
    next_run_at     REAL NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Trial grants: add channel_id
ALTER TABLE trial_grants ADD COLUMN channel_id INTEGER REFERENCES channels(id);

-- Member events: already has chat_id, stays as-is
-- Chat members: already has chat_id, stays as-is
-- Settings: becomes per-channel (key becomes "channel:{id}:{key}")
```

### How multi-panel channels work

A channel's offer groups can come from **multiple panels**. Each offer group
in `channel_offer_groups` is linked to both a channel and a panel.

When a user selects an offer group in the `/start` flow:
- The bot shows all offer groups for the channel (from all linked panels),
  each with a friendly label (e.g. "🇳🇱 هلند", "🇹🇷 ترکیه").
- The user picks **one** group → that determines which panel is used.
- The bot creates **one trial** on that panel, scoped to the chosen group.
- Delivers **one subscription URL**.

The user never sees panel names or technical details — only friendly labels.
Panels are an implementation detail hidden behind the offer group labels.

### Removed (from .env)
- `CHANNEL_ID`, `PANEL_*`, `TRIAL_*`, `PROMO_*`, `OFFER_GROUPS_FILE`,
  `AUTO_DELETE_DAYS`, `ALLOW_REGRANT_AFTER_DAYS`, `JOIN_APPROVAL_DELAY_SECONDS`,
  `TRIAL_MAX_MEMBER_AGE_DAYS`
- `OWNER_TG_IDS` is replaced by the `users` table with roles

---

## 3. Role system

| Role | Manages | Can do |
|------|---------|--------|
| **superadmin** | Everything | Add/remove panels, add/remove channels, assign admins, run any command on any channel, `/sysstats` |
| **admin** | Assigned channels | Full control over their channels (promo, trials, groups, join-requests, pause/resume, reset, stats) |
| **user** | Nothing | Only interacts via /start and join-requests |

### Bootstrap
- On first run, `OWNER_TG_IDS` from .env are seeded as `superadmin` in the `users` table.
- After that, superadmins manage roles via bot commands. `OWNER_TG_IDS` is only used for bootstrapping.

### No subadmin role
- If someone needs to manage their own channels, they deploy their own bot instance.
- This keeps the permission model simple: superadmin sees everything, admin sees their assigned channels with full control.

---

## 4. New bot commands (slash commands — secondary interface)

> **Note:** The primary management UI is the **inline button panel** (`/panel`) described in section 6.
> Slash commands are kept as a secondary / power-user interface. They work the same way
> (channel context from chat or explicit channel ID in DM), but most users will prefer `/panel`.

### Superadmin commands (global)

| Command | What it does |
|---------|-------------|
| `/addpanel <name> <url> <user> <pass>` | Register a new PasarGuard panel |
| `/panels` | List all panels with id, name, url, status |
| `/editpanel <id> <field> <value>` | Change a panel field (url, password, ssl, timeout, protocols, auto_delete_days) |
| `/removepanel <id>` | Soft-delete a panel (only if no active channels reference it) |
| `/addchannel <tg_channel_id>` | Register a channel (panels are linked via offer groups) |
| `/channels` | List all channels with id, title, panel, status |
| `/editchannel <id> <field> <value>` | Change a channel field (trial settings, promo settings, etc.) |
| `/removechannel <id>` | Soft-delete a channel |
| `/assign <tg_user_id> <channel_id>` | Assign a user as admin of a channel |
| `/unassign <tg_user_id> <channel_id>` | Remove a user's channel assignment |
| `/promote <tg_user_id>` | Promote a user to admin role |
| `/demote <tg_user_id>` | Set a user's role back to 'user' |
| `/users` | List all users with their roles |
| `/sysstats` | System-wide stats (all channels, all panels) |

### Admin commands (channel-scoped)

These are sent **in the channel** (the bot infers context from `event.chat.id`)
or with `/cmd <channel_id> <args>` when sent in DM.

| Command | What it does |
|---------|-------------|
| `/pause` / `/resume` | Master switch for this channel |
| `/pausejoins` / `/resumejoins` | Join-request switch for this channel |
| `/setpromo <text>` | Set this channel's promo message |
| `/setinterval <hours>` | Set this channel's promo interval |
| `/promonow` | Publish promo to this channel now |
| `/getpromo` | Show this channel's current promo |
| `/setjoindelay <seconds>` | Set this channel's join-request delay |
| `/setmaxage <days>` | Set this channel's member-age gate |
| `/settrial <field> <value>` | Change trial settings (data_limit, days, grace, regrant) |
| `/groups` | List panel groups from all panels linked to this channel |
| `/offergroups` | Show this channel's offer list (with panel names for admin reference) |
| `/setoffer <panel_id> <group_id> <label>` | Add an offer option from a specific panel |
| `/deloffer <panel_id> <group_id>` | Remove an offer option |
| `/reorder <ids>` | Reorder offer groups |
| `/clearoffers` | Empty this channel's offer list |
| `/reset <tg_user_id>` | Revoke a user's trial in this channel |
| `/stats` | Channel-specific stats |
| `/joinstats` | Channel-specific join-request stats |

---

## 5. Multi-channel context resolution

When an admin sends a command, the bot needs to know **which channel** they're acting on:

1. **In a channel/group chat**: `event.chat.id` is the channel — use that.
2. **In DM (private chat)**: require `cmd <channel_id> <args>` syntax.
   Every DM command must include the channel ID explicitly.
   Example: `/setpromo -1001234567890 Hello everyone!`

Commands sent in the channel itself need no channel ID (inferred from `event.chat.id`).

---

## 6. Inline management panel

Slash commands are kept as a power-user / fallback interface, but the **primary management UI** is an inline-button panel opened via `/panel` (or a single `/p` shortcut). The bot edits the same message as the user navigates — no message spam.

### Entry point

| Who types `/panel` | What they see |
|--------------------|---------------|
| **Superadmin** | Main menu with 4 buttons: 🖥 Panels, 📺 Channels, 👥 Users, 📊 System Stats |
| **Admin** | Their assigned channels list (tap one → full channel management) |

### Superadmin menu tree

```
/panel
├─ 🖥 Panels
│  ├─ [Panel 1: NL Panel]  [Panel 2: TR Panel]  ...
│  ├─ ➕ Add Panel → FSM: enter name, url, user, pass
│  └─ [Tap panel]
│     ├─ 📋 Info (name, url, ssl, timeout, protocols, auto-delete)
│     ├─ ✏️ Edit → pick field → FSM: enter new value
│     ├─ 🔑 Change Password → FSM: enter new password
│     ├─ 🔄 Toggle SSL
│     └─ 🗑 Remove (confirmation: "Are you sure?" → Yes/No)
│
├─ 📺 Channels
│  ├─ [Channel 1: @mychannel]  [Channel 2: @other]  ...
│  ├─ ➕ Add Channel → FSM: enter channel ID (panels are linked later via offer groups)
│  └─ [Tap channel] → same as admin channel view (see below)
│
├─ 👥 Users
│  ├─ List: [user_id: @name — role]  ...
│  ├─ 🔍 Lookup by ID → FSM: enter user ID
│  └─ [Tap user]
│     ├─ ℹ️ Info (id, username, role, assigned channels)
│     ├─ 🔄 Change Role → inline: [superadmin] [admin] [user]
│     ├─ 🔗 Assign Channel → list of channels → tap to assign
│     ├─ ✂️ Unassign Channel → list of assigned channels → tap to remove
│     └─ ↩️ Back
│
└─ 📊 System Stats
   └─ (read-only: panels, channels, users, grants summary)
```

### Admin channel management (tapped from channel list)

```
📺 Channel: @mychannel (Panel: NL Panel)
├─ ⏸ Pause / ▶️ Resume
├─ 📢 Promo
│  ├─ 📄 View Text
│  ├─ ✏️ Set Text → FSM: enter new promo text
│  ├─ ⏱ Interval: 6h → ✏️ Change → FSM: enter hours
│  ├─ 📌 Pin: ON → toggle
│  ├─ 🔇 Silent: ON → toggle
│  └─ 📤 Post Now
│
├─ 🎁 Trials
│  ├─ 📋 Settings
│  │  ├─ Data Limit: 5 GB → ✏️ Change → FSM: enter GB
│  │  ├─ Days: 3 → ✏️ Change → FSM: enter days
│  │  ├─ Grace: 7d → ✏️ Change → FSM: enter days
│  │  ├─ Regrant Cooldown: 30d → ✏️ Change → FSM: enter days
│  │  └─ Max Member Age: off → ✏️ Change → FSM: enter days
│  ├─ 🔄 Reset User → FSM: enter user ID
│  └─ 📊 Stats
│
├─ 🔗 Join Requests
│  ├─ ⏸ Pause / ▶️ Resume
│  ├─ ⏱ Delay: 10s → ✏️ Change → FSM: enter seconds
│  └─ 📊 Join Stats
│
├─ 🌐 Offer Groups
│  ├─ 📋 View List (shows panel name per group)
│  ├─ ➕ Add → pick panel → pick group from that panel → enter label
│  ├─ 🗑 Remove → list with ❌ buttons → tap to remove
│  ├─ ↕️ Reorder → list with ▲/▼ buttons
│  └─ 🧹 Clear All (confirmation)
│
└─ ↩️ Back to Channels
```

### Text input flow (FSM)

When the user taps a button that requires text input (e.g. "✏️ Set Text"):

1. Bot edits the message: "📝 لطفاً متن پیام تبلیغاتی جدید را ارسال کنید:" + ❌ Cancel button.
2. Bot sets FSM state (e.g. `AdminPanel.waiting_promo_text`).
3. User sends text as a regular message.
4. Bot processes it, saves to DB, edits the confirmation: "✅ متن ذخیره شد." + ↩️ Back button.

Cancel at any time: the ❌ button clears the FSM state and goes back.

### Callback data encoding

Use `CallbackData` from aiogram for type-safe callbacks:

```python
class AdminCB(CallbackData, prefix="adm"):
    action: str      # "view", "edit", "toggle", "confirm", "back"
    target: str      # "panel", "channel", "user", "promo", "trial", "offer", "join"
    target_id: int = 0   # panel_id, channel_id, user_id, offer_group_id, etc.
    extra: str = ""      # field name, value, etc.
```

Examples:
- `adm:view:channel:5:` — view channel 5 management
- `adm:edit:promo:text:5` — edit promo text for channel 5
- `adm:toggle:promo:pin:5` — toggle pin for channel 5
- `adm:confirm:panel:del:3` — confirm delete panel 3
- `adm:back:main:0:` — back to main menu

### Slash commands (kept as secondary interface)

All existing `/commands` remain available for power users and scripting.
They are **not removed** — just not the primary way to manage things.
When a slash command is used, it works the same as before (channel context
inferred from chat or explicit channel ID in DM).

The `/panel` command is the new primary entry point.

---

## 7. Multi-panel client management

Currently there's one `PasarGuardApiClient` instance in `dp["panel"]`.
With multiple panels, we need a **panel manager**:

```python
class PanelManager:
    """Manages multiple PasarGuardApiClient instances, keyed by panel id."""

    def __init__(self):
        self._clients: dict[int, PasarGuardApiClient] = {}

    async def get_client(self, panel_id: int, db) -> PasarGuardApiClient:
        """Return (or lazily create) the client for a panel."""
        ...

    async def close_all(self):
        ...
```

- Clients are created lazily on first use and cached.
- When a panel is edited (password changed), the cached client is invalidated.
- When a panel is soft-deleted, its client is closed and removed from cache.

---

## 8. Multi-channel promo scheduler

Currently: one `while True` loop for one channel.
Target: one scheduler that handles all active channels.

```python
async def run_scheduler(bot: Bot, db, panel_manager) -> None:
    """Manage promo posting for all active channels."""
    # Keep a dict of {channel_id: next_run_at}
    # Each iteration: sleep until the soonest next_run, post to that channel,
    # recalculate its next_run.
    # Re-scan channels table periodically (e.g. every 60s) for new/removed channels.
```

Key changes:
- `promo_state` table stores one row per channel (not `id=1` anymore).
- Each channel has its own interval, pin, silent settings.
- Pausing a channel skips its promo posts.

---

## 9. Handler changes

### member_events.py
- Currently filters by `settings.channel_id` — remove this filter.
- Process `chat_member` events for **any** channel the bot is admin of.
- Look up if the channel exists in the `channels` table; if not, ignore.

### join_request.py
- Currently filters by `settings.channel_id` — look up the channel in DB instead.
- Need to resolve the channel's panel, trial settings, offer groups, etc. from DB.

### trial.py (/start flow)
- Currently uses global `settings` — need to resolve the channel from the deep-link.
- **Deep-link format**: `?start=join_<channel_id>`. The bot looks up that channel's offer groups.
- Without a channel id (plain `/start`), and only one active channel exists → use it automatically.
  Multiple active channels → ask the user to pick one (inline keyboard of channel names).
- **Single-select**: the user picks **one** offer group (not multi-select as before).
  That group determines which panel is used. The bot creates one trial on that panel.
- Offer groups from different panels are shown in the same list with friendly labels —
  the user doesn't need to know which panel is behind each option.

### admin.py
- Every command needs to resolve the target channel (from chat context or DM arg).
- Role check: verify the user has the right role for that channel.
- Need a `resolve_channel` helper used by all admin commands.

---

## 10. Migration from single-tenant

Since this is a major schema change, we need a careful migration path:

1. **Schema migration**: Add all new tables. For existing `trial_grants`, add `channel_id` column.
2. **Seed data**: Read the old `.env` values and create:
   - One panel entry from `PANEL_BASE_URL`, `PANEL_ADMIN_USERNAME`, etc.
   - One channel entry from `CHANNEL_ID` with all the trial/promo settings.
   - One `promo_state` entry migrated from the old `id=1` row.
   - All `OWNER_TG_IDS` seeded as `superadmin` in the `users` table.
   - Existing `offer_groups` migrated to `channel_offer_groups` for the seeded channel,
     with `panel_id` set to the seeded panel.
   - All existing `trial_grants` get `channel_id` set to the seeded channel.
3. **Config simplification**: `.env` keeps only `TELEGRAM_BOT_TOKEN`, `DB_PATH`,
   `LOG_LEVEL`, `DEFAULT_LANG`, `RATE_LIMIT_PER_MINUTE`, and `OWNER_TG_IDS` (for bootstrap only).
4. **Backward compatible**: If the DB has zero channels after migration, seed from `.env`
   automatically (first-run detection).

---

## 11. Testing strategy

- **Unit tests**: Role resolution, channel context resolution, panel manager lifecycle.
- **Integration tests**: Multi-channel promo scheduler (mock time), admin command
  authorization (superadmin vs admin), cross-channel isolation
  (admin of channel A can't affect channel B).
- **Migration tests**: Old DB → new DB (seeded correctly, old grants have channel_id).

---

## 12. Implementation order

| Phase | What | Risk |
|-------|------|------|
| **P1** | New DB schema + migration + `users`/`panels`/`channels`/`channel_admins`/`channel_offer_groups` tables | Low — additive, doesn't break existing code yet |
| **P2** | Panel manager + refactor trial service to accept channel context | Medium — touches core trial flow |
| **P3** | Role system + `resolve_channel` + admin command refactoring | Medium — authorization is critical to get right |
| **P4** | Multi-channel promo scheduler | Medium — concurrent scheduling |
| **P5** | Superadmin commands (`/addpanel`, `/addchannel`, `/assign`, etc.) | Low — new commands, no existing code touched |
| **P6** | Channel-scoped admin commands | Medium — many commands to update |
| **P7** | Multi-channel `/start` + join-request handlers | Medium — needs deep-link changes |
| **P8** | Inline management panel (`/panel` command, menu trees, FSM text inputs) | Medium — new UI layer, many callbacks |
| **P9** | Migration from old .env-based config to DB | Medium — must not lose existing data |
| **P10** | Tests for everything above | Low |

---

## 13. Decisions (locked)

| # | Question | Decision |
|---|----------|----------|
| 1 | Encrypt panel passwords in DB? | **Yes** — Fernet with key from `DB_ENCRYPTION_KEY` env var |
| 2 | Where to store per-channel settings? | **Hybrid** — small/numeric settings as `channels` table columns (promo_interval, trial_days, grace_days, etc.); large/text settings in `settings` table with `channel:{id}:{key}` prefix (promo_text, paused, joins_paused) |
| 3 | DM command context resolution? | **Always explicit** — every DM command must include channel ID: `/setpromo <channel_id> <text>`. Commands sent in the channel itself need no ID (inferred from `event.chat.id`). |
| 4 | Subadmin role? | **Removed** — only superadmin and admin roles. Others deploy their own bot instance. |
| 5 | Primary management UI? | **Inline button panel** via `/panel` command — navigable menu trees with FSM-based text inputs. Slash commands kept as secondary/power-user interface. |
| 6 | Channel-panel relationship? | **Many-to-many** — a channel can pull offer groups from multiple panels. Each offer group is linked to a specific panel. Trials are created on the correct panel based on the chosen group. |
