# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- CI workflow template (`docs/github-ci.yml`) — CI is intentionally not used.
  The local gate (`scripts/devenv.sh check`) runs before every commit and
  enforces the same checks (lint, format, tests, coverage).

### Fixed

- **Media posts crashed on send/preview** (production, 2026-08-27):
  `Bot.send_photo() got an unexpected keyword argument 'link_preview_options'`
  — the send-kwargs builder attached `link_preview_options` to media methods
  that don't accept it (send_photo/video/animation). The same latent bug
  existed in published-post editing (`edit_message_caption` also rejects it).
  `link_preview_options` is now only passed to text methods
  (`send_message` / `edit_message_text`); the test fakes now mirror the real
  signatures so any regression fails loudly. Regression tests demonstrated
  against the pre-fix code (both failed before, pass after).

### Added

- **Channel posts v1.1 slice:**
  - **Media groups (albums)** — send 2–10 photos/videos as one Telegram
    album; items accumulate in the wizard (deduplicated), sent via
    `sendMediaGroup` with the caption on the first item. All message ids are
    tracked so pin / delete-previous / ephemeral expiry / deletion cover the
    whole album. Albums cannot have buttons (Telegram limitation) — the
    wizard skips the button steps with a notice. Premium-emoji fallback and
    in-place caption editing both work for albums.
  - **Template picker** — «📚 قالب‌ها» in the wizard's content step lists
    saved templates with load/delete buttons; loading preloads content,
    buttons and options (all still editable).
  - **Media swap** — «🔄 تعویض رسانه» on any active post: send a new
    photo/video/animation; published posts get the old message deleted and
    the new one sent in place (recurring schedules are NOT advanced);
    scheduled posts just update the stored fields.
  - Storage: `channel_posts.media_json` + `tg_message_ids_json` and
    `post_templates.media_json` columns (additive, idempotent migration).

- **Channel posts feature (v1)** — `/newpost` wizard, `/posts` management,
  `/checkpremium` diagnostic, and a 30-second posts scheduler:
  - `/newpost [channel_tg_id]` — guided wizard: multi-channel picker →
    content (text / photo / video / animation / forward, entities incl.
    premium emojis preserved verbatim) → button builder (URL / no-op /
    copy-to-clipboard, native colors green/red/blue, premium-emoji button
    icons) → layout (`2,1` row spec) → options (delete-previous, pin,
    silent, link preview, ephemeral 1/6/12/24h) → schedule (immediate /
    one-shot / daily / weekly Tehran time, save-as-template toggle) → live
    preview → confirm; multi-channel fan-out shares a `group_id`.
  - `/posts [channel_tg_id]` + «📝 پست‌ها» in the `/panel` channel menu —
    list posts, send now, cancel, reschedule, edit published text in place,
    copy-as-new, delete.
  - `/checkpremium` — echo diagnostic for premium-emoji sendability
    (Fragment username requirement).
  - Premium-emoji fallback: on Telegram rejection the post is resent with
    custom-emoji entities/icons stripped and the admin warned once in Persian.
  - Recurring posts are drift-free (`next_occurrence` from the actual send
    time); ephemeral posts auto-delete on the scheduler tick and expire.
  - Storage: `channel_posts` + `post_templates` tables,
    `channels.post_delete_previous` column (additive migration),
    `/editchannel … post_delete_previous 1|0`.
  - Persian strings in the new handlers are pending operator sign-off.
- `docs/channel-posts-plan.md` — design for the above (status: IMPLEMENTED;
  the six backlog ideas — ephemeral, edit-in-place, recurring, templates,
  multi-channel, copy-last-post — were promoted into v1 by operator decision).

- `scripts/devenv.sh` — development-environment helper with a `check` subcommand
  that verifies the whole environment is green in one shot (python version,
  venv, dependencies, `pip check`, `.env`, ruff lint + format, tests with the
  coverage gate), plus `setup` / `update` / `lint` / `test` subcommands.
- `scripts/git-safety.sh` — enforced git policy: no rebase/squash/force-push,
  session branch fast-forward only (local config + `pre-rebase`/`pre-push`
  hooks; re-runnable after sandbox rewinds). `devenv.sh check` reports its
  state.
- Coverage gate: `pytest-cov` wired into `pyproject.toml` with
  `--cov-fail-under=80` — the suite now fails if coverage drops below the
  threshold (`scripts/devenv.sh test` shows the number).
- `SESSION_HANDOFF.md` — session handoff document (Puploader format) for
  picking up work in a new session without re-discovering context, including
  the sandbox-rewind recovery ritual and the git safety policy.

### Changed

- `requirements.txt`: aiogram floor raised `>=3.13,<4` → `>=3.31,<4` — the
  channel-posts feature needs `InlineKeyboardButton.style` /
  `icon_custom_emoji_id`, `DisabledButton`, `CopyTextButton` and
  `LinkPreviewOptions` (verified on aiogram 3.31.0).
- `bot/main.py`: a dedicated posts-scheduler background task
  (`run_posts_scheduler`, 30 s tick) alongside the promo scheduler; both are
  cancelled on shutdown.

- **Test coverage raised from 68% to ~85%** (300 → 389 tests). New coverage:
  - `bot/main.py` — dispatcher wiring, startup/shutdown lifecycle, error
    handler, channel-title refresh (previously 0%).
  - `bot/handlers/panel.py` — all inline-panel view/back/confirm/toggle
    callbacks (46% → ~75%).
  - `bot/handlers/backup.py` — `/backup`, `/restore`, `/export`, `/import`
    happy paths and error paths, SQLite validation, upsert semantics (47% → ~90%).
  - `bot/handlers/trial.py` — group toggle/cancel/confirm callbacks and
    `/start` edge cases (50% → ~85%).
  - `bot/logging_setup.py` — logging configuration (0% → 100%).
- Whole repo brought back under lint: ~200 ruff findings fixed (unused imports,
  line length, multi-statement lines, unsorted imports) and all files
  reformatted. `ruff check .` and `ruff format --check .` are green again.

### Fixed

- `bot/handlers/panel.py`: the offer-group delete callback crashed with an
  uncaught `ValueError` when the callback payload was malformed (e.g. stale
  button from an older message). It now treats unparsable payloads as a no-op.
- `bot/handlers/admin.py`: dead code removed around panel deletion and group
  listing (unused variables and a pointless query).

## [0.1.0] — 2026-08-27

Initial release: multi-tenant Telegram bot that delivers free trial xray
configs from PasarGuard panels (single-tenant → multi-tenant rework merged via
PR #2).

### Features

- **Multi-panel, multi-channel**: manage any number of PasarGuard panels and
  Telegram channels from one bot instance; all runtime config lives in SQLite
  (panel passwords Fernet-encrypted).
- **Per-channel promo scheduler**: pinned, silent promo post on a per-channel
  interval; owner-editable text; `/promonow` for immediate posting.
- **Trial delivery**: `/start` deep-link → curated server/location picker with
  friendly labels → on-hold trial account created on the matching panel →
  subscription URL delivered in DM; one trial per user with re-grant cooldown.
- **Join-request flow**: trial delivered via DM first, channel approval after a
  configurable delay; users are never blocked (approve-first on any error).
- **Role-based access**: superadmins manage panels/users/everything; admins
  manage their assigned channels; role commands (`/promote`, `/demote`,
  `/assign`, `/unassign`).
- **Inline management panel** (`/panel`): button UI for channels, promo, trial
  settings, join requests, offer groups, panels, stats, backup.
- **Two independent pause switches** per channel: promo+trials vs join-request
  approvals.
- **Backup & restore**: `/backup` (SQLite file), `/restore` (replace DB +
  restart), `/export` / `/import` (portable JSON config).
- **Ops**: rate-limit middleware, HTML-escaping of user input,
  `python -m bot.smoke` pre-flight checker, Docker + docker-compose, 300-test
  pytest suite (pytest-asyncio + respx), ruff lint/format.
