"""Delegated Claude homes require their own OAuth login and code routing."""

import json
import os
import sys
import tempfile
import time
from unittest.mock import patch


TEST_ROOT = tempfile.mkdtemp(prefix="bridge_delegate_login_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import runtime  # noqa: E402
from state_store import delegate_key  # noqa: E402


def main():
    chat_id = 42
    owner_home = runtime.account_dir(chat_id)
    delegate_home = runtime.account_dir(chat_id, state_key=delegate_key(chat_id))
    assert owner_home != delegate_home
    assert not os.path.lexists(os.path.join(delegate_home, ".credentials.json"))

    calls = []
    with patch.object(handlers, "start_login", side_effect=lambda *a, **kw: calls.append((a, kw))), \
            patch.object(handlers, "send_message"):
        assert handlers.handle_command(chat_id, "/login delegate", {})
    assert calls == [((chat_id, {}), {"delegated": True})]

    received = []
    handlers.pending_logins[chat_id] = {"delegated": True}
    try:
        with patch.object(handlers, "feed_login_code", side_effect=lambda *a: received.append(a)), \
                patch.object(handlers, "send_message"):
            assert handlers.handle_onboarding(chat_id, chat_id, "oauth-code", {}, {str(chat_id)})
        assert received == [(chat_id, "oauth-code", {})]
    finally:
        handlers.pending_logins.pop(chat_id, None)

    credentials = os.path.join(delegate_home, ".credentials.json")
    with open(credentials, "w", encoding="utf-8") as handle:
        json.dump({"claudeAiOauth": {
            "accessToken": "access", "refreshToken": "refresh",
            "expiresAt": int((time.time() + 3600) * 1000),
        }}, handle)
    info = {"config_dir": delegate_home, "started_at": time.time()}
    assert handlers._login_credentials_ready(info)
    assert not handlers._login_credentials_ready({**info, "started_at": time.time() + 60})
    with open(credentials, "w", encoding="utf-8") as handle:
        json.dump({"claudeAiOauth": {"accessToken": "access", "expiresAt": 0}}, handle)
    assert not handlers._login_credentials_ready(info)


if __name__ == "__main__":
    main()
    print("CLOSED: delegated OAuth remains isolated and login codes reach its flow.")
