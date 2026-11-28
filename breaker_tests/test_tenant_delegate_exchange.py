"""C2a: tenant delegate routing and the bounded exchange channel."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_delegate_exchange_"))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001",
                  BRIDGE_STATE_FILE=str(TMP / "state.json"), BRIDGE_ISOLATION="off",
                  BRIDGE_TENANT_CONTROL_DIR=str(TMP / "control"),
                  BRIDGE_ISOLATED_MCP_DIR=str(TMP / "mcp"))
sys.path.insert(0, str(ROOT))
import delegate_to_codex_exchange_mcp as mcp  # noqa: E402
import delegate_to_codex_mcp as legacy  # noqa: E402
import runtime  # noqa: E402
import tenant_delegate_exchange as broker  # noqa: E402


def main():
    tenant = "1000000002"
    owner = runtime.OWNER_ID
    home = TMP / "home"
    exchange = home / ".exchange"
    for part in ("queue", "result"):
        (exchange / part).mkdir(parents=True)
    (TMP / "mcp").mkdir()
    (TMP / "mcp" / "delegate_to_codex_exchange_mcp.py").write_text("# installed")
    (TMP / "mcp" / "send_telegram_file_mcp.py").write_text("# installed")
    assert runtime.tenant_target(tenant) == ("main", "bridge")
    assert runtime.tenant_target("delegate:" + tenant) == ("main", "delegate")
    assert runtime.tenant_target("delegate:" + owner) == ("delegate", "bridge")
    provision = lambda key, kind="main": {"home": str(home), "exchange": str(exchange)}
    with patch.object(runtime, "ensure_tenant", side_effect=provision), \
            patch.object(runtime, "codex_bridge_dir", return_value=TMP), \
            patch.object(runtime, "BRIDGE_ISOLATION", "on"), \
            patch.object(runtime, "ISOLATED_TENANTS", frozenset((tenant, owner))):
        argv = runtime.tenant_run_argv("delegate:" + tenant, "delegate", args=("-p",))
        assert argv[4:8] == ["--kind", "main", "--product", "delegate"]
        assert str(exchange / "persona.delegate.md") in argv
        assert str(exchange / "mcp.delegate.json") in argv
        assert (exchange / "persona.delegate.md").exists()
        assert (exchange / "mcp.delegate.json").exists()
        assert runtime.account_dir(tenant, state_key="delegate:" + tenant) == str(home / ".claude-delegate")
        owner_argv = runtime.tenant_run_argv("delegate:" + owner, "delegate", args=("-p",))
        assert owner_argv[4:8] == ["--kind", "delegate", "--product", "bridge"]
        assert set(runtime._isolated_mcp_servers(tenant)) == {"send-telegram-file", "delegate-to-codex"}
        with patch.object(runtime, "codex_bridge_dir", return_value=None):
            assert set(runtime._isolated_mcp_servers(tenant)) == {"send-telegram-file"}

    def put(body, name=None):
        name = name or f"delegate-codex-{uuid.uuid4()}.json"
        path = exchange / "queue" / name
        path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        return name, path

    called = []
    def forward(text, wait_seconds=600, chat_id=None):
        called.append((text, chat_id))
        return True, "Ответ Codex"

    with patch.object(broker, "_delegate_request", side_effect=forward):
        name, path = put({"text": "задача"})
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        for _ in range(100):
            if (exchange / "result" / name).exists():
                break
            time.sleep(0.01)
        assert called == [("задача", int(tenant))]
        assert json.loads((exchange / "result" / name).read_text()) == {
            "done": True, "ok": True, "text": "Ответ Codex"}
        assert not path.exists()

    # Exercise the existing peer queue writer with the directory-bound id.
    peer = TMP / "peer"
    peer.mkdir()
    with patch.object(legacy, "codex_bridge_dir", return_value=peer), \
            patch.object(broker, "_delegate_request",
                         side_effect=lambda text, chat_id: legacy._delegate_request(text, wait_seconds=0, chat_id=chat_id)):
        name, _ = put({"text": "peer request"})
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        peer_queue = peer / "cross_delegate_queue"
        for _ in range(100):
            files = list(peer_queue.glob("*.json")) if peer_queue.exists() else []
            if files:
                break
            time.sleep(0.01)
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == {"chat_id": int(tenant), "text": "peer request"}
        (peer / "cross_delegate_result" / files[0].name).write_text(
            json.dumps({"done": True, "ok": True, "text": "accepted"}))
        for _ in range(100):
            if (exchange / "result" / name).exists():
                break
            time.sleep(0.01)
        assert json.loads((exchange / "result" / name).read_text())["ok"] is True

    with patch.object(broker, "_delegate_request", side_effect=forward):
        rejected = [b"{broken", b"x" * 65537, {"text": ""},
                    {"text": "x" * 32001}, {"text": "ok", "chat_id": int(owner)},
                    {"text": "ok", "target": owner}, b'{"text":"a","text":"b"}']
        for body in rejected:
            name, path = put(body)
            broker.process_tenant_delegate_queue(tenant, str(exchange))
            assert json.loads((exchange / "result" / name).read_text())["ok"] is False
            assert not path.exists()
        name, path = put({"text": "ok"}, "delegate-codex-bad.json")
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        assert json.loads((exchange / "result" / name).read_text())["ok"] is False
        assert not path.exists()
        # Requests addressed to the Claude side (written by the Codex bridge's tool) must stay untouched:
        # otherwise both bridges forward the same file and the task runs twice.
        other, other_path = put({"text": "for claude"}, f"delegate-claude-{uuid.uuid4()}.json")
        with patch.object(broker, "_delegate_request", side_effect=AssertionError("forwarded a delegate-claude request")):
            broker.process_tenant_delegate_queue(tenant, str(exchange))
        assert other_path.exists() and not (exchange / "result" / other).exists()
        other_path.unlink()
        outside = TMP / "outside"
        outside.write_text("secret")
        name = f"delegate-codex-{uuid.uuid4()}.json"
        (exchange / "queue" / name).symlink_to(outside)
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        assert outside.read_text() == "secret"
        assert json.loads((exchange / "result" / name).read_text())["ok"] is False
        name = f"delegate-codex-{uuid.uuid4()}.json"
        os.mkfifo(exchange / "queue" / name)
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        assert json.loads((exchange / "result" / name).read_text())["ok"] is False
        assert len(called) == 1

    gate = threading.Event()
    def slow(text, wait_seconds=600, chat_id=None):
        gate.wait(3)
        return True, "done"
    with patch.object(broker, "_delegate_request", side_effect=slow):
        first, _ = put({"text": "first"})
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        second, _ = put({"text": "second"})
        broker.process_tenant_delegate_queue(tenant, str(exchange))
        assert "занят" in json.loads((exchange / "result" / second).read_text())["text"]
        gate.set()
        for _ in range(100):
            if (exchange / "result" / first).exists():
                break
            time.sleep(0.01)
        assert (exchange / "result" / first).exists()

    result_dir = exchange / "result"
    result_dir.rename(exchange / "result-hidden")
    result_dir.symlink_to(TMP, target_is_directory=True)
    name, path = put({"text": "pending"})
    with patch.object(broker, "_delegate_request", side_effect=AssertionError("forwarded without result")):
        broker.process_tenant_delegate_queue(tenant, str(exchange))
    assert path.exists()
    result_dir.unlink()
    (exchange / "result-hidden").rename(result_dir)
    path.unlink()

    with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": str(exchange)}):
        request_id = str(uuid.uuid4())
        mcp._write_request(str(exchange), request_id, "hello")
        name = f"delegate-codex-{request_id}.json"
        assert json.loads((exchange / "queue" / name).read_text()) == {"text": "hello"}
        (exchange / "queue" / name).unlink()
        with patch.object(mcp, "POLL_INTERVAL_S", 0.01):
            def answer():
                for _ in range(100):
                    files = list((exchange / "queue").glob("delegate-*.json"))
                    if files:
                        (exchange / "result" / files[0].name).write_text(json.dumps({"done": True, "ok": True, "text": "ok"}))
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=answer)
            thread.start()
            assert mcp._delegate_request("hello", 1) == (True, "ok")
            thread.join()
        for file in (exchange / "queue").glob("delegate-*.json"):
            file.unlink()
        with patch.object(mcp, "POLL_TIMEOUT_S", 0.02), patch.object(mcp, "POLL_INTERVAL_S", 0.01):
            assert mcp._delegate_request("hello", 0)[0] is False
    with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": ""}):
        assert mcp._delegate_request("hello")[0] is False


def watchers_do_not_eat_each_other():
    """The file-send watcher and the delegation broker share queue/: each leaves the other's requests alone."""
    import tenant_exchange as files
    exchange = TMP / "shared" / ".exchange"
    for part in ("queue", "result", "outbox"):
        (exchange / part).mkdir(parents=True)
    (exchange / "outbox" / "f.txt").write_text("hello")
    file_id, delegate_id, junk = str(uuid.uuid4()), str(uuid.uuid4()), "junk.json"
    (exchange / "queue" / f"{file_id}.json").write_text(json.dumps(
        {"path": str(exchange / "outbox" / "f.txt"), "caption": "c"}))
    (exchange / "queue" / f"delegate-codex-{delegate_id}.json").write_text(json.dumps({"text": "task"}))
    (exchange / "queue" / junk).write_text("{}")
    sent = []
    with patch.object(broker, "_delegate_request", return_value=(True, "answered")):
        broker.process_tenant_delegate_queue("111111", str(exchange))
        for _ in range(100):
            if (exchange / "result" / f"delegate-codex-{delegate_id}.json").exists():
                break
            time.sleep(0.05)
        files.process_tenant_queue(111111, str(exchange),
                                   lambda chat, path, caption: (sent.append(os.path.basename(path)), {"ok": True})[1], 10**6)
    assert sent == ["f.txt"], sent
    delegate_result = json.loads((exchange / "result" / f"delegate-codex-{delegate_id}.json").read_text())
    file_result = json.loads((exchange / "result" / f"{file_id}.json").read_text())
    assert delegate_result["ok"] is True and delegate_result["text"] == "answered"
    assert file_result["ok"] is True
    # Whoever saw the junk name first refused it; neither watcher left it behind or touched the real requests.
    assert not any((exchange / "queue").iterdir())


if __name__ == "__main__":
    main()
    watchers_do_not_eat_each_other()
    print("CLOSED: isolated delegation routing and exchange boundary.")
