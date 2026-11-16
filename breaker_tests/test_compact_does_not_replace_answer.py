"""An automatic compaction during a normal turn must not replace the answer."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="breaker_compact_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(_TMP, "state.json")
os.environ.pop("SERVICE_NAME", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import chat_process  # noqa: E402
from state_store import set_session  # noqa: E402


class Proc:
    pass


def _deliver(final_text, compact_outcome):
    state = {}
    set_session(state, "8", "s")
    ts = chat_process._new_turn_accumulator(state, "8")
    ts["final_text"] = final_text
    ts["compact_outcome"] = compact_outcome
    delivered = []
    saved = {k: getattr(chat_process, k) for k in ("deliver_pending_final", "send_rich", "edit_rich", "write_last_turn")}
    chat_process.deliver_pending_final = lambda st, cid, delivery: delivered.append(delivery["text"]) or True
    chat_process.send_rich = chat_process.edit_rich = lambda *a, **kw: {"ok": True}
    chat_process.write_last_turn = lambda *a, **kw: None
    try:
        chat_process._deliver_turn_result("8", state, ts, "prompt", Proc())
    finally:
        for key, value in saved.items():
            setattr(chat_process, key, value)
    return ts, delivered[0]


def test_answer_wins_over_compaction_notice():
    _, text = _deliver("the real answer", "🗜 Контекст сессии сжат.")
    assert text.startswith("the real answer")
    assert "сжат" not in text


def test_compaction_notice_used_when_turn_has_no_text():
    _, text = _deliver("", "🗜 Контекст сессии сжат.")
    assert text.startswith("🗜 Контекст сессии сжат.")


def test_late_draft_flush_after_delivery_is_ignored():
    ts, _ = _deliver("answer", None)
    calls = []
    saved = chat_process.tg_call
    chat_process.tg_call = lambda *a, **kw: calls.append(a) or {"ok": True}
    try:
        ts["draft_thought"] = "🗜 Сжимаю контекст сессии... (30с)"
        ts["progress_msg_id"] = 5
        chat_process._flush_draft("8", ts, force=True)
    finally:
        chat_process.tg_call = saved
    assert calls == []
