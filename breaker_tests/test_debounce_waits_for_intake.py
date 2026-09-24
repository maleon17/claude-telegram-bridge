"""A debounce expiry waits for a same-chat attachment still in intake.

A quick text prompt may reach the debounce queue while the serial intake
worker for the next update is still downloading an attachment.  The timer
must leave that partial batch alone until the attachment has joined it.
"""

import os
import sys
import tempfile
import time


TMP = tempfile.mkdtemp(prefix="breaker_debounce_intake_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handlers  # noqa: E402
import runtime  # noqa: E402


def _reset(chat_id):
    handlers.cancel_pending_batch(chat_id)
    with runtime.pending_batches_lock:
        runtime.pending_batches.clear()
        runtime.batch_timers.clear()
        runtime.pending_batch_generations.clear()
    with runtime.intake_lock:
        runtime.intake_queues.clear()
        runtime.intake_active.clear()
    runtime.busy_chats.clear()


def main():
    chat_id = "same-chat"
    old_debounce = handlers.BATCH_DEBOUNCE_S
    old_start_turn_thread = handlers._start_turn_thread
    events = []
    handlers.BATCH_DEBOUNCE_S = 0.03
    handlers._start_turn_thread = lambda key, prompt, state, **kw: events.append((key, prompt))
    try:
        # The first message is already prompt-ready.  The second one is the
        # same chat's in-progress, deliberately slow attachment download.
        with runtime.intake_lock:
            runtime.intake_active.add(chat_id)
        handlers.queue_prompt(chat_id, "быстрый текст", {})
        time.sleep(0.08)
        assert events == [], "timer started a turn before the attachment finished downloading"

        # This emulates the attachment worker completing and delivering its
        # prompt immediately before it removes itself from intake_active.
        handlers.queue_prompt(chat_id, "[Прикреплён файл: /tmp/slow.zip]", {})
        with runtime.intake_lock:
            runtime.intake_active.discard(chat_id)
        deadline = time.time() + 1
        while not events and time.time() < deadline:
            time.sleep(0.01)
        assert events == [(
            chat_id,
            "быстрый текст\n\n---\n\n[Прикреплён файл: /tmp/slow.zip]",
        )], f"expected one combined turn after intake drained, got {events!r}"
    finally:
        handlers.BATCH_DEBOUNCE_S = old_debounce
        handlers._start_turn_thread = old_start_turn_thread
        _reset(chat_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: debounce keeps a partial batch until same-chat intake finishes.")
        raise SystemExit(0)
