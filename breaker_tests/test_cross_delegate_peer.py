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
    ok, text = delegate._delegate_request("real task", 0)
    worker.join()
    assert ok and text == "accepted", text

    # Peer installed but not running: the request is withdrawn, and says so.
    delegate.POLL_TIMEOUT_S = 0.6
    ok, text = delegate._delegate_request("nobody home", 0)
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
    ok, text = delegate._delegate_request("claimed", 0)
    worker.join()
    assert not ok and "забрал запрос" in text and "НЕ начата" not in text, text


def test_final_answer_comes_back_to_the_delegating_agent():
    peer = _make_peer("codex-telegram-bridge-final")
    os.environ["CODEX_TELEGRAM_BRIDGE_DIR"] = peer
    queue = os.path.join(peer, "cross_delegate_queue")
    results = os.path.join(peer, "cross_delegate_result")
    final_path = os.path.join(peer, "final.json")
    delegate.POLL_TIMEOUT_S = 5
    delegate.FINAL_POLL_INTERVAL_S = 0.05

    def peer_with(final_payload, delay=0.3, stale=None):
        """Accepts the request, then publishes (or not) the turn's final answer."""
        def run():
            deadline = time.time() + 5
            while time.time() < deadline:
                names = [n for n in os.listdir(queue) if n.endswith(".json")] if os.path.isdir(queue) else []
                if names:
                    os.remove(os.path.join(queue, names[0]))
                    if stale is not None:
                        with open(final_path, "w", encoding="utf-8") as handle:
                            json.dump(stale, handle)
                    with open(os.path.join(results, names[0]), "w", encoding="utf-8") as handle:
                        json.dump({
                            "done": True, "ok": True, "text": "accepted",
                            "final": {"path": final_path, "after_ts": time.time(), "consume": True},
                        }, handle)
                    if final_payload is not None:
                        time.sleep(delay)
                        payload = dict(final_payload, ts=time.time())
                        with open(final_path + ".tmp", "w", encoding="utf-8") as handle:
                            json.dump(payload, handle)
                        os.replace(final_path + ".tmp", final_path)
                    return
                time.sleep(0.02)
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    worker = peer_with({"text": "42 is the answer", "ok": True})
    ok, text = delegate._delegate_request("compute", 5)
    worker.join()
    assert ok and text.endswith("42 is the answer") and text.startswith("Ответ Codex"), text
    assert not os.path.exists(final_path), "a per-request result must be collected"

    worker = peer_with({"text": "boom", "ok": False})
    ok, text = delegate._delegate_request("compute", 5)
    worker.join()
    assert not ok and "boom" in text and "ошибкой" in text, text

    # An answer left over from an earlier turn is not this request's answer,
    # and a task that outlives the wait is reported as still running.
    worker = peer_with(None, stale={"text": "OLD ANSWER", "ok": True, "ts": time.time() - 3600})
    ok, text = delegate._delegate_request("slow", 0.4)
    worker.join()
    assert ok and "OLD ANSWER" not in text and "не завершилась" in text, text
    os.remove(final_path)

    # A peer that predates this feature sends no "final": still accepted.
    def old_peer():
        deadline = time.time() + 5
        while time.time() < deadline:
            names = [n for n in os.listdir(queue) if n.endswith(".json")]
            if names:
                os.remove(os.path.join(queue, names[0]))
                with open(os.path.join(results, names[0]), "w", encoding="utf-8") as handle:
                    json.dump({"done": True, "ok": True, "text": "accepted"}, handle)
                return
            time.sleep(0.02)
    worker = threading.Thread(target=old_peer)
    worker.start()
    ok, text = delegate._delegate_request("compat", 5)
    worker.join()
    assert ok and text.startswith("accepted"), text


if __name__ == "__main__":
    test_absent_peer()
    test_stray_directory_is_not_a_peer()
    test_present_peer()
    test_final_answer_comes_back_to_the_delegating_agent()
    print("CLOSED: delegate-to-codex is offered only with a real Codex bridge and reports its state truthfully.")
