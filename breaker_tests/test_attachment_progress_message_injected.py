"""A "downloading attachment" progress message must not orphan itself
when the message is injected into an already-running turn.

bridge.py sends a "downloading attachment..." status message, then stashes
its message_id in pending_progress_msg_ids so the eventual turn can turn it
into its own progress display. That recycling only happens in
_new_turn_accumulator, which only runs when _ensure_chat_process spawns a
FRESH process. If the target chat already has a live process, the prompt
is injected (live-steering) into that in-progress turn instead, and no new
accumulator ever runs -- the message would sit showing "downloading..."
until an unrelated later turn happens to pick it up. _ensure_chat_process
must finalize it itself on the reuse path.
"""

import os
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_attachment_progress_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chat_process  # noqa: E402
import runtime  # noqa: E402


class _FakeProc:
    def poll(self):
        return None  # still alive


def _signature():
    return chat_process._chat_process_signature(None, None, None, None, None)


def main():
    chat_id = "live-chat"
    calls = []
    original_tg_call = chat_process.tg_call
    chat_process.tg_call = lambda method, params: calls.append((method, params)) or {"ok": True}
    try:
        record = {"proc": _FakeProc(), "signature": _signature()}
        with runtime.chat_procs_lock:
            runtime.chat_procs[chat_id] = record

        # Case 1: an attachment status message is pending for this chat when
        # a prompt gets injected into the already-live process.
        with runtime.pending_progress_lock:
            runtime.pending_progress_msg_ids[chat_id] = 555
        got = chat_process._ensure_chat_process(
            chat_id, None, None, None, None, None, {}, output_chat_id=987654,
        )
        assert got is record, "must reuse the live process, not spawn a new one"
        assert calls == [(
            "editMessageText",
            {
                "chat_id": 987654, "message_id": 555,
                "text": "✅ Вложение получено — добавлено к текущему ходу.",
            },
        )], f"expected the orphaned progress message to be finalized, got {calls!r}"
        with runtime.pending_progress_lock:
            assert chat_id not in runtime.pending_progress_msg_ids, \
                "finalized message_id must be popped, not left for a later turn to reuse"

        # Case 2: the ordinary case -- no attachment, nothing pending, reuse
        # must not send a spurious edit.
        calls.clear()
        got = chat_process._ensure_chat_process(
            chat_id, None, None, None, None, None, {}, output_chat_id=987654,
        )
        assert got is record
        assert calls == [], f"reuse without a pending progress message must not call the API, got {calls!r}"
    finally:
        chat_process.tg_call = original_tg_call
        with runtime.chat_procs_lock:
            runtime.chat_procs.pop(chat_id, None)
        with runtime.pending_progress_lock:
            runtime.pending_progress_msg_ids.pop(chat_id, None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: an injected turn finalizes its chat's orphaned attachment-progress message.")
        raise SystemExit(0)
