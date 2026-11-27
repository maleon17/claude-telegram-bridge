"""I5 exchange boundaries, using only temporary tenant directories."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_exchange_"))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001",
                  BRIDGE_STATE_FILE=str(TMP / "state.json"), BRIDGE_ISOLATION="off")
sys.path.insert(0, str(ROOT))
import runtime  # noqa: E402
import send_telegram_file_mcp as mcp  # noqa: E402
import telegram_api  # noqa: E402
import tenant_exchange as exchange_io  # noqa: E402


def main():
    tenant = "1000000002"
    other = "1000000003"
    exchange = TMP / ".exchange"
    for part in ("inbox", "outbox", "queue", "result"):
        (exchange / part).mkdir(parents=True, exist_ok=True)
    payload = exchange / "outbox" / "good.txt"
    payload.write_text("safe content")
    calls = []

    def send(chat_id, path, caption):
        calls.append((chat_id, Path(path).read_text(), caption, path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        return {"ok": True}

    def request(body, name=None):
        name = name or f"{uuid.uuid4()}.json"
        path = exchange / "queue" / name
        path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        exchange_io.process_tenant_queue(int(tenant), str(exchange), send, 32)
        assert not path.exists()
        return json.loads((exchange / "result" / name).read_text())

    good = request({"path": str(payload), "caption": "hello", "chat_id": int(other)})
    assert good == {"done": True, "ok": True, "text": "Файл отправлен."}
    assert calls[-1][:3] == (int(tenant), "safe content", "hello")
    assert not Path(calls[-1][3]).exists()
    outside = TMP / "outside.txt"
    outside.write_text("outside")
    (exchange / "outbox" / "link.txt").symlink_to(outside)
    (exchange / "outbox" / "linkdir").symlink_to(TMP, target_is_directory=True)
    denied = [
        {"path": str(exchange / "outbox" / "link.txt")},
        {"path": str(exchange / "outbox" / "linkdir" / "outside.txt")},
        {"path": str(exchange / "outbox" / ".." / "inbox" / "x")},
        {"path": str(outside)},
        {"path": str(exchange / "outbox" / "pipe")},
        {"path": str(exchange / "outbox" / "large")},
        {"path": str(payload), "unexpected": 1},
        {"path": str(payload), "caption": "x" * 1025},
    ]
    os.mkfifo(exchange / "outbox" / "pipe")
    (exchange / "outbox" / "large").write_bytes(b"x" * 33)
    for body in denied:
        assert request(body)["ok"] is False
    assert request(b"x" * (exchange_io.REQUEST_MAX_BYTES + 1))["ok"] is False
    assert request(b"{broken")["ok"] is False
    assert request(('{"path":' + json.dumps(str(payload)) + ',"path":"other"}').encode())["ok"] is False
    assert request({"path": str(payload)}, "bad.json")["ok"] is False
    assert len(calls) == 1
    # A FIFO or symlink in the queue itself must never block or be followed.
    fifo_name = f"{uuid.uuid4()}.json"
    os.mkfifo(exchange / "queue" / fifo_name)
    exchange_io.process_tenant_queue(int(tenant), str(exchange), send, 32)
    assert not (exchange / "queue" / fifo_name).exists()
    assert json.loads((exchange / "result" / fifo_name).read_text())["ok"] is False
    link_name = f"{uuid.uuid4()}.json"
    (exchange / "queue" / link_name).symlink_to(outside)
    exchange_io.process_tenant_queue(int(tenant), str(exchange), send, 32)
    assert not (exchange / "queue" / link_name).exists()
    assert outside.read_text() == "outside"
    assert json.loads((exchange / "result" / link_name).read_text())["ok"] is False
    result = exchange / "result"
    result.rename(exchange / "result-hidden")
    (exchange / "result").symlink_to(TMP)
    orphan = exchange / "queue" / f"{uuid.uuid4()}.json"
    orphan.write_text(json.dumps({"path": str(payload)}))
    count_before = len(calls)
    exchange_io.process_tenant_queue(int(tenant), str(exchange), send, 32)
    assert orphan.exists() and len(calls) == count_before and not (TMP / orphan.name).exists()
    (exchange / "result").unlink()
    (exchange / "result-hidden").rename(result)
    exchange_io.process_tenant_queue(int(tenant), str(exchange), send, 32)
    assert not orphan.exists() and len(calls) == count_before + 1

    provision = lambda chat_id, kind="main": {"exchange": str(exchange), "home": str(TMP)}
    with patch.object(runtime, "ensure_tenant", side_effect=provision), \
            patch.object(runtime, "codex_bridge_dir", return_value=None), \
            patch.object(runtime, "BRIDGE_ISOLATION", "on"), \
            patch.object(runtime, "ISOLATED_TENANTS", frozenset({tenant})), \
            patch.dict(os.environ, {"BRIDGE_ISOLATED_MCP_DIR": str(ROOT),
                                 "BRIDGE_TENANT_CONTROL_DIR": str(TMP / "control")}):
        servers = runtime._isolated_mcp_servers(tenant)
        assert set(servers) == {"send-telegram-file"}
        assert servers["send-telegram-file"]["env"] == {}
        runtime.ensure_tenant_control(tenant)
        runtime.ensure_tenant_control(tenant)
        persona = Path(runtime.tenant_persona_path(tenant)).read_text()
        assert persona.count(runtime.FILE_SEND_AGENTS_MARKER) == 1
        assert set(json.loads(Path(runtime.tenant_mcp_path(tenant)).read_text())["mcpServers"]) == {
            "send-telegram-file"}
        with patch.dict(os.environ, {"BRIDGE_ISOLATED_MCP_DIR": str(TMP)}):
            assert runtime._isolated_mcp_servers(tenant) == {}
            runtime.ensure_tenant_control(tenant)
            assert runtime.FILE_SEND_AGENTS_MARKER not in Path(runtime.tenant_persona_path(tenant)).read_text()

    # Isolated MCP uses exchange only and omits chat_id. Legacy request remains unchanged.
    with patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": str(exchange), "CHAT_ID": tenant}):
        req_id = str(uuid.uuid4())
        mcp._write_request(req_id, int(tenant), str(payload), "")
        assert json.loads((exchange / "queue" / f"{req_id}.json").read_text()) == {
            "path": str(payload), "caption": ""}
    with patch.object(mcp, "QUEUE_DIR", TMP / "legacy-queue"), \
            patch.object(mcp, "RESULT_DIR", TMP / "legacy-result"), \
            patch.dict(os.environ, {"TENANT_EXCHANGE_DIR": ""}):
        req_id = str(uuid.uuid4())
        mcp._write_request(req_id, int(tenant), str(payload), "")
        assert json.loads((mcp.QUEUE_DIR / f"{req_id}.json").read_text())["chat_id"] == int(tenant)

    # Incoming names, modes, retention and nonisolated storage.
    source = TMP / "incoming"
    source.write_bytes(b"abcd")
    with patch.object(telegram_api, "tenant_isolated", return_value=True), \
            patch.object(telegram_api, "ensure_tenant", side_effect=provision), \
            patch.object(telegram_api, "LOCAL_BOT_API", True), \
            patch.object(telegram_api, "tg_call", return_value={"ok": True, "result": {"file_path": str(source)}}):
        path = telegram_api.download_telegram_file(tenant, "file", filename_hint="../bad\x00name")
        assert Path(path).parent == exchange / "inbox" and Path(path).read_bytes() == b"abcd"
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o644
        assert ".." not in Path(path).name and "\x00" not in Path(path).name
        old = exchange / "inbox" / "old"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))
        extra = exchange / "inbox" / "extra"
        extra.write_bytes(b"z" * 8)
        os.utime(extra, (time.time() - 100, time.time() - 100))
        second = TMP / "incoming2"
        second.write_bytes(b"new")
        with patch.object(telegram_api, "INBOX_MAX_BYTES", 7), \
                patch.object(telegram_api, "tg_call", return_value={"ok": True, "result": {"file_path": str(second)}}):
            newer = telegram_api.download_telegram_file(tenant, "file2")
        assert Path(newer).exists() and not old.exists() and not extra.exists()
        collision = TMP / "incoming3"
        collision.write_bytes(b"different")
        fixed = type("Fixed", (), {"hex": "0" * 32})()
        with patch.object(telegram_api.uuid, "uuid4", return_value=fixed), \
                patch.object(telegram_api.time, "time", return_value=1234567890), \
                patch.object(telegram_api, "tg_call", return_value={"ok": True, "result": {"file_path": str(collision)}}):
            first = telegram_api.download_telegram_file(tenant, "same", filename_hint="x")
            collision.write_bytes(b"different again")
            try:
                telegram_api.download_telegram_file(tenant, "same", filename_hint="x")
                raise AssertionError("collision overwrote an existing upload")
            except telegram_api.AttachmentDownloadError:
                pass
            assert Path(first).read_bytes() == b"different"
    legacy_source = TMP / "legacy-incoming"
    legacy_source.write_bytes(b"legacy")
    with patch.object(telegram_api, "tenant_isolated", return_value=False), \
            patch.object(telegram_api, "LOCAL_BOT_API", True), \
            patch.object(telegram_api, "UPLOADS_DIR", str(TMP / "uploads_state")), \
            patch.object(telegram_api, "tg_call", return_value={"ok": True, "result": {"file_path": str(legacy_source)}}):
        legacy = telegram_api.download_telegram_file(other, "file")
        assert Path(legacy).parent == TMP / "uploads_state" / other


if __name__ == "__main__":
    main()
    print("CLOSED: tenant exchange requests and uploads stay within their isolated directories.")
