# Session Handoff — Channel-posts feature (v1); devenv gate, coverage, changelog, lint recovery; offer-del fix; git safety + rewind ritual

**Updated:** 2026-08-27 (Asia/Tehran) — latest slice: the **channel-posts feature (v1)** (`/newpost` wizard, `/posts` management, `/checkpremium`, 30 s posts scheduler; all six backlog ideas promoted into v1 by operator decision). Previously: the **`scripts/devenv.sh` gate** (check/setup/update/lint/test), the **coverage gate at 80%**, the **`CHANGELOG.md`**, the **session-handoff document** (this file, Puploader format), the **whole-repo lint recovery** (~200 drifted ruff findings), the **`offer_del` malformed-payload fix**, and the **git safety policy** (`scripts/git-safety.sh` config + hooks; no rebase/squash/force-push, session branch fast-forward only) with the **sandbox-rewind recovery ritual**:

- **Channel posts v1.1 — prod crash fix + albums/templates/media-swap
  (2026-08-27, latest):** operator reported
  `Bot.send_photo() got an unexpected keyword argument
  'link_preview_options'` from a media-post preview in production. Root
  cause: `build_send_kwargs` attached `link_preview_options` to ALL sends,
  but media methods (`send_photo/video/animation`) — and
  `edit_message_caption` — have no such parameter (verified against aiogram
  3.31 signatures). Fixed by passing it only on text paths
  (`send_message` / `edit_message_text`); the fake bots now assert the
  forbidden kwargs (mirroring real signatures) and the regression tests were
  demonstrated against the pre-fix code. Persian strings signed off by the
  operator. Then the three remaining backlog items landed:
  - **Albums (media groups)**: wizard accumulates media-group items
    (deduplicated by `file_unique_id`, caption from the message that carries
    one), stored as `channel_posts.media_json` (`media_type='album'`),
    sent via `sendMediaGroup` (caption+entities on the first item, NO
    keyboard — Telegram limitation, wizard skips button/layout steps for
    albums with a notice), all message ids stored in
    `tg_message_ids_json` so pin (first)/delete-previous/ephemeral-expiry/
    delete cover the whole group; premium-emoji fallback + in-place caption
    edit (`edit_message_caption` on the first id) both work.
  - **Template picker**: «📚 قالب‌ها» in the content step (PostsCB actions
    `tpllist`/`tpl`/`tpldel`/`contentmenu`) — lists saved templates, loads
    content+buttons+opts into the wizard, deletes; album templates load back
    as accumulatable `media_items`.
  - **Media swap**: «🔄 تعویض رسانه» (pact action `swapmedia`, state
    `edit_media`): a single new photo/video/animation replaces the media;
    published posts → old message(s) deleted + new one sent in place WITHOUT
    advancing a recurring schedule; scheduled posts → fields updated only.
  - Storage: additive idempotent migrations `channel_posts.media_json`,
    `channel_posts.tg_message_ids_json`, `post_templates.media_json`.
  - Tests: 492 passed (was 467), coverage 83% (gate 80%).

- **Channel posts v1 (2026-08-27):** design doc first
  (`docs/channel-posts-plan.md`, committed as DRAFT, then updated to
  IMPLEMENTED after the operator promoted all six backlog ideas — ephemeral
  posts, edit-published-in-place, recurring schedules, templates,
  multi-channel send, copy-last-post — into v1). Landed:
  - `storage/db.py` — `channel_posts` + `post_templates` tables,
    `channels.post_delete_previous` (additive `ALTER TABLE` migration),
    full CRUD incl. due/recurring/expired scans and `create_channel`/
    `update_channel` support for the new field.
  - `services/posts.py` — button model → keyboard (native styles, URL /
    disabled / copy actions, premium-emoji icons with UTF-16-safe label
    extraction), entities round-trip (no parse_mode, `LinkPreviewOptions`),
    `send_post` (delete-previous → send → premium-fallback retry → pin →
    expiry stamp), `edit_published_post`, Tehran (+03:30, no DST)
    `next_occurrence` drift-free recurrence, `dispatch_due_posts` one-scan
    tick (due one-shot + due recurring + expired ephemeral),
    `run_posts_scheduler`, `send_and_record`, `send_preview`.
  - `bot/handlers/posts.py` — full FSM wizard (channel multi-picker with
    access guards → content → button loop → layout → options → schedule →
    preview → confirm), `/posts` management (send now / cancel / reschedule
    / edit published / copy as new / delete), `/checkpremium` echo
    diagnostic, PostsCB callbacks; every pact/plist/pview callback
    re-checks channel access.
  - `bot/main.py` — posts router + dedicated posts-scheduler task
    (cancelled on shutdown; lifecycle test extended).
  - `bot/handlers/panel.py` — «📝 پست‌ها» channel-menu entry → posts view
    (with back button); `bot/handlers/admin.py` — `/editchannel` gains
    `post_delete_previous`; `requirements.txt` — aiogram floor
    `>=3.31,<4` (needed for button `style`/`icon_custom_emoji_id`,
    `DisabledButton`, `CopyTextButton`; verified on 3.31.0).
  - Tests: `tests/test_posts_service.py` (34) +
    `tests/test_posts_handlers.py` (44); suite 389 → 467 passed, coverage
    82% (gate 80%). The handler suite caught a real bug pre-merge
    (`on_confirm` read `scheduled_at` while the wizard stores `sched_at` —
    scheduled confirms silently created nothing).
  - **Persian strings in the new handlers are pending operator sign-off**
    (standing rule).

- **Git safety policy + sandbox-rewind ritual (2026-08-27, new):** the sandbox
  reset local history to the pre-session base mid-session — HEAD fell back to
  `8e02a8d`, `.venv/` and `.git/config` were wiped, the working tree survived
  (exactly the Puploader failure mode). Recovery: `git fetch origin <session
  branch>` → `git reset --soft FETCH_HEAD && git reset` → rebuild `.venv` →
  re-run the full gate (the tree is identical, but prove it) → re-apply the
  git-safety config + hooks. The policy itself is now versioned as
  `scripts/git-safety.sh` (`install` / `check`; local `push.force=false`,
  `pull.rebase=false`, `rebase.autoSquash=false`, `fetch.prune=false` +
  executable `.git/hooks/pre-rebase` rejecting rebase and `.git/hooks/
  pre-push` rejecting non-fast-forward pushes and branch deletions — verified
  against a synthetic non-ff push). `devenv.sh check` reports git-safety
  state as a warn line so a rewind is caught on the next check. NOTE: one
  force-push happened earlier this session (commit-message fix) before this
  policy was imported — do not repeat it; amend-before-push only, or a
  follow-up commit. No migration; nothing to rebuild.

- **Regression demonstration for the offer-del fix (2026-08-27, retroactive):**
  `tests/test_panel_ui.py::test_confirm_offer_del_invalid_extra_just_acks`
  was re-run against the pre-fix `bot/handlers/panel.py` (checked out from
  `8e02a8d`): **FAILED** with the uncaught `ValueError: invalid literal for
  int() with base 10: 'not'`; against the fixed code it passes. This is the
  Puploader discipline — every behavior change gets a regression test
  demonstrated against the old implementation.

- **`scripts/devenv.sh` + coverage gate + CHANGELOG + handoff (2026-08-27):**
  `check` verifies a fresh session in one command — python ≥ 3.11, `.venv`,
  dependencies (idempotent install), `pip check`, `.env` (**warn-only**:
  tests never need it, only a bot run does), `ruff check`, `ruff format
  --check`, and `pytest` with the coverage gate. Coverage is wired into
  `pyproject.toml` `addopts` (`--cov --cov-report=term-missing
  --cov-fail-under=80`), so even a bare `pytest` cannot dodge the gate;
  `pytest-cov` added to `requirements-dev.txt`. `CHANGELOG.md` (Keep a
  Changelog; git history was squashed pre-publish, so the changelog is the
  real history) seeded with the `0.1.0` multi-tenant release notes.
  Subcommands: `setup` / `update` / `lint` / `test`. Exit codes verified:
  lint failure → 1, clean → 0, bad subcommand → 2. No migration; nothing to
  rebuild (dev tooling only).

- **Coverage 68% → ~85% + lint recovery + offer-del fix (2026-08-27):** 89 new
  tests — dispatcher lifecycle/startup-shutdown/`on_error`/title-refresh
  (`bot/main.py`, was 0%), the panel UI view/back/toggle/confirm callbacks
  including backup actions (`bot/handlers/panel.py` 46% → ~75%), all backup
  commands `/backup` `/restore` `/export` `/import` with validation, download
  failures and upsert semantics (`bot/handlers/backup.py` 47% → ~90%), the
  trial toggle/cancel/confirm callbacks and `/start` edge cases
  (`bot/handlers/trial.py` 50% → ~85%), and `logging_setup` (0% → 100%).
  ~200 drifted ruff findings fixed across the repo (unused imports/variables,
  line length, multi-statement lines, import order; `ruff format` applied
  everywhere including the Python fences in `MULTI_TENANT_PLAN.md`) —
  `ruff check .` and `ruff format --check .` are green again and enforced by
  the gate. **Bug fixed:** the offer-group delete callback
  (`PanelCB action=confirm target=offer_del`) crashed with an uncaught
  `ValueError` when `extra` was a malformed pair (e.g. a stale button from an
  edited message); it now treats unparsable payloads as a no-op ack.
  Dead code removed around panel deletion / group listing in
  `bot/handlers/admin.py`. No migration; `docker compose up -d --build` to
  deploy the fix (bot image only).

---

## Read first

1. [`README.md`](README.md) — product overview, setup, commands, development
2. [`CHANGELOG.md` → Unreleased](CHANGELOG.md) — every slice with root causes
3. [`PLAN.md`](PLAN.md) / [`MULTI_TENANT_PLAN.md`](MULTI_TENANT_PLAN.md) —
   original design docs (historical; README wins on conflict)
4. `scripts/devenv.sh` + `scripts/git-safety.sh` — the gate and the git policy

## Session work

Session 4 (channel posts v1) — plan doc (`docs/channel-posts-plan.md`,
commit `38bb90e`) followed by the implementation commit (see `git log
origin/arena/01a042b5-pasarguard-delivery-bot`). Earlier sessions:

```
265e329 style: reformat python code blocks in MULTI_TENANT_PLAN.md
b2668cc docs: add session handoff, changelog, devenv.sh check; update CI template
effc723 test: raise coverage 68%→85% (300→389 tests), add pytest-cov gate at 80%
```

DB migrations: **additive, automatic** — `channels.post_delete_previous` +
`channel_posts` + `post_templates` are created at boot by `storage/db.py`
(`_migrate` is idempotent; existing databases gain the column via a guarded
`ALTER TABLE`, nothing to run by hand).

## Git safety policy (enforced, do not disable)

Rebasing, squashing, and force-pushing are **never** acceptable in this
repository. Push only to the session branch
(`arena/01a042b5-pasarguard-delivery-bot` for this checkout), always
fast-forward. The policy is applied by `scripts/git-safety.sh install`
(local config + `pre-rebase` / `pre-push` hooks); re-run it after any sandbox
rewind.

### Sandbox-rewind recovery ritual

The sandbox periodically resets local history to an old base and wipes
`.venv/` and `.git/config` while preserving the working tree (it happened
once this session). **Never `git reset --hard`.** On each rewind:

```bash
git fetch origin arena/01a042b5-pasarguard-delivery-bot
git reset --soft FETCH_HEAD -q && git reset -q
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
scripts/git-safety.sh install
bash scripts/devenv.sh check      # re-run the full gate before re-committing
```

Then `git status --short` should show only the current turn's work. Check
`git log --oneline -1` + `git status` + `.venv` existence at the start of
every working turn.

## Gate

Run `scripts/devenv.sh check` (ruff check + ruff format --check + pytest with
the coverage gate). Prefer it over raw `pytest` — it also catches a missing
venv, broken deps, and an uninstalled git-safety policy. Latest full gate
(after the git-safety slice, post-rewind recovery):

```text
scripts/devenv.sh check
✓ python3 >= 3.11   ✓ .venv   ✓ deps   ✓ pip check   ! .env (warn-only)
✓ ruff check        ✓ ruff format --check
✓ pytest — coverage 83% (gate: 80%)   → 492 passed
✓ git safety (config + hooks)
```

## Known limitations / open loops

- **CI is intentionally not used** (operator decision). The gate
  (`scripts/devenv.sh check`) runs locally before every commit instead.
- **Coverage gaps** (biggest remaining): `bot/handlers/posts.py` menu-only
  wizard branches (e.g. `on_option_toggle` between-steps), `bot/handlers/
  panel.py` FSM wizard inputs, `bot/handlers/admin.py` error branches,
  `bot/smoke.py` (0% — diagnostic script; counted honestly, not excluded).
- **Gate ratchet:** raise `--cov-fail-under` from 80 toward 85 once stable.
- **Channel-posts follow-ups:** keep-last-N retention per channel; `pay` /
  Stars buttons; album→single-media conversion UX (swap covers single media).
- Persian strings are user-facing; do not reword them without operator
  sign-off — the posts handlers' Persian was signed off 2026-08-27
  ("fine for now; change later if needed").
- `bot/smoke.py` needs a real panel; it is the only intentionally untested
  runtime path.

## Test-suite quirks (read before touching tests)

- **Routers are module-level singletons** — `build_dispatcher()` can run only
  once per process (attaching the same routers to a second `Dispatcher`
  raises `RuntimeError`). `tests/test_main.py` therefore exercises wiring,
  startup and shutdown in ONE lifecycle test.
- Startup tests must patch `aiogram.client.bot.Bot.get_me` / `get_chat`
  (`fake_telegram` fixture) — otherwise the real Bot makes a network call to
  Telegram and the test burns ~90 s on DNS timeouts.
- `aiogram`'s `BufferedInputFile` exposes its bytes as `.data` (there is no
  sync `.read()`).
- The fake army lives in `tests/helpers.py`: `FakeBot`, `FakeBotWithDM`,
  `FakeFileBot` (file downloads + `get_chat`), `FakeMessage`,
  `FakeEditableMessage` (edit_text/edit_reply_markup/answer/answer_document),
  `FakeCallback`, `FakePanel`, `FakePanelManager`, `FakeState`,
  `make_settings()`. Extend these before inventing new fakes.
- DB-backed tests use a real SQLite connection on `tmp_path` per test
  (`store.connect(tmp_path / "test.db")`); restore tests close the connection
  themselves, so their fixture tolerates a double close.
- Single-test runs report the coverage gate as failed (one test can't reach
  80%) — that is the gate working; use `-p no:cacheprovider` and read only
  pass/fail for the test itself.

## Repository discipline

Keep changes surgical; update `README.md` when behavior is operator-facing
and add a root-cause entry under `CHANGELOG.md` → Unreleased before every
commit. `git diff --check` must be clean. Every behavioral change needs a
regression test demonstrated against the old implementation (check out the
old file → prove the failure → restore). Run the gate
(`scripts/devenv.sh check`), never assume it is green. The coverage gate is
a floor, not a ceiling — new code lands with tests.

---

## Current state

| Thing | State |
|---|---|
| Tests | **492 passed**, coverage **83%** (gate: 80%) |
| Lint | `ruff check .` + `ruff format --check .` — green |
| CI | Intentionally not used — local gate before every commit |
| Deployment | Docker / docker-compose (bot only, SQLite on a volume) |
| Last release | `0.1.0` (see `CHANGELOG.md`) |

### Session history (condensed)

- **Session 4 — 2026-08-27 — channel posts v1** (top entry above; plan doc
  committed first, then implemented layer by layer: storage → service →
  scheduler wiring → handlers → docs).
- **Session 3 — 2026-08-27 — docs/tooling/coverage + git safety** (entries
  below).
- **Session 2 — 2026-08-27 — multi-tenant rework (PR #2):** per-channel
  settings in SQLite, roles, `/panel` UI, per-channel promo + pauses, offer
  groups, panel manager, encrypted panel passwords, backup/restore/export/
  import. See `MULTI_TENANT_PLAN.md`, `CHANGELOG.md` → 0.1.0.
- **Session 1 — pre-2026-08-27 — initial build:** panel client, promo
  scheduler, trial flow (M1–M5), admin commands, smoke checker, Docker. See
  `PLAN.md`.

## Key files map

```
bot/main.py            entrypoint: dispatcher, startup/shutdown, error handler,
                       promo + posts scheduler tasks
bot/handlers/          admin (slash commands), panel (/panel UI), posts
                       (/newpost, /posts, /checkpremium), trial (/start),
                       join_request, member_events, backup
services/posts.py      channel-posts logic + 30 s scheduler
services/trial.py      eligibility, offered-group validation, trial creation
panel/                 PasarGuard API client + multi-panel manager
storage/db.py          SQLite schema + all CRUD
scripts/devenv.sh      the gate (check/setup/update/lint/test)
scripts/git-safety.sh  git policy (install/check)
```
