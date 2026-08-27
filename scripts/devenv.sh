#!/usr/bin/env bash
#
# devenv.sh — development environment helper for pasarguard-delivery-bot.
#
# Usage:
#   scripts/devenv.sh check    Verify the whole dev environment is green:
#                              python, venv, deps, .env, lint, format, tests+coverage.
#   scripts/devenv.sh setup    Create .venv and install all dependencies.
#   scripts/devenv.sh update   Reinstall dependencies (after pulling new commits).
#   scripts/devenv.sh test     Run the test suite with coverage only.
#   scripts/devenv.sh lint     Run ruff check + format check only.
#
# `check` exits non-zero if anything is red — safe to run in a fresh session
# (see SESSION_HANDOFF.md) or in CI.

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
COVERAGE_THRESHOLD="$(sed -n 's/.*--cov-fail-under=\([0-9]*\).*/\1/p' pyproject.toml | head -1)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
OFF='\033[0m'

RESULTS=()
FAILURES=0

step() { # step <name> <status: ok|warn|fail> [detail]
    local name="$1" status="$2" detail="${3:-}"
    case "$status" in
        ok)   printf "  ${GREEN}✓${OFF} %s\n" "$name" ;;
        warn) printf "  ${YELLOW}!${OFF} %s ${YELLOW}%s${OFF}\n" "$name" "$detail" ;;
        fail) printf "  ${RED}✗${OFF} %s ${RED}%s${OFF}\n" "$name" "$detail" ;;
    esac
    RESULTS+=("$name|$status")
    [ "$status" = fail ] && FAILURES=$((FAILURES + 1))
}

summary() {
    echo
    echo -e "${BOLD}══════════════ SUMMARY ══════════════${OFF}"
    for r in "${RESULTS[@]}"; do
        name="${r%%|*}"; status="${r##*|}"
        case "$status" in
            ok)   printf "  ${GREEN}✓${OFF} %s\n" "$name" ;;
            warn) printf "  ${YELLOW}!${OFF} %s\n" "$name" ;;
            fail) printf "  ${RED}✗${OFF} %s\n" "$name" ;;
        esac
    done
    echo -e "${BOLD}═════════════════════════════════════${OFF}"
    if [ "$FAILURES" -gt 0 ]; then
        printf "${RED}%d check(s) failed.${OFF}\n" "$FAILURES"
        return 1
    fi
    printf "${GREEN}All green — environment ready.${OFF}\n"
}

require_venv() {
    if [ ! -x "$PY" ]; then
        step "virtualenv (.venv)" fail "missing — run: scripts/devenv.sh setup"
        return 1
    fi
    return 0
}

cmd_setup() {
    echo -e "${BOLD}Setting up development environment…${OFF}"
    if [ ! -x "$PY" ]; then
        python3 -m venv "$VENV" || { echo "venv creation failed"; exit 1; }
    fi
    "$PIP" install -r requirements.txt -r requirements-dev.txt || exit 1
    echo
    step "virtualenv (.venv)" ok
    step "dependencies installed" ok
    summary
}

cmd_update() {
    echo -e "${BOLD}Updating dependencies…${OFF}"
    require_venv || summary || exit 1
    "$PIP" install -r requirements.txt -r requirements-dev.txt || exit 1
    step "dependencies updated" ok
    summary
}

cmd_lint() {
    echo -e "${BOLD}Lint & format (ruff)…${OFF}"
    require_venv || summary || exit 1
    if "$VENV/bin/ruff" check .; then
        step "ruff check" ok
    else
        step "ruff check" fail
    fi
    if "$VENV/bin/ruff" format --check .; then
        step "ruff format" ok
    else
        step "ruff format" fail
    fi
    summary
}

cmd_test() {
    echo -e "${BOLD}Tests + coverage (threshold: ${COVERAGE_THRESHOLD:-?}%)…${OFF}"
    require_venv || summary || exit 1
    if "$PY" -m pytest -q; then
        step "pytest (coverage ≥ ${COVERAGE_THRESHOLD:-?}%)" ok
    else
        step "pytest (coverage ≥ ${COVERAGE_THRESHOLD:-?}%)" fail
    fi
    summary
}

cmd_check() {
    echo -e "${BOLD}pasarguard-delivery-bot — environment check${OFF}"
    echo

    echo -e "${BOLD}[1/5] Python${OFF}"
    if command -v python3 >/dev/null 2>&1; then
        PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
            step "python3 >= 3.11 (found $PYV)" ok
        else
            step "python3 >= 3.11" fail "found $PYV"
        fi
    else
        step "python3" fail "not found"
    fi

    echo -e "${BOLD}[2/5] Virtualenv & dependencies${OFF}"
    if [ ! -x "$PY" ]; then
        step "virtualenv (.venv)" fail "missing — run: scripts/devenv.sh setup"
        summary; exit 1
    fi
    step "virtualenv (.venv)" ok

    # Idempotent install: refreshes anything missing, quiet when satisfied.
    if "$PIP" install -q -r requirements.txt -r requirements-dev.txt 2>/dev/null; then
        step "dependencies (requirements*.txt)" ok
    else
        step "dependencies (requirements*.txt)" fail "pip install failed"
    fi
    if "$PIP" check >/dev/null 2>&1; then
        step "pip check (no broken deps)" ok
    else
        step "pip check (no broken deps)" warn "inconsistent environment"
    fi

    echo -e "${BOLD}[3/5] Environment file${OFF}"
    if [ -f "$ROOT/.env" ]; then
        missing_keys=()
        while IFS='=' read -r key _; do
            case "$key" in ''|\#*) continue ;; esac
            grep -q "^${key}=" "$ROOT/.env" || missing_keys+=("$key")
        done < <(grep -v '^#' "$ROOT/.env.example" | grep -E '^[A-Z_]+=')
        if [ "${#missing_keys[@]}" -eq 0 ]; then
            step ".env present, all keys from .env.example" ok
        else
            step ".env present" warn "missing keys: ${missing_keys[*]}"
        fi
    else
        # Tests never need .env — only a bot run does.
        step ".env" warn "not found (only needed to RUN the bot: cp .env.example .env)"
    fi

    echo -e "${BOLD}[4/5] Lint & format${OFF}"
    if "$VENV/bin/ruff" check . >/dev/null 2>&1; then
        step "ruff check" ok
    else
        step "ruff check" fail "run: .venv/bin/ruff check ."
    fi
    if "$VENV/bin/ruff" format --check . >/dev/null 2>&1; then
        step "ruff format --check" ok
    else
        step "ruff format --check" fail "run: .venv/bin/ruff format ."
    fi

    echo -e "${BOLD}[5/5] Tests + coverage${OFF}"
    if "$PY" -m pytest -q >/tmp/devenv_pytest.log 2>&1; then
        cov="$(grep -oE 'TOTAL.*' /tmp/devenv_pytest.log | awk '{print $(NF)}' | tr -d '%')"
        step "pytest — coverage ${cov:-?}% (gate: ${COVERAGE_THRESHOLD:-?}%)" ok
    else
        cov="$(grep -oE 'TOTAL.*' /tmp/devenv_pytest.log | awk '{print $(NF)}' | tr -d '%')"
        if [ -n "${cov:-}" ] && [ "${cov}" -lt "${COVERAGE_THRESHOLD:-0}" ] 2>/dev/null; then
            step "pytest — coverage ${cov}% < gate ${COVERAGE_THRESHOLD}%" fail "add tests or lower the gate in pyproject.toml"
        else
            step "pytest" fail "see /tmp/devenv_pytest.log"
        fi
    fi
    echo
    tail -3 /tmp/devenv_pytest.log 2>/dev/null || true

    echo -e "${BOLD}[extra] Git safety policy${OFF}"
    if [ -x "$ROOT/scripts/git-safety.sh" ]; then
        if bash "$ROOT/scripts/git-safety.sh" check >/dev/null 2>&1; then
            step "git safety (config + hooks)" ok
        else
            step "git safety (config + hooks)" warn "run: scripts/git-safety.sh install"
        fi
    fi

    summary
}

case "${1:-check}" in
    check) cmd_check ;;
    setup) cmd_setup ;;
    update) cmd_update ;;
    lint)  cmd_lint ;;
    test)  cmd_test ;;
    *) echo "Usage: scripts/devenv.sh {check|setup|update|lint|test}"; exit 2 ;;
esac
