"""Tenant state stays private under the default systemd umask."""

import os
import stat
import sys
import tempfile
from unittest.mock import patch


os.umask(0o022)
TEST_ROOT = tempfile.mkdtemp(prefix="bridge_tenant_file_modes_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_process  # noqa: E402
import runtime  # noqa: E402
from state_store import save_state  # noqa: E402


def main():
    save_state({"4242": {"pending_prompt": "secret"}})
    assert stat.S_IMODE(os.stat(runtime.STATE_FILE).st_mode) & 0o044 == 0

    chat_process.write_last_turn(4242, "secret answer")
    last_turn = os.path.join(TEST_ROOT, "last_turn_state_4242.json")
    assert stat.S_IMODE(os.stat(last_turn).st_mode) & 0o044 == 0

    runtime.tenant_file_outbox(5151)
    tenant_dir = runtime.account_dir(5151)
    assert stat.S_IMODE(os.stat(tenant_dir).st_mode) & 0o077 == 0
    delegated_dir = runtime.account_dir(5151, state_key="delegate:5151")
    assert stat.S_IMODE(os.stat(delegated_dir).st_mode) & 0o077 == 0

    with patch.dict(os.environ, {
        "PATH": "/usr/bin", "HOME": TEST_ROOT, "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret",
        "TELEGRAM_API_HASH": "secret", "TELEGRAM_API_ID": "secret",
    }):
        child_env = runtime.claude_env(tenant_dir)
        assert all(child_env[key] == os.environ[key] for key in ("PATH", "HOME", "LANG"))
        assert not {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                    "TELEGRAM_API_HASH", "TELEGRAM_API_ID"} & child_env.keys()
        assert child_env.keys() <= runtime.BRIDGE_ENV_ALLOWLIST | {"CLAUDE_CONFIG_DIR"}
        explicit_env = runtime.claude_env(
            tenant_dir, extra_env={"ANTHROPIC_API_KEY": "per-turn"},
        )
        assert explicit_env["ANTHROPIC_API_KEY"] == "per-turn"


if __name__ == "__main__":
    main()
    print("CLOSED: tenant state and account directory modes are private; Claude inherits only allowed environment variables.")
