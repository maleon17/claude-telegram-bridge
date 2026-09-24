"""Owner config migration and /persona preserve exactly the intended persona.

The owner must move into accounts/<OWNER_ID> without losing their existing
Claude MCP configuration, and only replies to a bot-issued /persona snapshot
may replace that file.
"""

import os
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_persona_claude_")
os.environ["HOME"] = TMP
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import handlers  # noqa: E402
import runtime  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _prepare_owner_home():
    _write(os.path.join(TMP, ".claude", "CLAUDE.md"), "OWNER PERSONA MARKER\n")
    _write(os.path.join(TMP, ".claude", ".credentials.json"), "credentials")
    _write(
        os.path.join(TMP, ".claude.json"),
        '{"mcpServers": {"owner-marker": {"command": "marker"}}}\n',
    )


def main():
    assert hasattr(handlers, "handle_persona_reply"), "/persona reply handler is missing"
    _prepare_owner_home()
    owner_dir = runtime.account_dir(runtime.OWNER_ID)
    assert owner_dir is not None
    persona_path = os.path.join(owner_dir, "CLAUDE.md")
    assert "OWNER PERSONA MARKER" in open(persona_path, encoding="utf-8").read()
    config = open(os.path.join(owner_dir, ".claude.json"), encoding="utf-8").read()
    assert "owner-marker" in config
    assert "delegate-to-codex" in config and "send-telegram-file" in config
    credentials = os.path.join(owner_dir, ".credentials.json")
    assert os.path.islink(credentials)
    assert os.path.realpath(credentials) == os.path.join(TMP, ".claude", ".credentials.json")

    sent = []
    documents = []
    original_send = handlers.send_message
    original_document = handlers.send_document
    original_download = handlers.download_telegram_file
    handlers.send_message = lambda chat_id, text, **kw: sent.append((chat_id, text)) or {
        "ok": True, "result": {"message_id": 71 + len(sent)}
    }
    handlers.send_document = lambda chat_id, path, caption=None: documents.append(
        (chat_id, open(path, encoding="utf-8").read(), caption)
    ) or {"ok": True, "result": {"message_id": 99}}
    try:
        _write(persona_path, "small current persona")
        assert handlers.handle_command(runtime.OWNER_ID, "/persona", {})
        # account_dir() idempotently appends the send-telegram-file section
        # (marker-guarded) on every call, including this read -- the owner's
        # own content must still come through unmodified, as a prefix.
        assert sent[-1][1].startswith("small current persona")
        snapshot_id = 72
        assert handlers.handle_persona_reply(runtime.OWNER_ID, {
            "text": "replacement text",
            "reply_to_message": {"message_id": snapshot_id},
        })
        assert open(persona_path, encoding="utf-8").read() == "replacement text"

        _write(persona_path, "x" * (runtime.MAX_MSG_LEN + 1))
        assert handlers.handle_command(runtime.OWNER_ID, "/persona", {})
        assert documents[-1][1].startswith("x" * (runtime.MAX_MSG_LEN + 1))
        assert documents[-1][2] == "Текущая персона"

        uploaded = os.path.join(TMP, "uploaded.md")
        _write(uploaded, "file replacement")
        handlers.download_telegram_file = lambda *args, **kwargs: uploaded
        assert handlers.handle_persona_reply(runtime.OWNER_ID, {
            "document": {"file_id": "file", "file_name": "persona.md"},
            "reply_to_message": {"message_id": 99},
        })
        assert open(persona_path, encoding="utf-8").read() == "file replacement"

        before = open(persona_path, encoding="utf-8").read()
        assert not handlers.handle_persona_reply(runtime.OWNER_ID, {
            "text": "ordinary reply", "reply_to_message": {"message_id": 123456},
        })
        assert open(persona_path, encoding="utf-8").read() == before

        assert handlers.handle_persona_reply(runtime.OWNER_ID, {
            "text": "   ", "reply_to_message": {"message_id": 99},
        })
        assert open(persona_path, encoding="utf-8").read() == before
        assert "пуст" in sent[-1][1].lower()
        _write(uploaded, "")
        assert handlers.handle_persona_reply(runtime.OWNER_ID, {
            "document": {"file_id": "empty", "file_name": "persona.md"},
            "reply_to_message": {"message_id": 99},
        })
        assert open(persona_path, encoding="utf-8").read() == before
        assert "пуст" in sent[-1][1].lower()

        assert handlers.handle_command(runtime.OWNER_ID, "/persona reset", {})
        expected = open(os.path.join(ROOT, "personality.example.md"), encoding="utf-8").read()
        assert open(persona_path, encoding="utf-8").read() == expected
    finally:
        handlers.send_message = original_send
        handlers.send_document = original_document
        handlers.download_telegram_file = original_download


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: owner migration and /persona replies preserve the intended persona.")
        raise SystemExit(0)
