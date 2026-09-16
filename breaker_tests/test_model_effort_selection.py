"""Regression coverage for Claude model and effort selection.

Run directly with:
    python3 breaker_tests/test_model_effort_selection.py
"""

import os
import sys
import tempfile


_TMP = tempfile.mkdtemp(prefix="breaker_model_effort_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(_TMP, "state.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chat_process  # noqa: E402
import handlers  # noqa: E402
from state_store import get_effort, get_model, set_effort, set_model  # noqa: E402


def test_model_forms_and_errors():
    expected = {
        "claude-opus-5": "claude-opus-5",
        "claude-opus-4-7": "claude-opus-4-7",
        "opus-5": "claude-opus-5",
        "opus 5": "claude-opus-5",
        "opus 4.7": "claude-opus-4-7",
        "opus-4-7": "claude-opus-4-7",
        "opus 4-7": "claude-opus-4-7",
        "Opus 5": "claude-opus-5",
        "opus": "claude-opus-5",
        "claude-opus": "claude-opus-5",
        "fable 5.1": "claude-fable-5-1",
        "claude-fable-5-1": "claude-fable-5-1",
        "DEFAULT": None,
    }
    for spec, model_id in expected.items():
        assert handlers.resolve_model_spec(spec) == model_id, spec
    for spec in ("unknown", "opus 9", "claude-unknown-5"):
        try:
            handlers.resolve_model_spec(spec)
        except ValueError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"{spec!r} should be rejected")
    picker = handlers.render_model_picker("claude-opus-5", "xhigh")
    assert picker.splitlines()[1] == "● Opus 5 — `/model claude-opus-5`"
    print("[1/4] all documented model forms resolve and bad choices fail")


def test_effort_validation():
    assert handlers.resolve_effort_spec("claude-opus-5", "XHIGH") == "xhigh"
    assert handlers.resolve_effort_spec("claude-opus-4-6", "max") == "max"
    assert handlers.resolve_effort_spec("claude-opus-4-5", "default") is None
    assert handlers.resolve_effort_spec(None, "max") == "max"
    assert handlers._effort_label("claude-sonnet-4-5", None) == "не поддерживается"
    for model_id, effort in (("claude-opus-4-6", "xhigh"), ("claude-sonnet-4-5", "low")):
        try:
            handlers.resolve_effort_spec(model_id, effort)
        except ValueError as exc:
            assert "недоступна" in str(exc)
        else:
            raise AssertionError(f"{effort} must be unavailable for {model_id}")
    print("[2/4] effort is validated against the selected model")


def test_model_change_clears_unsupported_effort():
    state = {}
    sent = []
    original_send_message = handlers.send_message
    handlers.send_message = lambda chat_id, text: sent.append((chat_id, text))
    try:
        set_model(state, "chat", "claude-opus-5")
        set_effort(state, "chat", "xhigh")
        assert handlers.handle_command("chat", "/model opus 4.6", state)
    finally:
        handlers.send_message = original_send_message
    assert get_model(state, "chat") == "claude-opus-4-6"
    assert get_effort(state, "chat") is None
    assert "сброшена" in sent[-1][1]
    print("[3/4] switching to an incompatible model clears effort")


def test_start_process_passes_effort_flag():
    calls = []

    class FakeProcess:
        stdin = None
        stdout = []

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    original_popen = chat_process.subprocess.Popen
    original_thread = chat_process.threading.Thread
    chat_process.subprocess.Popen = lambda args, **kwargs: (calls.append(args), FakeProcess())[1]
    chat_process.threading.Thread = FakeThread
    try:
        chat_process._start_chat_process(
            "test-effort-process", "claude-opus-5", "xhigh", None, "/tmp",
            "/tmp/config", "session-id", {},
        )
    finally:
        chat_process.subprocess.Popen = original_popen
        chat_process.threading.Thread = original_thread
        chat_process.chat_procs.pop("test-effort-process", None)
    assert "--effort=xhigh" in calls[0]
    assert "--model=claude-opus-5" in calls[0]
    assert "--resume=session-id" in calls[0]
    print("[4/4] persistent process receives --effort")


if __name__ == "__main__":
    test_model_forms_and_errors()
    test_effort_validation()
    test_model_change_clears_unsupported_effort()
    test_start_process_passes_effort_flag()
    print("\nCLOSED: model/effort selection is validated and reaches Claude CLI.")
