"""A re-login for an already-onboarded non-owner tenant must not re-ask
their name.

feed_login_code's success path used to route EVERY non-owner, non-delegated
login straight to "awaiting_display_name" -- including a routine re-login
after the OAuth token simply expired, not just a brand-new tenant's first
login. Live incident 2026-10-09: an established tenant (persona already
customized, used for weeks) got asked "how should I address you?" again
after a plain re-login. The real signal for "never onboarded" is whether
the persona still has the <user> placeholder, not "is this the first login
this process has ever seen."
"""

import json
import os
import sys
import tempfile
import time


TEST_ROOT = tempfile.mkdtemp(prefix="bridge_relogin_onboarding_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import runtime  # noqa: E402


class FakeThread:
    """Runs the target synchronously instead of in a real background
    thread, so the test doesn't need to sleep/poll for it to finish."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _run_feed_login_code(chat_id, persona_contains_placeholder):
    tenant_dir = runtime.account_dir(chat_id)
    persona_path = os.path.join(tenant_dir, "CLAUDE.md")
    with open(persona_path, "w", encoding="utf-8") as f:
        f.write("Обращайся к пользователю как: <user>.\n" if persona_contains_placeholder
                 else "Обращайся к пользователю как: Анатолий.\n")

    fifo_path = os.path.join(TEST_ROOT, f"fifo-{chat_id}")
    open(fifo_path, "w").close()
    state = {}
    handlers.pending_logins[chat_id] = {
        "fifo": fifo_path, "config_dir": tenant_dir, "delegated": False,
        "state_key": chat_id,
    }

    messages = []
    original_thread = handlers.threading.Thread
    original_run = handlers.subprocess.run
    original_ready = handlers._login_credentials_ready
    original_stop = handlers._stop_chat_process
    original_send = handlers.send_message
    original_sleep = handlers.time.sleep
    handlers.threading.Thread = FakeThread
    handlers.subprocess.run = lambda *a, **kw: type(
        "R", (), {"stdout": json.dumps({"loggedIn": True})}
    )()
    handlers._login_credentials_ready = lambda info: True
    handlers._stop_chat_process = lambda key: None
    handlers.send_message = lambda chat_id, text, **kw: messages.append(text)
    handlers.time.sleep = lambda s: None
    try:
        assert handlers.feed_login_code(chat_id, "oauth-code", state)
    finally:
        handlers.threading.Thread = original_thread
        handlers.subprocess.run = original_run
        handlers._login_credentials_ready = original_ready
        handlers._stop_chat_process = original_stop
        handlers.send_message = original_send
        handlers.time.sleep = original_sleep
        handlers.pending_logins.pop(chat_id, None)
    return state, messages


def main():
    # A genuinely new tenant (placeholder still present) still gets asked.
    chat_id = 501
    state, messages = _run_feed_login_code(chat_id, persona_contains_placeholder=True)
    assert handlers.get_account_status(state, chat_id) == "awaiting_display_name"
    assert any("обращаться" in m.lower() or "address" in m.lower() for m in messages), messages

    # An already-onboarded tenant re-logging in goes straight to ready.
    chat_id = 502
    state, messages = _run_feed_login_code(chat_id, persona_contains_placeholder=False)
    assert handlers.get_account_status(state, chat_id) == "ready"
    assert not any("обращаться" in m.lower() or "address" in m.lower() for m in messages), messages


if __name__ == "__main__":
    main()
    print("CLOSED: a re-login for an already-named tenant goes straight to ready.")
