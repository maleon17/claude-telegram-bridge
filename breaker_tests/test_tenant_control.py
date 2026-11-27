"""Isolated persona and MCP files belong to the bridge control plane."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_tenant_control_"))
# Hermetic: the default MCP directory exists on hosts that installed the isolation module.
os.environ["BRIDGE_ISOLATED_MCP_DIR"] = str(TMP / "no-mcp")
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001",
                  BRIDGE_STATE_FILE=str(TMP / "state.json"), BRIDGE_ISOLATION="off")
sys.path.insert(0, str(ROOT))
import handlers  # noqa: E402
import runtime  # noqa: E402


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def main():
    tenant = "1000000002"
    owner = runtime.OWNER_ID
    config = TMP / "accounts" / tenant
    owner_config = TMP / "accounts" / owner
    exchange = TMP / "exchange"
    exchange.mkdir()
    control_root = TMP / "bridge"
    control_root.mkdir()
    shutil.copyfile(ROOT / "personality.example.md", control_root / "personality.example.md")
    provision = lambda chat_id, kind="main": {
        "id": str(chat_id).removeprefix("delegate:"), "kind": kind,
        "home": str(config), "exchange": str(exchange), "uid": 22001,
    }
    with patch.object(runtime, "__file__", str(control_root / "runtime.py")), \
            patch.object(runtime, "ensure_tenant", side_effect=provision), \
            patch.object(runtime, "ACCOUNTS_DIR", str(TMP / "accounts")):
        runtime.BRIDGE_ISOLATION = "off"
        with patch.object(handlers, "account_dir", return_value=str(config)):
            assert handlers._persona_path(tenant) == str(config / "CLAUDE.md")
        ordinary = runtime.tenant_run_argv(tenant, "main", args=("-p",))
        assert ordinary[-2:] == ["claude", "-p"]
        assert "--mcp-config" not in ordinary

        runtime.BRIDGE_ISOLATION = "on"
        runtime.ISOLATED_TENANTS = frozenset((tenant, owner))
        with patch.object(runtime, "_ensure_tenant_mcp_config", side_effect=AssertionError("tenant MCP write")), \
                patch.object(handlers, "account_dir", side_effect=AssertionError("tenant config write")):
            persona = Path(handlers._persona_path(tenant))
            assert persona == control_root / "tenant_control" / tenant / "persona.md"
            assert persona.exists() and mode(persona) == 0o600
            assert mode(persona.parent) == 0o700
            assert mode(persona.parent.parent) == 0o700
            assert mode(persona.parent / ".persona_default_sha256") == 0o600
            assert mode(persona.parent / ".workspace_rules_seeded") == 0o600
            argv = runtime.tenant_run_argv(tenant, "main", args=("-p",))
            assert argv[-7:] == ["claude", "--append-system-prompt-file",
                                 str(exchange / "persona.md"), "--mcp-config",
                                 str(exchange / "mcp.json"), "--strict-mcp-config", "-p"]
            assert (exchange / "persona.md").read_bytes() == persona.read_bytes()
            assert mode(exchange / "persona.md") == 0o644
            mcp = Path(runtime.tenant_mcp_path(tenant))
            assert mode(mcp) == 0o600 and mode(exchange / "mcp.json") == 0o644
            assert json.loads(mcp.read_text()) == {"mcpServers": {}}
            assert json.loads((exchange / "mcp.json").read_text()) == {"mcpServers": {}}
            before = (exchange / "persona.md").stat().st_mtime_ns
            runtime.tenant_run_argv(tenant, "main", args=("-p",))
            assert (exchange / "persona.md").stat().st_mtime_ns == before
            (exchange / "persona.md").write_text("tenant replacement")
            (exchange / "mcp.json").write_text('{"mcpServers":{"evil":{}}}')
            runtime.tenant_run_argv(tenant, "main", args=("-p",))
            assert (exchange / "persona.md").read_bytes() == persona.read_bytes()
            assert (exchange / "mcp.json").read_bytes() == mcp.read_bytes()

            delegate = Path(runtime.tenant_persona_path("delegate:" + tenant))
            assert delegate.parent == persona.parent / "delegate"
            runtime.tenant_run_argv("delegate:" + tenant, "delegate", args=("-p",))
            assert delegate.exists() and mode(delegate) == 0o600

            with patch.object(handlers, "send_message", return_value={}):
                assert handlers.handle_command(owner, "/persona reset", {})
                owner_persona = Path(runtime.tenant_persona_path(owner))
                assert owner_persona.read_text() == (ROOT / "personality.example.md").read_text()
                assert mode(owner_persona) == 0o600
                with patch.object(handlers, "set_chat_commands"), \
                        patch.object(handlers, "_finish_language_message"):
                    assert handlers.handle_command(owner, "/language en", {})
                assert owner_persona.read_text() == (ROOT / "personality.example.en.md").read_text()
                assert mode(owner_persona) == 0o600
                owner_persona.write_text("# Custom persona\n\nKeep my own wording and structure.\n")
                translated = json.dumps({"result": "# Eigene Persona\n\nBehalte meine Struktur und Formulierung.\n"})
                with patch.object(handlers, "set_chat_commands"), \
                        patch.object(handlers, "_finish_language_message"), \
                        patch.object(handlers.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0, translated, "")) as run:
                    assert handlers.handle_command(owner, "/language de", {})
                assert "--no-session-persistence" in run.call_args.args[0]
                assert "--strict-mcp-config" in run.call_args.args[0]
                assert owner_persona.read_text().startswith("# Eigene Persona")
                handlers.persona_message_ids[str(owner)] = {77}
                assert handlers.handle_persona_reply(owner, {
                    "text": "Owner reply persona", "reply_to_message": {"message_id": 77},
                })
                assert owner_persona.read_text() == "Owner reply persona"

        assert not config.exists() and not owner_config.exists()
        assert runtime.account_dir(tenant) == str(config / ".claude-bridge")
        assert not config.exists()


if __name__ == "__main__":
    main()
    print("CLOSED: tenant control files are private; exchange copies and argv are restored.")
