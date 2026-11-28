"""The runtime note an isolated agent receives depends on its role: members have no sudo, administrators do."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_admin_note_"))
os.environ["BRIDGE_ISOLATED_MCP_DIR"] = str(TMP / "no-mcp")
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001", BRIDGE_STATE_FILE=str(TMP / "state.json"),
                  BRIDGE_ISOLATION="on", ISOLATED_TENANTS="1000000002,1000000003",
                  BRIDGE_TENANT_CONTROL_DIR=str(TMP / "control"))
sys.path.insert(0, str(ROOT))
import runtime  # noqa: E402


def main():
    member = runtime.with_isolation_note("persona")
    admin = runtime.with_isolation_note("persona", "admin")
    assert runtime.ISOLATION_NOTE in member and runtime.ADMIN_ISOLATION_NOTE not in member
    assert runtime.ADMIN_ISOLATION_NOTE in admin and runtime.ISOLATION_NOTE not in admin
    assert "без пароля" in admin and "недоступны" in member
    assert runtime.with_isolation_note(admin, "admin") == admin and runtime.with_isolation_note(member) == member
    exchange = TMP / ".exchange"
    exchange.mkdir()
    for chat, role in (("1000000002", "member"), ("1000000003", "admin")):
        def provision(chat_id, kind="main", role=role):
            return {"exchange": str(exchange), "home": str(TMP), "role": role}
        with patch.object(runtime, "ensure_tenant", side_effect=provision):
            runtime.stage_tenant_control(chat)
        staged = (exchange / "persona.md").read_text(encoding="utf-8")
        assert (runtime.ADMIN_ISOLATION_NOTE in staged) == (role == "admin"), role
        assert (runtime.ISOLATION_NOTE in staged) == (role == "member"), role
        stored = Path(runtime.tenant_persona_path(chat)).read_text(encoding="utf-8")
        assert runtime.ADMIN_ISOLATION_NOTE not in stored and runtime.ISOLATION_NOTE not in stored
    shutil.rmtree(TMP, ignore_errors=True)
    print("CLOSED: administrator tenants get the sudo-aware runtime note, members the restricted one; the stored persona stays clean.")


if __name__ == "__main__":
    main()
