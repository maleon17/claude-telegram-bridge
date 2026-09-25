"""Language switching preserves customized personas and cleans translation sessions."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="breaker_language_claude_") as temporary:
        os.environ["HOME"] = temporary
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
        os.environ["OWNER_ID"] = "1000000001"
        os.environ["BRIDGE_STATE_FILE"] = str(Path(temporary) / "state.json")
        import handlers
        import runtime
        from state_store import _localize_limit_line
        from strings import current_language, t

        chat_id = 2000000002
        state = {}
        sent = []
        edits = []
        events = []

        def fake_send(cid, message, **kwargs):
            message_id = 100 + len(sent)
            sent.append((cid, message, message_id))
            events.append(("send", cid, message_id, message))
            return {"ok": True, "result": {"message_id": message_id}}

        def fake_edit(cid, message_id, message):
            edits.append((cid, message_id, message))
            events.append(("edit", cid, message_id, message))
            return {"ok": True}

        handlers.send_message = fake_send
        handlers.edit_message = fake_edit
        real_set_chat_commands = handlers.set_chat_commands
        handlers.set_chat_commands = lambda cid, language: None

        def assert_switch_message(code, start):
            assert len(events) == start + 2
            pending, finished = events[start:]
            assert pending[:2] == ("send", chat_id)
            assert pending[3] == t("language_progress", lang=code)
            assert finished == ("edit", chat_id, pending[2], t("language_changed", lang=code))

        tenant = Path(runtime.account_dir(chat_id))
        persona = tenant / "CLAUDE.md"
        marker = tenant / ".persona_default_sha256"
        assert marker.exists() and handlers._persona_is_default(chat_id)
        assert not (tenant / "projects").exists()

        start = len(events)
        assert handlers.handle_command(chat_id, "/language EN", state)
        assert_switch_message("en", start)
        assert state[str(chat_id)]["language"] == "en"
        assert current_language.get() == "en"
        assert persona.read_text(encoding="utf-8") == (ROOT / "personality.example.en.md").read_text(encoding="utf-8")
        assert handlers._persona_is_default(chat_id)

        # Onboarding substitutes the name into a seeded persona and keeps its
        # marker aligned, so the next language switch still swaps templates.
        state[str(chat_id)]["account_status"] = "awaiting_display_name"
        handlers.handle_onboarding(chat_id, chat_id, "Alex", state, {str(chat_id)})
        assert "Alex" in persona.read_text(encoding="utf-8")
        assert handlers._persona_is_default(chat_id)
        start = len(events)
        assert handlers.handle_command(chat_id, "/language de", state)
        assert_switch_message("de", start)
        assert persona.read_text(encoding="utf-8") == (ROOT / "personality.example.de.md").read_text(encoding="utf-8")
        assert handlers._persona_is_default(chat_id)

        start = len(events)
        assert handlers.handle_command(chat_id, "/language ru", state)
        assert_switch_message("ru", start)
        assert persona.read_text(encoding="utf-8") == (ROOT / "personality.example.md").read_text(encoding="utf-8")
        assert "Russian" not in events[start + 1][3]

        persona.write_text("# My persona\nAlways answer clearly. Preserve this meaning.\n", encoding="utf-8")
        assert not handlers._persona_is_default(chat_id)
        state[str(chat_id)]["model"] = "claude-sonnet-5"
        other_session = tenant / "projects" / "unexpected-name" / "keep-me.jsonl"
        other_session.parent.mkdir(parents=True)
        other_session.write_text("keep", encoding="utf-8")
        calls = []

        def fake_run(command, **kwargs):
            assert events[-1][0] == "send" and events[-1][3] == t("language_progress")
            events.append(("run",))
            calls.append((command, kwargs))
            session_id = command[command.index("--session-id") + 1]
            session_file = other_session.with_name(f"{session_id}.jsonl")
            session_file.write_text("temporary", encoding="utf-8")
            assert session_file.exists()
            assert kwargs["cwd"] == str(tenant)
            assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(tenant)
            assert kwargs["env"].get("TELEGRAM_BOT_TOKEN") is None
            assert "--permission-mode" in command and command[command.index("--permission-mode") + 1] == "plan"
            assert "--permission-prompts" in command and command[command.index("--permission-prompts") + 1] == "none"
            assert "--strict-mcp-config" in command
            assert "--tools" in command and command[command.index("--tools") + 1] == ""
            assert "--model" in command and command[command.index("--model") + 1] == "claude-sonnet-5"
            assert "--dangerously-skip-permissions" not in command
            if len(calls) == 1:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "# My persona\nAlways answer in Ukrainian. Preserve this meaning faithfully and clearly.\n"}), stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="translation failed")

        handlers.subprocess.run = fake_run
        start = len(events)
        assert handlers.handle_command(chat_id, "/language uk", state)
        assert events[start][3] == t("language_progress", lang="uk")
        assert events[start + 1] == ("run",)
        assert events[start + 2] == ("edit", chat_id, events[start][2], t("language_changed", lang="uk"))
        assert state[str(chat_id)]["language"] == "uk"
        assert "Ukrainian" in persona.read_text(encoding="utf-8")
        assert not handlers._persona_is_default(chat_id)
        assert other_session.read_text(encoding="utf-8") == "keep"
        assert list(other_session.parent.glob("*.jsonl")) == [other_session]

        before = persona.read_text(encoding="utf-8")
        start = len(events)
        assert handlers.handle_command(chat_id, "/language kk", state)
        assert events[start][3] == t("language_progress", lang="kk")
        assert events[start + 1] == ("run",)
        assert events[start + 2] == (
            "edit", chat_id, events[start][2],
            t("language_persona_failed", lang="kk", error="translation failed"),
        )
        assert state[str(chat_id)]["language"] == "kk"
        assert current_language.get() == "kk"
        assert persona.read_text(encoding="utf-8") == before
        assert "мүмкін болмады" in edits[-1][2]
        assert list(other_session.parent.glob("*.jsonl")) == [other_session]

        assert handlers.handle_command(chat_id, "/language xx", state)
        assert state[str(chat_id)]["language"] == "kk"
        assert persona.read_text(encoding="utf-8") == before
        assert sent[-1][1] == t("language_usage")

        marker.unlink()
        assert not handlers._persona_is_default(chat_id)
        assert handlers.handle_command(runtime.OWNER_ID, "/persona reset", state)
        owner = Path(runtime.account_dir(runtime.OWNER_ID))
        assert (owner / ".persona_default_sha256").exists()
        assert handlers._persona_is_default(runtime.OWNER_ID)

        calls = []
        handlers.tg_call = lambda method, payload: calls.append((method, payload)) or {"ok": True}
        handlers.set_chat_commands = real_set_chat_commands
        handlers.register_commands(state)
        selected = [payload for method, payload in calls
                    if method == "setMyCommands" and payload.get("scope") ==
                    {"type": "chat", "chat_id": chat_id}]
        assert len(selected) == 1
        assert selected[0]["commands"][0]["description"] == \
            handlers.COMMAND_DESCRIPTIONS["kk"]["language"]
        sample = "Current session: 3% used · resets Sep 25, 9pm (Europe/Kyiv)"
        for code in ("ru", "en", "uk", "kk", "de"):
            current_language.set(code)
            line = _localize_limit_line(sample)
            assert t("account_limit_session") in line
            assert "25.09 21:00 (Europe/Kyiv)" in line

        handlers.fetch_account_limits = lambda config_dir=None: t("state_store_fetch_account_limits_2")
        for code in ("ru", "en", "uk", "kk", "de"):
            state[str(chat_id)]["language"] = code
            assert handlers.handle_command(chat_id, "/usage", state)
            report = sent[-1][1]
            assert t("usage_session_header", lang=code) in report
            assert t("usage_tokens_header", lang=code) in report
            assert t("usage_limits_header", lang=code) in report


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: language persona marker, template swap, translation isolation and failure paths.")
        raise SystemExit(0)
