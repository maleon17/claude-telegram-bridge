#!/usr/bin/env bash
# Runs every check for this repository: byte-compilation of all modules and
# each breaker_tests/test_*.py script. Tests import the checkout this script
# lives in (not a fixed install path), so it can validate an update candidate
# in a separate worktree. Cross-repo tests skip themselves when the sibling
# checkouts (CLAUDE_JARVIS_DIR / CODEX_JARVIS_DIR) are absent.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m py_compile bridge.py runtime.py telegram_api.py state_store.py chat_process.py \
    handlers.py telegram_format.py bridge_exec.py send_telegram_file_mcp.py \
    delegate_to_codex_mcp.py || exit 1

failed=0
for test in breaker_tests/test_*.py; do
    # Tests must never inherit the live bridge's identity or state.
    if output="$(env -u CHAT_ID -u SERVICE_NAME -u BRIDGE_STATE_FILE timeout 300 python3 "$test" 2>&1)"; then
        if grep -q '^SKIP:' <<<"$output"; then
            echo "SKIP  $test"
        else
            echo "PASS  $test"
        fi
    else
        echo "FAIL  $test"
        tail -20 <<<"$output" | sed 's/^/      /'
        failed=1
    fi
done
exit "$failed"
