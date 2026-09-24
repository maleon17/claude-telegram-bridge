"""Regression coverage for the local Telegram Bot API transport.

The test uses the real bridge modules, but replaces only network-facing
functions.  It verifies that a local getFile absolute path is moved into the
chat upload directory, cloud oversize files fail with a structured reason,
file:// sends do not retry after an unknown transport failure, and the local
download status message becomes the normal progress card.
"""

import os
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_local_bot_api_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")
os.environ["TELEGRAM_API_URL"] = "http://127.0.0.1:8081/"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402
import chat_process  # noqa: E402
import runtime  # noqa: E402
import telegram_api  # noqa: E402


class Replace:
    def __init__(self, module, **values):
        self.module = module
        self.values = values
        self.old = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.old[name] = getattr(self.module, name)
            setattr(self.module, name, value)

    def __exit__(self, *_exc):
        for name, value in self.old.items():
            setattr(self.module, name, value)


def main():
    assert runtime.telegram_api_config("") == ("https://api.telegram.org", False)
    assert runtime.telegram_api_config("http://127.0.0.1:8081/") == ("http://127.0.0.1:8081", True)
    assert runtime.LOCAL_BOT_API
    assert runtime.MAX_DOCUMENT_BYTES == 2000 * 1024 * 1024
    print("[1/6] local URL normalization and the 2 GB local limit")

    source = os.path.join(TMP, "telegram-server-file.bin")
    with open(source, "wb") as handle:
        handle.write(b"local data")
    uploads = os.path.join(TMP, "uploads")
    with Replace(telegram_api, UPLOADS_DIR=uploads, tg_call=lambda *a, **k: {
        "ok": True, "result": {"file_path": source},
    }):
        stored = telegram_api.download_telegram_file("42", "file-id", "safe.bin")
    assert os.path.exists(stored) and open(stored, "rb").read() == b"local data"
    assert not os.path.exists(source), "local absolute Bot API path must be moved, not HTTP-downloaded"
    print("[2/6] local getFile absolute path moves into the chat directory")

    try:
        with Replace(telegram_api, LOCAL_BOT_API=False):
            telegram_api.download_telegram_file(
                "42", "too-big", file_size=runtime.TELEGRAM_CLOUD_FILE_MAX_BYTES + 1,
            )
    except telegram_api.AttachmentDownloadError as exc:
        assert exc.reason == "too_big_for_cloud"
        assert "20 МБ" in str(exc)
    else:
        raise AssertionError("oversize cloud file was not rejected structurally")
    print("[3/6] cloud oversize file exposes too_big_for_cloud")

    payload = os.path.join(TMP, "out.bin")
    with open(payload, "wb") as handle:
        handle.write(b"x")
    calls = []
    with Replace(
        telegram_api,
        tg_call=lambda *a, **k: calls.append((a, k)) or {"ok": False, "error": "timed out"},
        _multipart_request=lambda *a, **k: (_ for _ in ()).throw(AssertionError("unsafe retry")),
    ):
        result = telegram_api.send_document("42", payload)
    assert not result["ok"] and len(calls) == 1
    assert calls[0][0][1]["document"].startswith("file://")
    print("[4/6] unknown local send failure never retries as multipart")

    sent, prompts, edits = [], [], []
    old_pending = dict(runtime.pending_progress_msg_ids)
    runtime.pending_progress_msg_ids.clear()
    message = {
        "chat": {"id": "42"}, "from": {"id": "1000000001"},
        "document": {"file_id": "doc", "file_name": "x.txt", "file_size": 1},
    }
    try:
        with Replace(
            bridge,
            send_message=lambda chat_id, text, *a, **k: sent.append((chat_id, text)) or {
                "ok": True, "result": {"message_id": 77},
            },
            download_telegram_file=lambda *a, **k: "/tmp/x.txt",
            handle_onboarding=lambda *a, **k: False,
            route_prompt=lambda chat_id, prompt, state: prompts.append((chat_id, prompt)),
        ):
            bridge._process_message(message, {})
        assert sent[0][1] == "📥 Загружаю вложение…"
        assert "/tmp/x.txt" in prompts[0][1]
        ts = chat_process._new_turn_accumulator({}, "42")
        assert ts["progress_msg_id"] == 77
        with Replace(chat_process, tg_call=lambda method, params, **k: edits.append((method, params)) or {"ok": True}):
            chat_process._flush_draft("42", ts, force=True)
        assert edits[0][0] == "editMessageText" and edits[0][1]["message_id"] == 77
    finally:
        runtime.pending_progress_msg_ids.clear()
        runtime.pending_progress_msg_ids.update(old_pending)
    print("[5/6] download status is reused as the thinking card")

    sent, prompts = [], []
    failure = telegram_api.AttachmentDownloadError("too_big_for_cloud", "Файл не скачан: лимит 20 МБ.")
    with Replace(
        bridge,
        send_message=lambda chat_id, text, *a, **k: sent.append(text) or {"ok": True},
        download_telegram_file=lambda *a, **k: (_ for _ in ()).throw(failure),
        handle_onboarding=lambda *a, **k: False,
        route_prompt=lambda chat_id, prompt, state: prompts.append(prompt),
    ):
        bridge._process_message(message, {})
    assert sent[1] == str(failure)
    # The prompt note must carry the actual explanation, not the bare
    # machine-readable reason code -- Claude gets "лимит 20 МБ", not a
    # meaningless "too_big_for_cloud" token it has to guess at.
    assert "too_big_for_cloud" not in prompts[0]
    assert "[Файл не скачан: лимит 20 МБ." in prompts[0]
    assert "Не ищи его на диске.]" in prompts[0]
    print("[6/6] attachment failures reach both the user and Claude's prompt")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nSTILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("\nCLOSED: local Bot API transport uses safe file paths, limits, errors, and progress reuse.")
        raise SystemExit(0)
