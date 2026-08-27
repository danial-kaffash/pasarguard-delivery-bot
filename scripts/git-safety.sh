#!/usr/bin/env bash
#
# git-safety.sh — enforce the repository's git safety policy.
#
# Policy (do not disable):
#   - Rebasing, squashing, and force-pushing are NEVER acceptable here.
#   - Push only to the session branch, always fast-forward.
#   - No branch deletions pushed.
#
# The sandbox periodically rewinds .git/config and hooks — re-run
# `scripts/git-safety.sh install` after any rewind (the recovery ritual in
# SESSION_HANDOFF.md includes it).
#
# Usage:
#   scripts/git-safety.sh install   Apply local config + install hooks.
#   scripts/git-safety.sh check     Verify config + hooks are in place (exit 1 if not).

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SESSION_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"

RED='\033[0;31m'
GREEN='\033[0;32m'
OFF='\033[0m'

apply_config() {
    git config --local push.force false
    git config --local push.forceWithLease false
    git config --local pull.rebase false
    git config --local rebase.autoSquash false
    git config --local fetch.prune false
    if [ -n "$SESSION_BRANCH" ]; then
        git config --local "branch.${SESSION_BRANCH}.rebase" false
    fi
}

install_pre_rebase() {
    cat > .git/hooks/pre-rebase <<'HOOK'
#!/usr/bin/env bash
# Git safety policy: rebasing is never acceptable in this repository.
echo "pre-rebase hook: REBASE REJECTED — repository policy forbids rebasing." >&2
echo "See SESSION_HANDOFF.md → 'Git safety policy'." >&2
exit 1
HOOK
    chmod +x .git/hooks/pre-rebase
}

install_pre_push() {
    cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
# Git safety policy: fast-forward pushes to the session branch only.
# Rejects non-fast-forward updates (force pushes) and branch deletions.
remote="$1"
zero="0000000000000000000000000000000000000000"
fail=0
while read -r local_ref local_sha remote_ref remote_sha; do
    [ -z "$remote_sha" ] && continue
    if [ "$local_sha" = "$zero" ]; then
        echo "pre-push hook: DELETION REJECTED — $remote_ref" >&2
        fail=1
        continue
    fi
    if [ "$remote_sha" != "$zero" ]; then
        if ! git merge-base --is-ancestor "$remote_sha" "$local_sha" 2>/dev/null; then
            echo "pre-push hook: NON-FAST-FORWARD REJECTED — $remote_ref" >&2
            echo "Force-pushing violates repository policy (SESSION_HANDOFF.md)." >&2
            fail=1
        fi
    fi
done
exit $fail
HOOK
    chmod +x .git/hooks/pre-push
}

cmd_install() {
    apply_config
    install_pre_rebase
    install_pre_push
    echo -e "${GREEN}Git safety policy installed:${OFF}"
    cmd_check
}

cmd_check() {
    ok=true
    for key in push.force push.forceWithLease pull.rebase rebase.autoSquash; do
        val="$(git config --local --get "$key" 2>/dev/null || echo "<unset>")"
        if [ "$val" = "false" ]; then
            echo -e "  ${GREEN}✓${OFF} git config $key = false"
        else
            echo -e "  ${RED}✗${OFF} git config $key = $val (run: scripts/git-safety.sh install)"
            ok=false
        fi
    done
    if [ -x .git/hooks/pre-rebase ]; then
        echo -e "  ${GREEN}✓${OFF} pre-rebase hook installed"
    else
        echo -e "  ${RED}✗${OFF} pre-rebase hook missing"
        ok=false
    fi
    if [ -x .git/hooks/pre-push ]; then
        echo -e "  ${GREEN}✓${OFF} pre-push hook installed"
    else
        echo -e "  ${RED}✗${OFF} pre-push hook missing"
        ok=false
    fi
    if [ -n "$SESSION_BRANCH" ]; then
        echo "  session branch: $SESSION_BRANCH (push only here, fast-forward only)"
    fi
    $ok || exit 1
}

case "${1:-check}" in
    install) cmd_install ;;
    check)   cmd_check ;;
    *) echo "Usage: scripts/git-safety.sh {install|check}"; exit 2 ;;
esac
