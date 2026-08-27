# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `scripts/devenv.sh` — development-environment helper with a `check` subcommand
  that verifies the whole environment is green in one shot (python version,
  venv, dependencies, `pip check`, `.env`, ruff lint + format, tests with the
  coverage gate), plus `setup` / `update` / `lint` / `test` subcommands.
- Coverage gate: `pytest-cov` wired into `pyproject.toml` with
  `--cov-fail-under=80` — the suite now fails if coverage drops below the
  threshold (`scripts/devenv.sh test` shows the number).
- `SESSION_HANDOFF.md` — session handoff document for picking up work in a new
  session without re-discovering context.

### Changed

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
