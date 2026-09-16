"""Regression coverage for review findings C01-C09, T02 and T03.

Run directly with:
    python3 breaker_tests/test_review_task2.py
No live services, Telegram, or Claude processes are touched.
"""

import json
import os
import sys
import tempfile
import threading
import time


_TMP = tempfile.mkdtemp(prefix="breaker_review_task2_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(_TMP, "state.json")
os.environ.pop("SERVICE_NAME", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402
import bridge_exec  # noqa: E402
import chat_process  # noqa: E402
import handlers  # noqa: E402
import runtime  # noqa: E402
import telegram_api  # noqa: E402
from state_store import (  # noqa: E402
    get_pending_prompt, get_permission_mode, get_session, pending_prompt_is_current,
    projects_dir_for, set_pending_prompt, set_session,
)


class Capture:
    """Swap module attributes for the duration of a with-block."""

    def __init__(self, **targets):
        self.targets = targets
        self.saved = []

    def __enter__(self):
        for key, value in self.targets.items():
            module_name, attr = key.split("__", 1)
            module = globals()[module_name]
            self.saved.append((module, attr, getattr(module, attr)))
            setattr(module, attr, value)
        return self

    def __exit__(self, *exc):
        for module, attr, value in reversed(self.saved):
            setattr(module, attr, value)


def _messages():
    sent = []
    return sent, (lambda chat_id, text, *a, **kw: sent.append((chat_id, text)) or {"ok": True})


def test_c01_mode_is_case_insensitive_and_canonical():
    state = {}
    sent, fake = _messages()
    with Capture(handlers__send_message=fake):
        assert handlers.handle_command("42", "/mode ACCEPTedits", state)
    assert get_permission_mode(state, "42") == "acceptEdits"
    assert "acceptEdits" in sent[-1][1]
    print("[C01] /mode acceptEdits is selectable in any case")


def test_c02_resume_requires_unambiguous_prefix():
    state = {}
    chat_id = "resume-chat"
    pdir = projects_dir_for(runtime.account_dir(chat_id), None)
    os.makedirs(pdir, exist_ok=True)
    first = "aabbccdd-1111-4111-8111-111111111111"
    second = "aabbccdd-2222-4222-8222-222222222222"
    for sid in (first, second):
        open(os.path.join(pdir, sid + ".jsonl"), "w").close()
    sent, fake = _messages()
    with Capture(handlers__send_message=fake, handlers___stop_chat_process=lambda key: None):
        handlers.handle_command(chat_id, "/resume aabbccdd", state)
        assert get_session(state, chat_id) is None
        assert first in sent[-1][1] and second in sent[-1][1]
        handlers.handle_command(chat_id, "/resume aabb*", state)
        assert get_session(state, chat_id) is None
        handlers.handle_command(chat_id, "/resume aabbccdd-2", state)
        assert get_session(state, chat_id) == second
    print("[C02] ambiguous or glob /resume never picks a session silently")


def test_c03_pending_approval_bound_to_session():
    state = {}
    set_session(state, "7", "old-session")
    set_pending_prompt(state, "7", "old prompt", session_id="old-session")
    assert pending_prompt_is_current(state, "7")
    set_session(state, "7", "new-session")
    assert not pending_prompt_is_current(state, "7")

    set_pending_prompt(state, "7", "prompt", session_id="new-session")
    sent, fake = _messages()
    with Capture(handlers__send_message=fake, handlers___stop_chat_process=lambda key: None):
        handlers.handle_command("7", "/new", state)
    assert get_pending_prompt(state, "7") is None
    print("[C03] /new drops the pending approval; a session switch invalidates it")


def test_c03_replaced_process_cannot_leave_pending_approval():
    state = {}
    set_session(state, "8", "fresh-session")

    class OldProc:
        pass

    ts = chat_process._new_turn_accumulator(state, "8")
    ts["final_text"] = "done"
    ts["denials"] = [{"tool_name": "Bash", "tool_input": {}}]
    sent, fake = _messages()
    with Capture(
        chat_process__send_rich=lambda *a, **kw: {"ok": True},
        chat_process__edit_rich=lambda *a, **kw: {"ok": True},
        chat_process__send_message=fake,
        chat_process__write_last_turn=lambda *a, **kw: None,
    ):
        chat_process._deliver_turn_result("8", state, ts, "denied prompt", OldProc())
    assert get_pending_prompt(state, "8") is None
    print("[C03] a late result from a replaced process leaves no approval behind")


def test_c04_draining_refuses_new_work_and_idle_is_atomic():
    state = {}
    sent, fake = _messages()
    runtime.draining.set()
    try:
        with Capture(
            handlers__send_message=fake,
            handlers___start_turn_thread=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("started")),
        ):
            assert handlers.spawn_turn("d1", "hi", state) is False
            assert handlers.queue_prompt("d1", "hi", state) is False
        assert "d1" not in runtime.busy_chats
        assert not runtime.pending_batches.get("d1")
        assert all(text == runtime.DRAINING_TEXT for _, text in sent)
    finally:
        runtime.draining.clear()

    assert bridge._bridge_is_idle()
    runtime.pending_batches["d2"] = ["queued"]
    assert not bridge._bridge_is_idle()
    runtime.pending_batches.pop("d2")
    runtime.intake_active.add("d3")
    assert not bridge._bridge_is_idle()
    runtime.intake_active.discard("d3")
    print("[C04] draining refuses turns/batches explicitly; pending work blocks restart")


def test_c04_restart_request_waits_while_batch_pending():
    runtime.pending_batches["waiting"] = ["msg"]
    with open(runtime.RESTART_SIGNAL_FILE, "w") as handle:
        json.dump({"chat_id": 1}, handle)
    ticks = []

    def fake_sleep(_seconds):
        ticks.append(1)
        if len(ticks) > 3:
            raise StopIteration

    try:
        with Capture(bridge__time=type("T", (), {"sleep": staticmethod(fake_sleep)})):
            try:
                bridge._restart_watcher_loop({})
            except StopIteration:
                pass
        assert os.path.exists(runtime.RESTART_SIGNAL_FILE)
        assert not runtime.draining.is_set()
    finally:
        runtime.pending_batches.pop("waiting", None)
        os.remove(runtime.RESTART_SIGNAL_FILE)
    print("[C04] a pending debounce batch keeps the restart request waiting")


def test_c07_slow_message_does_not_block_intake_and_keeps_order():
    started, release = threading.Event(), threading.Event()
    processed = []

    def slow_process(msg, state):
        if msg["text"] == "slow":
            started.set()
            release.wait(2)
        processed.append((msg["chat"]["id"], msg["text"]))

    with Capture(bridge___process_message=slow_process):
        with runtime.intake_lock:
            bridge._enqueue_intake({"chat": {"id": 1}, "text": "slow"}, {})
            bridge._enqueue_intake({"chat": {"id": 1}, "text": "after"}, {})
        assert started.wait(2)
        begun = time.monotonic()
        with runtime.intake_lock:
            bridge._enqueue_intake({"chat": {"id": 2}, "text": "other chat"}, {})
        assert time.monotonic() - begun < 0.5
        deadline = time.time() + 2
        while (2, "other chat") not in processed and time.time() < deadline:
            time.sleep(0.01)
        assert (2, "other chat") in processed
        assert (1, "after") not in processed
        release.set()
        deadline = time.time() + 2
        while runtime.intake_active and time.time() < deadline:
            time.sleep(0.01)
    assert [text for chat, text in processed if chat == 1] == ["slow", "after"]
    print("[C07] a slow message never blocks intake or other chats; order kept per chat")


def test_c07_stop_is_recognised_for_urgent_handling():
    state = {}
    with Capture(bridge__load_whitelist=lambda: {"5"}):
        msg = {"chat": {"id": 1000000001}, "from": {"id": 5}, "text": "/stop"}
        state["1000000001"] = {"account_status": "ready"}
        assert bridge._is_urgent_stop(msg, state)
        assert not bridge._is_urgent_stop(dict(msg, text="/status"), state)
        assert not bridge._is_urgent_stop(dict(msg, **{"from": {"id": 6}}), state)
    print("[C07] /stop from a ready chat bypasses the intake queue")


def test_c08_concurrent_requests_get_isolated_results():
    request_dir = os.path.join(_TMP, "req")
    os.environ["BRIDGE_EXEC_EXTERNAL_REQUEST_FILE"] = request_dir
    os.environ["BRIDGE_EXEC_STATE_FILE"] = os.environ["BRIDGE_STATE_FILE"]
    first = bridge_exec.submit_request({"text": "one"})
    second = bridge_exec.submit_request({"text": "two"})
    assert first != second
    names = sorted(os.listdir(bridge_exec.external_request_dir()))
    assert names == sorted([first + ".json", second + ".json"])
    assert bridge_exec.external_result_dir() == runtime.EXTERNAL_RESULT_DIR

    state = {}
    last_turns = []
    sent, fake = _messages()
    runtime.busy_chats.add(handlers.delegate_key(1000000001))
    try:
        with Capture(
            handlers__send_message=fake,
            handlers__write_last_turn=lambda *a, **kw: last_turns.append(a),
        ):
            assert handlers.start_delegate_turn(1000000001, "two", state, request_id=second) is False
    finally:
        runtime.busy_chats.discard(handlers.delegate_key(1000000001))
    assert last_turns == []
    busy_result = bridge_exec.poll_request_result(second, timeout_s=1, poll_interval=0.01)
    assert busy_result["ok"] is False and "Уже выполняю" in busy_result["text"]
    assert not os.path.exists(os.path.join(runtime.EXTERNAL_RESULT_DIR, first + ".json"))
    print("[C08] a rejected second caller gets its own result; the first is untouched")


def test_t02_long_rich_text_is_split_not_truncated():
    body = "\n".join(["```python"] + ["x = 1  # " + "y" * 80] * 800 + ["```", "tail"])
    parts = telegram_api.split_rich_text(body, limit=4000)
    assert len(parts) > 1
    assert all(len(part) <= 4000 for part in parts)
    assert all(part.count("```") % 2 == 0 for part in parts)
    assert parts[-1].endswith("tail")

    calls = []

    def fake_tg(method, params=None, timeout=20):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 1}}

    original = telegram_api.split_rich_text
    text = "a" * 3000 + "\n" + "b" * 3000
    chunks = original(text, limit=4000)
    with Capture(
        telegram_api__tg_call=fake_tg,
        telegram_api__split_rich_text=lambda value, limit=None: original(value, limit=4000),
    ):
        assert telegram_api.send_rich(1, text).get("ok")
    sent = [params["rich_message"]["markdown"] for method, params in calls if method == "sendRichMessage"]
    assert "".join(sent).replace("\n", "") == text.replace("\n", "")
    assert len(sent) == len(chunks) == 2
    print("[T02] long rich answers are delivered in full across messages")


def test_t02_edit_rich_sends_overflow():
    calls = []

    def fake_tg(method, params=None, timeout=20):
        calls.append(method)
        return {"ok": True, "result": {"message_id": 1}}

    original = telegram_api.split_rich_text
    with Capture(
        telegram_api__tg_call=fake_tg,
        telegram_api__split_rich_text=lambda text, limit=None: original(text, limit=10),
    ):
        assert telegram_api.edit_rich(1, 99, "line one\nline two\nline three").get("ok")
    assert calls[0] == "editMessageText" and calls.count("sendRichMessage") >= 1
    print("[T02] an edited final carries overflow over as new messages")


def test_t03_final_is_persisted_retried_and_not_duplicated():
    state = {}
    attempts = []
    signals = []

    def failing_send(chat_id, text, *a, **kw):
        attempts.append(text)
        return {"ok": False, "error_code": 429}

    delivery = {
        "chat_id": 9, "text": "answer", "parts": ["answer"], "next_part": 0,
        "edit_message_id": None, "signal_last_turn": True, "delegated": True,
        "stopped": False, "request_id": None, "attempts": 0, "next_retry_at": 0,
    }
    chat_process.set_pending_delivery(state, "9", delivery)
    with Capture(
        chat_process__send_rich=failing_send,
        chat_process__write_last_turn=lambda *a, **kw: signals.append(kw.get("ok")),
    ):
        assert chat_process.deliver_pending_final(state, "9", delivery) is False
    pending = state["9"]["pending_delivery"]
    assert pending["attempts"] == 1 and pending["next_retry_at"] > time.time()
    assert signals == [False]  # caller is told right away that delivery is pending

    chat_process.retry_pending_finals(state)  # not due yet
    assert len(attempts) == 1

    entered, release = threading.Event(), threading.Event()

    def slow_ok(chat_id, text, *a, **kw):
        attempts.append(text)
        entered.set()
        release.wait(2)
        return {"ok": True}

    state["9"]["pending_delivery"]["next_retry_at"] = 0
    with Capture(
        chat_process__send_rich=slow_ok,
        chat_process__write_last_turn=lambda *a, **kw: signals.append(kw.get("ok")),
    ):
        worker = threading.Thread(target=chat_process.retry_pending_finals, args=(state,))
        worker.start()
        assert entered.wait(2)
        assert chat_process.deliver_pending_final(state, "9", state["9"]["pending_delivery"]) is False
        release.set()
        worker.join(2)
    assert len(attempts) == 2
    assert "pending_delivery" not in state["9"]
    assert signals[-1] is True
    print("[T03] undelivered finals are kept, retried with backoff, never sent twice")


def test_t03_permanent_failure_gives_up():
    state = {}
    signals = []
    delivery = {
        "chat_id": 10, "text": "answer", "parts": ["answer"], "next_part": 0,
        "edit_message_id": None, "signal_last_turn": True, "delegated": False,
        "stopped": False, "request_id": None, "attempts": 0, "next_retry_at": 0,
    }
    chat_process.set_pending_delivery(state, "10", delivery)
    with Capture(
        chat_process__send_rich=lambda *a, **kw: {"ok": False, "description": "blocked"},
        chat_process__write_last_turn=lambda *a, **kw: signals.append(kw.get("ok")),
    ):
        for _ in range(chat_process.DELIVERY_MAX_ATTEMPTS + 2):
            pending = state["10"].get("pending_delivery")
            if not pending:
                break
            pending["next_retry_at"] = 0
            chat_process.retry_pending_finals(state)
    assert "pending_delivery" not in state["10"]
    assert signals[-1] is False
    print("[T03] a permanently rejected final stops retrying and is reported undelivered")


def test_c05_c06_c09_installer_keeps_secrets_out_of_the_unit():
    with open(os.path.join(ROOT, "claude-telegram-bridge.service.example")) as handle:
        unit = handle.read()
    assert "__BOT_TOKEN__" not in unit and "TELEGRAM_BOT_TOKEN=" not in unit
    assert "EnvironmentFile=__ENV_FILE__" in unit
    with open(os.path.join(ROOT, "setup.sh")) as handle:
        setup = handle.read()
    assert 'UNIT_FILE="/tmp/' not in setup
    assert "mktemp -d" in setup and "install -m 600" in setup
    assert "SERVICE_NAME=%s.service" in setup and "CLAUDE_BIN=%s" in setup
    assert "visudo -cf" in setup
    assert "^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$" in setup
    print("[C05/C06/C09] installer writes a 600 env file, validated sudoers, real CLI path")


def test_c09_voice_without_whisper_gets_explicit_reply():
    state = {"1000000001": {"account_status": "ready"}}
    sent, fake = _messages()
    routed = []
    msg = {
        "chat": {"id": 1000000001}, "from": {"id": 1000000001},
        "voice": {"file_id": "v"},
    }
    with Capture(
        bridge__send_message=fake,
        bridge__voice_transcription_available=lambda: False,
        bridge__download_telegram_file=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("downloaded")),
        bridge__route_prompt=lambda *a, **kw: routed.append(a),
        bridge__load_whitelist=lambda: {"1000000001"},
    ):
        bridge._process_message(msg, state)
    assert routed == []
    assert "faster-whisper" in sent[-1][1]
    print("[C09] voice without faster-whisper gets a clear answer instead of a silent failure")


if __name__ == "__main__":
    test_c01_mode_is_case_insensitive_and_canonical()
    test_c02_resume_requires_unambiguous_prefix()
    test_c03_pending_approval_bound_to_session()
    test_c03_replaced_process_cannot_leave_pending_approval()
    test_c04_draining_refuses_new_work_and_idle_is_atomic()
    test_c04_restart_request_waits_while_batch_pending()
    test_c07_slow_message_does_not_block_intake_and_keeps_order()
    test_c07_stop_is_recognised_for_urgent_handling()
    test_c08_concurrent_requests_get_isolated_results()
    test_t02_long_rich_text_is_split_not_truncated()
    test_t02_edit_rich_sends_overflow()
    test_t03_final_is_persisted_retried_and_not_duplicated()
    test_t03_permanent_failure_gives_up()
    test_c05_c06_c09_installer_keeps_secrets_out_of_the_unit()
    test_c09_voice_without_whisper_gets_explicit_reply()
    print("\nCLOSED: review task 2 findings are covered.")
