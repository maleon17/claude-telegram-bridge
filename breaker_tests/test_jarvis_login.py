"""Owner-only ClaudeAsk OAuth uses its own home and leaves the chat login alone."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bridge_jarvis_login_"))
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = str(TEST_ROOT / "state.json")
os.environ["BRIDGE_PEOPLE_FILE"] = str(TEST_ROOT / "people.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handlers  # noqa: E402
import runtime  # noqa: E402


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for stub OAuth")


def main():
    owner = int(os.environ["OWNER_ID"])
    other = owner + 1
    runtime.people.add(other, owner)
    config_dir = TEST_ROOT / "worker_home"
    config_dir.mkdir()
    stub_log = TEST_ROOT / "stub_calls.jsonl"
    stub = TEST_ROOT / "claude_stub"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"log = Path({str(stub_log)!r})\n"
        "config = Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps({'args': sys.argv[1:], 'config': str(config)}) + '\\n')\n"
        "if sys.argv[1:] == ['auth', 'login', '--claudeai']:\n"
        "    print('https://example.test/oauth', flush=True)\n"
        "    code = sys.stdin.readline().strip()\n"
        "    if code:\n"
        "        config.mkdir(parents=True, exist_ok=True)\n"
        "        data = {'claudeAiOauth': {'accessToken': 'fake', 'refreshToken': 'fake',"
        " 'expiresAt': int((time.time() + 3600) * 1000)}}\n"
        "        (config / '.credentials.json').write_text(json.dumps(data))\n"
        "elif sys.argv[1:] == ['auth', 'status']:\n"
        "    print(json.dumps({'loggedIn': (config / '.credentials.json').exists()}))\n"
    )
    stub.chmod(0o700)
    state = {str(owner): {"account_status": "ready", "language": "en"}}
    original = json.loads(json.dumps(state))
    owner_credentials = runtime.ACCOUNTS_DIR + f"/{owner}/.credentials.json"
    messages = []
    with patch.object(handlers, "CLAUDE_BIN", str(stub)), \
            patch.object(handlers, "send_message", side_effect=lambda _id, msg, **kw: messages.append(msg)), \
            patch.object(handlers, "tg_call", return_value={}), \
            patch.dict(os.environ, {"JARVIS_CLAUDE_CONFIG_DIR": str(config_dir)}):
        assert not handlers.handle_onboarding(
            owner, owner, "login flow is broken, fix it", state, {str(owner)}
        )
        assert handlers.handle_onboarding(other, other, "/login jarvis", {}, {str(other)})
        assert not handlers.pending_logins.get(f"jarvis:{other}")
        assert not stub_log.exists()
        assert "owner" in messages[-1].lower() or "владель" in messages[-1].lower()

        assert handlers.handle_onboarding(owner, owner, "/login jarvis", state, {str(owner)})
        key = f"jarvis:{owner}"
        info = handlers.pending_logins[key]
        assert info["config_dir"] == str(config_dir)
        assert Path(info["fifo"]).parent == Path(handlers.__file__).resolve().parent / "login"
        assert not Path(info["fifo"]).is_relative_to(config_dir)
        assert (Path(info["fifo"]).parent.stat().st_mode & 0o777) == 0o700
        assert state == original
        assert not os.path.exists(owner_credentials)
        wait_for(lambda: stub_log.exists())
        wait_for(lambda: "ttl_timer" in info)
        assert not handlers.handle_onboarding(
            owner, owner, "login flow is broken, fix it", state, {str(owner)}
        )
        assert not handlers.handle_onboarding(owner, owner, "/stop", state, {str(owner)})
        assert not handlers.handle_onboarding(owner, owner, "short", state, {str(owner)})
        assert not handlers.handle_onboarding(owner, owner, "a" * 20 + " space", state, {str(owner)})
        assert not handlers.handle_onboarding(owner, owner, "a" * 20 + "\n", state, {str(owner)})
        first_proc = info["proc"]
        first_timer = info["ttl_timer"]
        assert handlers.handle_onboarding(owner, owner, "/login jarvis", state, {str(owner)})
        wait_for(lambda: first_proc.poll() is not None)
        wait_for(lambda: not first_timer.is_alive())
        info = handlers.pending_logins[key]
        assert info["proc"] is not first_proc
        wait_for(lambda: "ttl_timer" in info)
        assert handlers.handle_onboarding(owner, owner, "test-code-12345678901234", state, {str(owner)})
        wait_for(lambda: key not in handlers.pending_logins)
        calls = [json.loads(line) for line in stub_log.read_text().splitlines()]
        assert [call["args"] for call in calls] == [
            ["auth", "login", "--claudeai"],
            ["auth", "login", "--claudeai"],
            ["auth", "status"],
        ]
        assert all(call["config"] == str(config_dir) for call in calls)
        assert state == original
        assert not os.path.exists(owner_credentials)
        assert any("ClaudeAsk connected" in msg for msg in messages)
        assert not Path(info["fifo"]).exists()

        # The timer starts when the link is shown and cleans up an idle login.
        with patch.object(handlers, "JARVIS_LOGIN_TTL_S", 0.2):
            handlers.start_login(owner, state, jarvis=True)
            expired = handlers.pending_logins[key]
            wait_for(lambda: "ttl_timer" in expired)
            wait_for(lambda: key not in handlers.pending_logins)
        wait_for(lambda: expired["proc"].poll() is not None)
        assert not Path(expired["fifo"]).exists()
        wait_for(lambda: any("timed out" in msg and "/login jarvis" in msg for msg in messages))
        assert state == original

        # A pending Jarvis login routes only long code tokens. Login commands
        # still dispatch while ordinary commands and prompts reach the bridge.
        chat_info = {"started_at": time.time() - 10, "delegated": False}
        jarvis_info = {"started_at": time.time(), "jarvis": True}
        handlers.pending_logins[owner] = chat_info
        handlers.pending_logins[key] = jarvis_info
        received = []
        with patch.object(handlers, "feed_login_code", side_effect=lambda *a, **kw: received.append((a, kw))), \
                patch.object(handlers, "start_login") as start:
            assert handlers.handle_onboarding(owner, owner, "a" * 20, state, {str(owner)})
            assert received == [((owner, "a" * 20, state), {"login_key": key})]
            assert not handlers.handle_onboarding(owner, owner, "/status", state, {str(owner)})
            assert not handlers.handle_onboarding(owner, owner, "login flow is broken, fix it", state, {str(owner)})
            assert handlers.handle_onboarding(owner, owner, "/login delegate", state, {str(owner)})
            start.assert_called_with(owner, state, delegated=True)
            assert handlers.handle_onboarding(owner, owner, "/login", state, {str(owner)})
            start.assert_called_with(owner, state, delegated=False)
        handlers.pending_logins.pop(owner)
        handlers.pending_logins.pop(key)

        # Legacy delegated-pending and awaiting-code restart branches survive.
        with patch.object(handlers, "start_login") as start, \
                patch.object(handlers, "send_message"):
            handlers.pending_logins[owner] = {"delegated": True}
            assert handlers.handle_onboarding(owner, owner, "/login", state, {str(owner)})
            start.assert_called_with(owner, state, delegated=True)
            start.reset_mock()
            assert handlers.handle_onboarding(owner, owner, "login", state, {str(owner)})
            start.assert_called_with(owner, state, delegated=True)
            handlers.pending_logins.pop(owner)
            state[str(owner)]["account_status"] = "awaiting_code"
            start.reset_mock()
            assert handlers.handle_onboarding(owner, owner, "/login", state, {str(owner)})
            start.assert_called_with(owner, state)
            start.reset_mock()
            assert handlers.handle_onboarding(owner, owner, "login", state, {str(owner)})
            start.assert_called_with(owner, state)
            state[str(owner)]["account_status"] = "ready"

        # Empty and relative overrides must resolve to the default home.
        for override in ("", "relative-home"):
            with patch.dict(os.environ, {"JARVIS_CLAUDE_CONFIG_DIR": override, "HOME": str(TEST_ROOT)}):
                handlers.start_login(owner, state, jarvis=True)
                fallback = handlers.pending_logins[key]
                assert fallback["config_dir"] == str(TEST_ROOT / ".claude")
                handlers._cleanup_login(key, fallback)


if __name__ == "__main__":
    main()
    print("CLOSED: owner-only ClaudeAsk OAuth stays separate from chat credentials.")
