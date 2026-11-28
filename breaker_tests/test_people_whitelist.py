"""Hermetic registry and owner whitelist UI coverage."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="people_whitelist_"))
OWNER = "1000000001"
OTHER = "1000000002"
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID=OWNER,
                  BRIDGE_STATE_FILE=str(TMP / "state.json"), BRIDGE_PEOPLE_FILE=str(TMP / "people.json"),
                  BRIDGE_ISOLATION="off", BRIDGE_ISOLATED_MCP_DIR=str(TMP / "no-mcp"))
sys.path.insert(0, str(ROOT))
import people  # noqa: E402
import runtime  # noqa: E402
import handlers  # noqa: E402


def callback(data, from_id=OWNER):
    return {"id": "cb", "data": data, "from": {"id": int(from_id)},
            "message": {"chat": {"id": int(OWNER)}}}


def main():
    legacy = TMP / "whitelist.txt"
    legacy.write_text(f"{OTHER},-123456,abc\n1000000003\n", encoding="utf-8")
    assert people.is_allowed(OWNER)
    assert people.is_allowed(OTHER)
    assert people.is_allowed("1000000003")
    assert not people.is_allowed("-1000000003")
    assert legacy.exists() and json.loads((TMP / "people.json").read_text())["version"] == 1
    assert not any(path.name.startswith(".people-") for path in TMP.iterdir())
    assert people.add(OTHER, OWNER) is False
    people.set_note(OTHER, "Friend")
    assert people.load()["people"][OTHER]["note"] == "Friend"
    with patch.object(people, "_read", side_effect=AssertionError("cache miss")):
        assert people.load()["people"][OTHER]["note"] == "Friend"
    external = json.loads((TMP / "people.json").read_text())
    external["people"][OTHER]["note"] = "Changed"
    (TMP / "people.json").write_text(json.dumps(external))
    assert people.load()["people"][OTHER]["note"] == "Changed"
    assert people.remove(OTHER)
    assert not people.is_allowed(OTHER)
    assert not people.remove(OTHER)
    people.reject(OTHER)
    assert people.is_rejected(OTHER)
    assert not people.is_allowed(OTHER)
    assert people.add(OTHER, OWNER)
    assert not people.is_rejected(OTHER)
    with patch.object(people, "_write", side_effect=people.ReadOnlyError("read-only")):
        try:
            people.add("1000000004", OWNER)
            raise AssertionError("read-only write succeeded")
        except people.ReadOnlyError:
            pass
    assert not people.is_allowed("1000000004")
    original = (TMP / "people.json").read_bytes()
    with patch.object(people.os, "replace", side_effect=PermissionError("read-only")):
        try:
            people.add("1000000004", OWNER)
            raise AssertionError("replace failure was ignored")
        except people.ReadOnlyError:
            pass
    assert (TMP / "people.json").read_bytes() == original
    assert not any(path.name.startswith(".people-") for path in TMP.iterdir())
    with patch.dict(os.environ, {"BRIDGE_PEOPLE_FILE": str(TMP / "readonly.json")}), \
            patch.object(people.os, "replace", side_effect=PermissionError("read-only")):
        assert OTHER in people.load()["people"]
        assert not (TMP / "readonly.json").exists()

    calls = []
    def role_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "list":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"static": [OWNER], "dynamic": [OTHER]}), "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    with patch.object(people.subprocess, "run", side_effect=role_run):
        assert people.role_of(OWNER) == "owner"
        assert people.role_of(OTHER) == "admin"
        assert people.role_of(OTHER) == "admin" and len(calls) == 1
        assert people.grant_admin("123; echo evil") == (False, "Invalid Telegram ID")
        assert people.revoke_admin("-1000000002") == (False, "Invalid Telegram ID")
        assert people.grant_admin(OTHER)[0]
        assert people.revoke_admin(OTHER)[0]
        assert calls[-2][0] == ["sudo", "-n", people.ROLE_HELPER, "grant", OTHER]
        assert calls[-1][0] == ["sudo", "-n", people.ROLE_HELPER, "revoke", OTHER]
        assert calls[-1][1]["timeout"] == 120
        assert people.role_of(OTHER) == "admin" and len(calls) == 4
    people._admins_until = 0
    with patch.object(people.subprocess, "run", side_effect=OSError("unavailable")):
        assert people.admins() == set()
    assert people.role_of(OTHER) == "member"
    assert people.role_of("1000000004") == "none"

    # real `tenant-role list` output: static and dynamic administrators are both admins
    people._admins_until = 0
    with patch.object(people.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"static": ["597609807"], "dynamic": [OTHER]}), "")):
        assert people.admins() == {"597609807", OTHER}

    # a corrupt registry fails closed and does not crash the gate
    corrupt = TMP / "corrupt.json"
    corrupt.write_text("{not json")
    with patch.dict(os.environ, {"BRIDGE_PEOPLE_FILE": str(corrupt)}):
        people._cache = None
        assert people.is_allowed(OTHER) is False
        assert people.is_allowed(OWNER) is True
        assert people.is_rejected(OTHER) is False
    people._cache = None

    runtime.BRIDGE_ISOLATION = "on"
    assert runtime.tenant_isolated(OWNER) and runtime.tenant_isolated(OTHER)
    assert not runtime.tenant_isolated("-1000000002")
    assert not runtime.tenant_isolated("1000000004")
    with patch.dict(os.environ, {"BRIDGE_ISOLATION_EXEMPT": OTHER}):
        assert not runtime.tenant_isolated(OTHER) and runtime.tenant_isolated(OWNER)
    runtime.BRIDGE_ISOLATION = "off"

    sent = []
    def fake_tg(method, params, **kwargs):
        sent.append((method, params))
        return {"ok": True}
    state = {OTHER: {"account_status": "ready"}}
    with patch.object(handlers, "tg_call", side_effect=fake_tg), \
            patch.object(handlers, "__file__", str(TMP / "handlers.py")), \
            patch.object(people, "admins", return_value=set()), \
            patch.object(handlers, "ensure_tenant", return_value={"id": OTHER}), \
            patch.object(handlers, "_wl_lock", return_value=(True, "")), \
            patch.object(people, "grant_admin", return_value=(True, "ok")), \
            patch.object(people, "revoke_admin", return_value=(True, "ok")):
        assert handlers.handle_whitelist_message({"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER)}, "text": "/whitelist"}, state)
        before = len(sent)
        assert handlers.handle_command(OWNER, "/whitelist", state, from_id=OTHER)
        assert len(sent) == before
        assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "wl:list"
        before = len(sent)
        handlers.handle_callback_query(callback("wl:add", OTHER), state)
        assert len(sent) == before
        handlers.handle_callback_query(callback("wl:add"), state)
        assert handlers._whitelist_pending[OWNER] > 0
        repeated = callback("wl:list")
        repeated["message"]["message_id"] = 77
        handlers.handle_callback_query(repeated, state)
        before = len(sent)
        handlers.handle_callback_query(repeated, state)
        assert len(sent) == before + 1 and sent[-1][0] == "answerCallbackQuery"
        handlers._whitelist_pending[OWNER] = 0
        assert handlers.handle_whitelist_message({"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER)}, "text": OTHER}, state)
        assert "истекло" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback("wl:add"), state)
        assert handlers.handle_whitelist_message({"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER)},
                                                   "forward_origin": {"sender_user": {"id": int(OTHER), "first_name": "Test"}}}, state)
        assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == f"wl:r:{OTHER}:a"
        handlers.handle_callback_query(callback(f"wl:r:{OTHER}:m"), state)
        assert "добавлен" in sent[-1][1]["text"] or "сделано" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback(f"wl:r:{OTHER}:m"), state)
        assert "Уже сделано" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback(f"wl:r:{OTHER}:x"), state)
        assert "Отменено" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback(f"wl:r:{OTHER}:a"), state)
        assert "root" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback(f"wl:ac:{OTHER}:n"), state)
        handlers.handle_callback_query(callback(f"wl:ac:{OTHER}:y"), state)
        handlers.handle_callback_query(callback(f"wl:list"), state)
        handlers.handle_callback_query(callback(f"wl:p:{OTHER}"), state)
        handlers.handle_callback_query(callback(f"wl:s:{OTHER}:a"), state)
        handlers.handle_callback_query(callback(f"wl:s:{OTHER}:m"), state)
        handlers.handle_callback_query(callback(f"wl:d:{OTHER}"), state)
        handlers.handle_callback_query(callback(f"wl:dc:{OTHER}:n"), state)
        handlers.handle_callback_query(callback(f"wl:dc:{OTHER}:e"), state)
        handlers.handle_callback_query(callback(f"wl:de:{OTHER}:n"), state)
        handlers.handle_callback_query(callback(f"wl:dc:{OTHER}:k"), state)
        assert not people.is_allowed(OTHER)
        handlers.handle_callback_query(callback(f"wl:dc:{OTHER}:k"), state)
        assert "Уже сделано" in sent[-1][1]["text"]
        handlers.handle_callback_query(callback("wl:menu"), state)
        too_long = "wl:p:" + OTHER + "x" * 64
        before = len(sent)
        handlers.handle_callback_query(callback(too_long), state)
        assert len(sent) == before
        handlers.handle_callback_query(callback("wl:add"), state)
        assert handlers.handle_whitelist_message({"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER)}, "text": "/cancel"}, state)
        assert OWNER not in handlers._whitelist_pending
        stranger = "1000000005"
        prompts = []
        with patch.object(handlers, "send_whitelist_prompt", side_effect=lambda value: prompts.append(value)):
            assert handlers.handle_onboarding(int(stranger), int(stranger), "hello", {}, None)
        assert prompts == [int(stranger)]
        handlers.notify_stranger({"chat": {"id": int(stranger)}, "from": {"id": int(stranger), "first_name": "New"}})
        before = len(sent)
        handlers.notify_stranger({"chat": {"id": int(stranger)}, "from": {"id": int(stranger), "first_name": "New"}})
        assert len(sent) == before
        handlers.handle_callback_query(callback(f"wl:r:{stranger}:x"), state)
        assert people.is_rejected(stranger)
        prompts = []
        with patch.object(handlers, "send_whitelist_prompt", side_effect=lambda value: prompts.append(value)):
            assert handlers.handle_onboarding(int(stranger), int(stranger), "hello", {}, None)
        assert prompts == []
        before = len(sent)
        handlers.handle_callback_query(callback(f"wl:r:{stranger}:x"), state)
        assert "Уже сделано" in sent[-1][1]["text"]
        assert len(sent) == before + 2  # callback answer and owner result
        people.add(OTHER, OWNER)
        handlers.handle_callback_query(callback(f"wl:dc:{OTHER}:e"), state)
        handlers.handle_callback_query(callback(f"wl:de:{OTHER}:y"), state)
        assert json.loads((TMP / "data_removal_requests" / f"{OTHER}.json").read_text())["id"] == OTHER

    # button presses edit the pressed message; username (not id) is shown in the list
    calls = []
    def tg2(method, params, **kwargs):
        calls.append((method, params))
        if method == "getChat":
            return {"ok": True, "result": {"first_name": "Pal", "username": "pal_user"}}
        if method == "editMessageText" and params.get("text", "").startswith("FAIL"):
            return {"ok": False}
        return {"ok": True}
    people.add("1000000009", OWNER)
    handlers._wl_lookup_done.clear()
    handlers._wl_cached.clear()
    with patch.object(handlers, "tg_call", side_effect=tg2), \
            patch.object(people, "admins", return_value=set()):
        pressed = callback("wl:list")
        pressed["message"]["message_id"] = 501
        handlers.handle_callback_query(pressed, {})
        methods = [m for m, _ in calls if m in ("editMessageText", "sendMessage")]
        assert methods == ["editMessageText"], methods
        edit = [p for m, p in calls if m == "editMessageText"][0]
        assert edit["message_id"] == 501 and edit["chat_id"] == int(OWNER)
        labels = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
        assert any("Pal (@pal_user)" in label for label in labels), labels
        assert people.load()["people"]["1000000009"]["username"] == "pal_user"
        # edit failure falls back to a new message; later sends in one press are new messages
        calls.clear()
        handlers._wl_ctx.edit = (int(OWNER), 7)
        handlers._wl_send(int(OWNER), "FAIL")
        assert [m for m, _ in calls] == ["editMessageText", "sendMessage"]
        calls.clear()
        handlers._wl_ctx.edit = (int(OWNER), 7)
        handlers._wl_send(1000000009, "stranger")
        handlers._wl_send(int(OWNER), "first")
        handlers._wl_send(int(OWNER), "second")
        assert [m for m, _ in calls] == ["sendMessage", "editMessageText", "sendMessage"]
        handlers._wl_ctx.edit = None
    assert people.set_profile("1000000009", "", "bad name!") is False
    assert people.set_profile("1000000777", "X", "xuser") is False  # unknown people are never created
    assert people.set_profile("1000000009", "Other", "new_user") is True
    assert people.load()["people"]["1000000009"]["username"] == "new_user"
    assert people.load()["people"]["1000000009"]["note"] == "Pal"
    print("CLOSED: people registry, roles, isolation, owner UI, stranger notification, and callback protocol")


if __name__ == "__main__":
    main()
