#!/usr/bin/env bash
# Updates this install to the latest push on the tracking branch. The new
# revision is checked out into a temporary worktree and must pass
# scripts/run_tests.sh there first; only then is the working checkout
# fast-forwarded. On any failure the working checkout is left untouched.
# Restarting is not done here: /update schedules the usual deferred restart,
# and a manual run should be followed by /restart (or systemctl restart).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: local changes to tracked files present -- resolve or stash them first." >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch origin "$BRANCH"
TARGET="origin/$BRANCH"

if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$TARGET")" ]; then
    echo "Already up to date at $(git rev-parse --short HEAD) on $BRANCH."
    exit 0
fi
if ! git merge-base --is-ancestor HEAD "$TARGET"; then
    echo "ERROR: $TARGET is not a fast-forward of the local checkout." >&2
    exit 1
fi

CANDIDATE="$(mktemp -d)"
cleanup() {
    git worktree remove --force "$CANDIDATE" >/dev/null 2>&1 || rm -rf "$CANDIDATE"
}
trap cleanup EXIT
git worktree add --detach "$CANDIDATE" "$TARGET" >/dev/null

echo "Checking candidate $(git rev-parse --short "$TARGET")..."
if [ -x "$CANDIDATE/scripts/run_tests.sh" ]; then
    CHECK=("$CANDIDATE/scripts/run_tests.sh")
else
    CHECK=(bash -c "cd '$CANDIDATE' && python3 -m py_compile bridge.py runtime.py telegram_api.py state_store.py chat_process.py handlers.py telegram_format.py")
fi
if ! "${CHECK[@]}"; then
    echo "ERROR: candidate $(git rev-parse --short "$TARGET") failed checks -- checkout left unchanged." >&2
    exit 1
fi

git merge --ff-only "$TARGET"
echo "Updated to $(git rev-parse --short HEAD) on $BRANCH."
