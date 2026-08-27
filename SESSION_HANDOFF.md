# Session Handoff — pasarguard-delivery-bot

> **Purpose:** pick up work in a new session (human or AI) without
> re-discovering context. **Update this file at the end of every working
> session** — keep the newest session at the top, prune sessions older than
> three entries.

---

## How to start a new session

```bash
git pull
scripts/devenv.sh check      # everything green? then you're ready to work
```

`check` verifies: python ≥ 3.11, `.venv`, dependencies, `pip check`, `.env`
(warn-only — tests don't need it), `ruff check`, `ruff format --check`, and
`pytest` with the coverage gate (**≥ 80%**, configured in `pyproject.toml` →
`addopts`). If the venv is missing: `scripts/devenv.sh setup`.

**Always work on a branch, never directly on `main`.** Open a PR and let CI
(`docs/github-ci.yml` → `.github/workflows/ci.yml` when enabled) confirm green
before merging.

---

## Current state (updated 2026-08-27)

| Thing | State |
|---|---|
| Tests | **389 passed**, coverage **~85%** (gate: 80%) |
| Lint | `ruff check .` + `ruff format --check .` — green |
| CI | Template exists at `docs/github-ci.yml`; **not yet enabled** in `.github/workflows/` |
| Deployment | Docker / docker-compose (bot only, SQLite on a volume) |
| Last release | `0.1.0` (see `CHANGELOG.md`) |

### Session log

#### Session 3 — 2026-08-27 — Docs, tooling & test coverage

**Goal:** documentation and handoff infrastructure.

**Done:**

- `scripts/devenv.sh` — `check` / `setup` / `update` / `lint` / `test`.
- `CHANGELOG.md` (Keep-a-Changelog format, seeded with 0.1.0).
- `SESSION_HANDOFF.md` (this file).
- Coverage gate `--cov-fail-under=80` in `pyproject.toml`; `pytest-cov` added
  to `requirements-dev.txt`.
- Coverage 68% → ~85% (300 → 389 tests): new tests for `bot/main.py`
  (dispatcher lifecycle, on_error, title refresh), panel UI callbacks
  (view/back/toggle/confirm incl. backup actions), backup commands
  (/backup //restore //export //import incl. validation + upserts), trial
  confirm/toggle/cancel callbacks, logging setup.
- Fixed ~200 ruff findings across the repo (lint had drifted — CI was never
  enabled); all files reformatted.
- **Bug fixed:** `bot/handlers/panel.py` offer-delete callback crashed with
  `ValueError` on malformed callback payloads (now a no-op). Found by the new
  tests.

**Decisions:**

- Coverage gate at 80 (actual ~85) — headroom to add code without immediate
  gate failures; ratchet upward over time.
- `.env` is warn-only in `devenv.sh check` — the test suite never needs it.
- `bot/smoke.py` stays counted in coverage even though it's 0% (diagnostic
  script; excluding it would inflate the number dishonestly).

**Next steps (suggested):**

1. Enable CI: `mkdir -p .github/workflows && cp docs/github-ci.yml
   .github/workflows/ci.yml` (needs a push token with `workflows` scope).
2. Push coverage toward 90% — biggest remaining gaps: `bot/handlers/panel.py`
   FSM wizard inputs, `bot/handlers/admin.py` error branches (~75%).
3. Consider `coverage` branch measurement (`[tool.coverage.run] branch = true`).
4. Ratchet the gate: raise `--cov-fail-under` to 85 once stable.

#### Session 2 — 2026-08-27 — Multi-tenant rework (PR #2)

Single-channel → multi-panel/multi-channel architecture: per-channel settings
in SQLite, role-based access (superadmin/admin), inline `/panel` UI,
per-channel promo scheduler + pause switches, offer groups per channel,
panel manager with lazy client cache, Fernet-encrypted panel passwords,
`/backup` `/restore` `/export` `/import`. See `MULTI_TENANT_PLAN.md` and
`CHANGELOG.md` → 0.1.0.

#### Session 1 — pre-2026-08-27 — Initial build

Single-channel bot per `PLAN.md`: panel client, promo scheduler, trial flow
(M1–M5), admin commands, smoke checker, Docker.

---

## Gotchas & tribal knowledge

- **Routers are module-level singletons** — `build_dispatcher()` can run only
  once per process (attaching the same routers to a second `Dispatcher`
  raises). See `tests/test_main.py` for the single-lifecycle-test pattern.
- `tests/helpers.py` has the fake army: `FakeBot`, `FakeMessage`,
  `FakeCallback`, `FakeEditableMessage`, `FakeFileBot`, `FakePanel`,
  `FakePanelManager`, `FakeState`, `make_settings()`. Prefer extending these
  over inventing new fakes.
- `aiogram` `BufferedInputFile` exposes bytes as `.data` (not a sync `.read()`).
- Git history was squashed to a single commit before the repo went public —
  `CHANGELOG.md` is the real history, not `git log`.
- Persian strings are everywhere (user-facing bot language). Don't "fix" them.
- Callback payloads (`PanelCB`, `GroupCB`) are the wire format of inline
  buttons — old messages in Telegram can carry stale payloads forever; every
  new callback handler must tolerate garbage input (see the offer-del fix).

## Key files map

```
bot/main.py            entrypoint: dispatcher, startup/shutdown, error handler
bot/handlers/          admin (slash commands), panel (/panel UI), trial (/start),
                       join_request, member_events, backup
services/trial.py      eligibility, offered-group validation, trial creation
panel/                 PasarGuard API client + multi-panel manager
storage/db.py          SQLite schema + all CRUD
scripts/devenv.sh      environment check / setup
docs/github-ci.yml     CI template (copy to .github/workflows/ to enable)
PLAN.md / MULTI_TENANT_PLAN.md   original design docs
```
