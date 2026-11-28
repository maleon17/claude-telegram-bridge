"""Direct (non-brokered) MCP servers exist only for administrator tenants with an explicit grant."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_direct_mcp_"))
os.environ["BRIDGE_ISOLATED_MCP_DIR"] = str(TMP / "mcp")
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001", BRIDGE_STATE_FILE=str(TMP / "state.json"),
                  BRIDGE_ISOLATION="on", BRIDGE_PEOPLE_FILE=str(TMP / "people.json"),
                  BRIDGE_TENANT_CONTROL_DIR=str(TMP / "control"))
(TMP / "mcp").mkdir()
for name in ("send_telegram_file_mcp.py", "brokered_mcp_exchange_mcp.py"):
    (TMP / "mcp" / name).write_text("# fixture\n")
sys.path.insert(0, str(ROOT))
import runtime  # noqa: E402
runtime.people.add("1000000002", "1000000001")
runtime.people.add("1000000003", "1000000001")
import tenant_mcp_broker as broker  # noqa: E402

DEFS = {
    "phone": {"type": "http", "url": "http://100.64.0.1:8486/mcp"},
    "relay": {"type": "stdio", "command": "/opt/x/python", "args": ["/opt/x/relay.py"], "env": {"RELAY_TOKEN": "t", "INSTANCE_ID": "i"}},
    "bad-relative": {"type": "stdio", "command": "python3", "args": []},
    "bad-scheme": {"type": "http", "url": "file:///etc/passwd"},
    "sql": {"type": "stdio", "command": "/opt/x/sql.sh", "args": []},
}
GRANTS = {"phone": {"direct": True}, "relay": {"direct": True}, "bad-relative": {"direct": True},
          "bad-scheme": {"direct": True}, "sql": {"tools": ["*"]}}


def servers_for(role):
    info = {"exchange": str(TMP / ".exchange"), "home": str(TMP), "role": role}
    with patch.object(broker, "server_definitions", return_value=DEFS), \
            patch.object(broker, "tenant_grants", return_value=GRANTS), \
            patch.object(runtime, "ensure_tenant", return_value=info), \
            patch.object(runtime, "codex_bridge_dir", return_value=None):
        return runtime._isolated_mcp_servers("1000000002")


def main():
    admin = servers_for("admin")
    assert admin["phone"] == {"type": "http", "url": "http://100.64.0.1:8486/mcp"}, admin.get("phone")
    assert admin["relay"] == {"type": "stdio", "command": "/opt/x/python", "args": ["/opt/x/relay.py"],
                              "env": {"RELAY_TOKEN": "t", "INSTANCE_ID": "i"}}
    assert "bad-relative" not in admin and "bad-scheme" not in admin  # invalid definitions are dropped
    assert admin["sql"]["env"]["BROKERED_SERVER"] == "sql"            # a tools grant is still proxied
    member = servers_for("member")
    assert "phone" not in member and "relay" not in member            # no direct servers without the role
    assert member["sql"]["env"]["BROKERED_SERVER"] == "sql"
    assert direct_missing_script_ok()
    shutil.rmtree(TMP, ignore_errors=True)
    print("CLOSED: direct MCP servers exist only for administrator tenants with a grant; invalid definitions are dropped.")


def direct_missing_script_ok():
    (TMP / "mcp" / "brokered_mcp_exchange_mcp.py").unlink()
    admin = servers_for("admin")
    return "phone" in admin and "sql" not in admin  # direct needs no proxy script, brokered servers do


if __name__ == "__main__":
    main()
