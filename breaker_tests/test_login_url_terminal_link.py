"""The /login button gets the bare sign-in URL, even when the CLI prints it
as an OSC 8 terminal hyperlink (Claude Code 2.1.3xx under the `script` pty)."""

import os
import sys
import tempfile


TEST_ROOT = tempfile.mkdtemp(prefix="bridge_login_url_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402

URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference&code_challenge=xyz&code_challenge_method=S256&state=st"
)


def main():
    # What the CLI really prints on a terminal: the link wrapped in OSC 8,
    # with the URL repeated as the visible text.
    linked = f"If the browser didn't open, visit: \x1b]8;;{URL}\x1b\\{URL}\x1b]8;;\x1b\\\r\n"
    assert handlers._login_url(linked) == URL, handlers._login_url(linked)

    # The same link terminated with BEL instead of ST.
    assert handlers._login_url(f"visit: \x1b]8;;{URL}\x07{URL}\x1b]8;;\x07\n") == URL

    # Plain output (no terminal), coloured output, and no link at all.
    assert handlers._login_url(f"If the browser didn't open, visit: {URL}\n") == URL
    assert handlers._login_url(f"visit: \x1b[1;36m{URL}\x1b[0m\n") == URL
    assert handlers._login_url("Opening browser to sign in…\r\n") is None


if __name__ == "__main__":
    main()
    print("CLOSED: the login button carries exactly one clean URL.")
