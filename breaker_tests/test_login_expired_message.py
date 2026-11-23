"""A turn that fails only because the Claude sign-in expired tells the user to
use the bridge's /login, not the CLI's terminal advice."""

import os
import sys
import tempfile


TEST_ROOT = tempfile.mkdtemp(prefix="bridge_login_expired_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_process  # noqa: E402
from strings import t  # noqa: E402

EXPIRED = (
    "Failed to authenticate: this login reached its time limit and cannot be renewed. "
    "Run claude auth login to sign in again, or use claude setup-token and set "
    "CLAUDE_CODE_OAUTH_TOKEN for unattended jobs"
)


def main():
    assert chat_process.is_auth_failure({"type": "result", "is_error": True, "result": EXPIRED})
    # Older CLIs did not always flag it as an error; the wording still gives it away.
    assert chat_process.is_auth_failure({"type": "result", "result": EXPIRED})
    assert chat_process.is_auth_failure({
        "type": "result", "is_error": True,
        "result": "API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}",
    })
    # A normal answer that merely talks about logging in is left alone.
    assert not chat_process.is_auth_failure({
        "type": "result", "result": "To fix it, run claude auth login on the server.",
    })
    assert not chat_process.is_auth_failure({"type": "result", "is_error": True, "result": "Overloaded"})

    hint = t("chat_process_login_expired", value0="/login", lang="ru")
    assert "/login" in hint and "claude auth login" not in hint


if __name__ == "__main__":
    main()
    print("CLOSED: an expired sign-in points to the bridge's /login.")
