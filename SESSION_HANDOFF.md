# Session Handoff — Docs & tooling: devenv gate, coverage 68→85%, changelog, lint recovery; offer-del payload fix; git safety policy + rewind ritual

**Updated:** 2026-08-27 (Asia/Tehran) — this checkout adds the **`scripts/devenv.sh` gate** (check/setup/update/lint/test), the **coverage gate at 80%** with the coverage raised **68% → ~85%** (300 → 389 tests), the **`CHANGELOG.md`**, the **session-handoff document** (this file, Puploader format), the **whole-repo lint recovery** (~200 drifted ruff findings fixed — lint had silently rotted because CI was never enabled), the **`offer_del` malformed-payload fix** (with the regression test demonstrated against the old code), and the **git safety policy** (`scripts/git-safety.sh` config + hooks; no rebase/squash/force-push, session branch fast-forward only) together with the **sandbox-rewind recovery ritual** (a rewind happened live during this session and was recovered with it):

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

## Session work (published commits through `265e329`)

```
265e329 style: reformat python code blocks in MULTI_TENANT_PLAN.md
b2668cc docs: add session handoff, changelog, devenv.sh check; update CI template
effc723 test: raise coverage 68%→85% (300→389 tests), add pytest-cov gate at 80%
```

DB migrations: none (SQLite schema is created at boot by `storage/db.py`;
`bot/migration.py` only seeds from `.env` on first run).

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
✓ pytest — coverage 85% (gate: 80%)   → 389 passed, 84.73%
✓ git safety (config + hooks)
```

## Known limitations / open loops

- **CI is intentionally not used** (operator decision). The gate
  (`scripts/devenv.sh check`) runs locally before every commit instead.
- **Coverage gaps** (biggest remaining): `bot/handlers/panel.py` FSM wizard
  inputs, `bot/handlers/admin.py` error branches (~75%), `bot/smoke.py` (0% —
  diagnostic script; counted honestly, not excluded).
- **Gate ratchet:** raise `--cov-fail-under` from 80 toward 85 once stable.
- Persian strings are user-facing; do not reword them without operator
  sign-off.
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
| Tests | **389 passed**, coverage **~85%** (gate: 80%) |
| Lint | `ruff check .` + `ruff format --check .` — green |
| CI | Intentionally not used — local gate before every commit |
| Deployment | Docker / docker-compose (bot only, SQLite on a volume) |
| Last release | `0.1.0` (see `CHANGELOG.md`) |

### Session history (condensed)

- **Session 3 — 2026-08-27 — docs/tooling/coverage + git safety** (this
  handoff's entries above).
- **Session 2 — 2026-08-27 — multi-tenant rework (PR #2):** per-channel
  settings in SQLite, roles, `/panel` UI, per-channel promo + pauses, offer
  groups, panel manager, encrypted panel passwords, backup/restore/export/
  import. See `MULTI_TENANT_PLAN.md`, `CHANGELOG.md` → 0.1.0.
- **Session 1 — pre-2026-08-27 — initial build:** panel client, promo
  scheduler, trial flow (M1–M5), admin commands, smoke checker, Docker. See
  `PLAN.md`.

## Key files map

```
bot/main.py            entrypoint: dispatcher, startup/shutdown, error handler
bot/handlers/          admin (slash commands), panel (/panel UI), trial (/start),
                       join_request, member_events, backup
services/trial.py      eligibility, offered-group validation, trial creation
panel/                 PasarGuard API client + multi-panel manager
storage/db.py          SQLite schema + all CRUD
scripts/devenv.sh      the gate (check/setup/update/lint/test)
scripts/git-safety.sh  git policy (install/check)
```
