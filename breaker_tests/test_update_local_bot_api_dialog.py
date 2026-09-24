"""The owner-only /update local-Bot-API dialog stays in memory and out of Claude.

After a successful code update without TELEGRAM_API_URL, the dialog consumes
Yes, api_id, and api_hash before normal prompt routing.  It keeps credentials
out of the bridge state and starts the installer only after both values arrive.
"""

import os
import subprocess
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_update_local_api_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")
os.environ["BRIDGE_ENV_FILE"] = os.path.join(TMP, "bridge.env")
os.environ["TELEGRAM_BOT_API_ENV_FILE"] = os.path.join(TMP, "telegram-bot-api.env")
os.environ["SERVICE_NAME"] = "test-bridge.service"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handlers  # noqa: E402


def main():
    assert hasattr(handlers, "_start_local_bot_api_install"), (
        "/update has no local Bot API installer dialog"
    )
    messages = []
    installs = []
    original_run = handlers.subprocess.run
    original_install = handlers._start_local_bot_api_install
    original_send = handlers.send_message
    handlers.send_message = lambda chat_id, text, **kw: messages.append((chat_id, text)) or {"ok": True}
    handlers.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout="already up to date\n", stderr=""
    )
    handlers._start_local_bot_api_install = lambda chat_id, api_id, api_hash: installs.append(
        (chat_id, api_id, api_hash)
    )
    state = {"1000000001": {"account_status": "ready"}}
    try:
        assert handlers.handle_command("1000000001", "/update", state)
        assert "Включить приём файлов до 2 ГБ" in messages[-1][1]
        assert handlers.handle_onboarding("1000000001", "1000000001", "Да", state, {"1000000001"})
        assert handlers.handle_onboarding("1000000001", "1000000001", "12345", state, {"1000000001"})
        assert handlers.handle_onboarding("1000000001", "1000000001", "secret-api-hash", state, {"1000000001"})
        assert installs == [("1000000001", "12345", "secret-api-hash")]
        assert "secret-api-hash" not in repr(state)
        env_path = os.environ["BRIDGE_ENV_FILE"]
        env_contents = open(env_path, encoding="utf-8").read() if os.path.exists(env_path) else ""
        assert "secret-api-hash" not in env_contents

        setup = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "setup.sh"), encoding="utf-8").read()
        assert "LOCAL_BOT_API_SETUP" in setup
        assert "TELEGRAM_BOT_API_UNIT" in setup
        assert '"$TELEGRAM_BOT_API_UNIT"' in setup
    finally:
        handlers.subprocess.run = original_run
        handlers._start_local_bot_api_install = original_install
        handlers.send_message = original_send


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: /update consumes local Bot API credentials in memory before Claude can see them.")
        raise SystemExit(0)
