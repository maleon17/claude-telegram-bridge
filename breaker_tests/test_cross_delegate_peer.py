"""Cross-delegation to the Codex bridge only exists when that bridge does.

A fresh install has no sibling Codex bridge (or has it under the GitHub clone
name). The delegate tool must then be absent rather than silently creating a
ghost directory and reporting a misleading timeout.
"""

import json
import os
import sys
import tempfile
import threading
import time


TMP = tempfile.mkdtemp(prefix="breaker_cross_delegate_")
os.environ["HOME"] = TMP
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.pop("CODEX_TELEGRAM_BRIDGE_DIR", None)
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")
os.environ["CHAT_ID"] = "4242"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import delegate_to_codex_mcp as delegate  # noqa: E402
import runtime  # noqa: E402

OUR_SCRIPT = os.path.join(ROOT, "delegate_to_codex_mcp.py")


def _servers(config_dir):
    with open(os.path.join(config_dir, ".claude.json"), encoding="utf-8") as handle:
        return json.load(handle)["mcpServers"]


def _make_peer(name):
    peer = os.path.join(TMP, name)
    os.makedirs(peer)
    for marker in delegate.PEER_MARKER_FILES:
        open(os.path.join(peer, marker), "w").close()
    return peer


def test_absent_peer():
    delegate.PEER_DIR_NAMES = ()  # whatever sits next to this checkout is irrelevant
    assert delegate.codex_bridge_dir() is None
    before = set(os.listdir(os.path.dirname(ROOT)))
    ok, text = delegate._delegate_request("do something")
    assert not ok and "не установлен" in text, text
    assert set(os.listdir(os.path.dirname(ROOT))) == before, "a ghost peer directory was created"

    tenant = os.path.join(TMP, "tenant-absent")
    os.makedirs(tenant)
    runtime._ensure_tenant_mcp_config(tenant)
    assert "delegate-to-codex" not in _servers(tenant)
    assert "send-telegram-file" in _servers(tenant)

    # An entry this bridge seeded earlier is withdrawn; a repointed one stays.
    for args, survives in (([OUR_SCRIPT], False), (["/elsewhere/custom.py"], True)):
        tenant = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(tenant, ".claude.json"), "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": {"delegate-to-codex": {"command": "x", "args": args}}}, handle)
        runtime._ensure_tenant_mcp_config(tenant)
        assert ("delegate-to-codex" in _servers(tenant)) is survives, args


def test_stray_directory_is_not_a_peer():
    os.makedirs(os.path.join(TMP, "not-a-bridge"))
    os.environ["CODEX_TELEGRAM_BRIDGE_DIR"] = os.path.join(TMP, "not-a-bridge")
    try:
        assert delegate.codex_bridge_dir() is None
    finally:
        del os.environ["CODEX_TELEGRAM_BRIDGE_DIR"]


def test_present_peer():
    peer = _make_peer("codex-telegram-bridge")
    os.environ["CODEX_TELEGRAM_BRIDGE_DIR"] = peer
    assert str(delegate.codex_bridge_dir()) == peer

    tenant = os.path.join(TMP, "tenant-present")
    os.makedirs(tenant)
    runtime._ensure_tenant_mcp_config(tenant)
    entry = _servers(tenant)["delegate-to-codex"]
    assert entry["args"] == [OUR_SCRIPT]
    # Claude passes its MCP servers a filtered environment, so the location is
    # stored with the server -- and follows the peer if it moves.
    assert entry["env"] == {"CODEX_TELEGRAM_BRIDGE_DIR": peer}
    assert "CHAT_ID" not in entry["env"]
    moved = _make_peer("codex-telegram-bridge-moved")
    os.environ["CODEX_TELEGRAM_BRIDGE_DIR"] = moved
    runtime._ensure_tenant_mcp_config(tenant)
    assert _servers(tenant)["delegate-to-codex"]["env"] == {"CODEX_TELEGRAM_BRIDGE_DIR": moved}
    os.environ["CODEX_TELEGRAM_BRIDGE_DIR"] = peer

    queue = os.path.join(peer, "cross_delegate_queue")
    results = os.path.join(peer, "cross_delegate_result")

    def accepting_peer():
        deadline = time.time() + 5
        while time.time() < deadline:
            names = [n for n in os.listdir(queue) if n.endswith(".json")] if os.path.isdir(queue) else []
            if names:
                request = json.load(open(os.path.join(queue, names[0]), encoding="utf-8"))
                assert request == {"chat_id": 4242, "text": "real task"}
                os.remove(os.path.join(queue, names[0]))
                with open(os.path.join(results, names[0]), "w", encoding="utf-8") as handle:
                    json.dump({"done": True, "ok": True, "text": "accepted"}, handle)
                return
            time.sleep(0.05)

    worker = threading.Thread(target=accepting_peer)
    worker.start()
    ok, text = delegate._delegate_request("real task")
    worker.join()
    assert ok and text == "accepted", text

    # Peer installed but not running: the request is withdrawn, and says so.
    delegate.POLL_TIMEOUT_S = 0.6
    ok, text = delegate._delegate_request("nobody home")
    assert not ok and "НЕ начата" in text, text
    assert not [n for n in os.listdir(queue) if n.endswith(".json")]

    # Peer claimed the request but never confirmed: never claim "not started".
    def claiming_peer():
        deadline = time.time() + 5
        while time.time() < deadline:
            names = [n for n in os.listdir(queue) if n.endswith(".json")]
            if names:
                os.remove(os.path.join(queue, names[0]))
                return
            time.sleep(0.05)

    worker = threading.Thread(target=claiming_peer)
    worker.start()
    ok, text = delegate._delegate_request("claimed")
    worker.join()
    assert not ok and "забрал запрос" in text and "НЕ начата" not in text, text


if __name__ == "__main__":
    test_absent_peer()
    test_stray_directory_is_not_a_peer()
    test_present_peer()
    print("CLOSED: delegate-to-codex is offered only with a real Codex bridge and reports its state truthfully.")
