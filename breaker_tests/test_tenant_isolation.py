"""Flagged tenant execution must fail closed and leave ordinary chats intact."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge_isolation_"))
os.environ["BRIDGE_TENANT_CONTROL_DIR"] = str(TMP / "control")
os.environ["BRIDGE_ISOLATED_MCP_DIR"] = str(TMP / "no-mcp")
os.environ.update(TELEGRAM_BOT_TOKEN="fake", OWNER_ID="1000000001",
                  BRIDGE_STATE_FILE=str(TMP / "state.json"), BRIDGE_ISOLATION="off")
sys.path.insert(0, str(ROOT))
import chat_process  # noqa: E402
import handlers  # noqa: E402
import runtime  # noqa: E402
import state_store  # noqa: E402


class FakeProcess:
    stdin = None
    stdout = []

    def poll(self):
        return None

    def terminate(self):
        pass


class FakeThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def main():
    tenant = "1000000002"
    home = TMP / "tenant"
    (home / "workspace").mkdir(parents=True)
    exchange = home / ".exchange"
    exchange.mkdir()
    fake_bin = TMP / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text("#!/bin/sh\n[ \"$1\" = -n ] || exit 1\nshift\nexec \"$@\"\n")
    sudo.chmod(0o755)
    provision = TMP / "provision"
    payload = {"id": tenant, "kind": "main", "home": str(home),
               "exchange": str(exchange), "uid": 22001}
    provision.write_text("#!/usr/bin/python3\nimport json,sys\n"
                         f"data = {payload!r}\ndata['kind'] = sys.argv[-1]\nprint(json.dumps(data))\n")
    provision.chmod(0o755)
    runner = TMP / "runner"
    runner.write_text("#!/bin/sh\nexit 0\n")
    runner.chmod(0o755)
    runtime.BRIDGE_TENANT_PROVISION = str(provision)
    runtime.BRIDGE_TENANT_RUN = str(runner)
    runtime.ISOLATED_TENANTS = frozenset({tenant})
    with patch.dict(os.environ, {"PATH": str(fake_bin) + ":/usr/bin:/bin"}):
        assert runtime.ensure_tenant(tenant)["uid"] == 22001
        assert runtime.ensure_tenant(tenant, "delegate")["kind"] == "delegate"
    calls = []
    original_popen = subprocess.Popen
    with patch.dict(os.environ, {"PATH": str(fake_bin) + ":/usr/bin:/bin"}), \
            patch.object(chat_process.subprocess, "Popen", side_effect=lambda argv, **kw: (calls.append((argv, kw)), FakeProcess())[1]), \
            patch.object(chat_process.threading, "Thread", FakeThread):
        chat_process._start_chat_process(tenant, None, None, None, "/tmp", "/tmp/config", None, {})
        argv, kwargs = calls.pop()
        assert argv == [chat_process.CLAUDE_BIN, "-p", "--input-format=stream-json",
                        "--output-format=stream-json", "--include-partial-messages",
                        "--verbose", "--dangerously-skip-permissions"]
        assert kwargs["cwd"] == "/tmp"
        runtime.BRIDGE_ISOLATION = "on"
        assert runtime.tenant_isolated(tenant) and runtime.tenant_isolated("delegate:" + tenant)
        assert not runtime.tenant_isolated("1000000003")
        chat_process._start_chat_process(tenant, None, None, None, "workspace", None, None, {})
        argv, kwargs = calls.pop()
        assert argv[:10] == ["sudo", "-n", str(runner), tenant, "--kind", "main",
                             "--product", "bridge", "--cwd", "workspace"]
        assert "--env" in argv and "CHAT_ID=" + tenant in argv
        assert "CLAUDE_TELEGRAM_OUTBOX=" + str(exchange / "outbox") in argv
        assert "WAKEUP_SIGNAL_DIR=" not in " ".join(argv)
        assert argv[-6:] == ["-p", "--input-format=stream-json", "--output-format=stream-json",
                             "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
        assert kwargs["cwd"] == "/" and kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        chat_process._start_chat_process(tenant, None, None, None, "workspace", None, None, {},
                                         extra_env={"CHAT_ID": "wrong", "DISABLE_FOO": "1"})
        forwarded_argv = calls.pop()[0]
        assert "CHAT_ID=" + tenant in forwarded_argv and "CHAT_ID=wrong" not in forwarded_argv
        assert "DISABLE_FOO=1" in forwarded_argv
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")) as status_run:
            assert state_store.get_account_status({}, tenant) == "ready"
            assert status_run.call_args.args[0][-8:] == [
                "claude", "--append-system-prompt-file", str(exchange / "persona.md"),
                "--mcp-config", str(exchange / "mcp.json"), "--strict-mcp-config",
                "auth", "status",
            ]
            assert str(runner) in status_run.call_args.args[0]
        state_store.forget_isolated_ready(tenant)
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"loggedIn": false}', "")):
            assert state_store.get_account_status({tenant: {"account_status": "ready"}}, tenant) is None
        # A logged-out account exits non-zero but still prints its JSON: a normal answer, not a failure.
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '{"loggedIn": false}', "")):
            assert state_store.get_account_status({}, tenant) is None
        # A tenant-run refusal has empty stdout and a reason on stderr: that must fail closed.
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "tenant-run: refused")):
            try:
                state_store.get_account_status({}, tenant)
                raise AssertionError("a refusal was read as a status")
            except runtime.TenantUnavailable:
                pass
        # A positive answer is remembered briefly (one subprocess call, not one per message) and forgotten on demand.
        state_store.forget_isolated_ready(tenant)
        with patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"loggedIn": true}', "")) as cached_run:
            assert state_store.get_account_status({}, tenant) == "ready"
            assert state_store.get_account_status({}, tenant) == "ready"
            assert cached_run.call_count == 1
            state_store.forget_isolated_ready(tenant)
            assert state_store.get_account_status({}, tenant) == "ready"
            assert cached_run.call_count == 2
        state_store.forget_isolated_ready(tenant)
        chat_process._start_chat_process("delegate:" + tenant, None, None, None, "workspace", None, None, {}, output_chat_id=tenant)
        assert "delegate" in calls.pop()[0]
        try:
            runtime.tenant_run_argv(tenant, "main", env={"PATH": "/evil"})
            raise AssertionError("forbidden env reached runner")
        except runtime.TenantUnavailable:
            pass
        try:
            runtime.tenant_run_argv(tenant, "main", env={"ANTHROPIC_API_KEY": "x"})
            raise AssertionError("secret env reached runner")
        except runtime.TenantUnavailable:
            pass
        messages = []
        with patch.object(handlers, "send_message", side_effect=lambda cid, msg: messages.append(msg)):
            state = {tenant: {"account_status": "ready"}}
            for bad in ("../outside", "/etc", "workspace/../outside"):
                handlers.handle_command(tenant, "/workspace " + bad, state)
                assert state[tenant].get("workspace") is None
                assert messages[-1] == handlers.t("tenant_workspace_invalid")
        with patch.object(handlers.subprocess, "Popen", side_effect=lambda argv, **kw: (calls.append((argv, kw)), FakeProcess())[1]), \
                patch.object(handlers.threading, "Thread", FakeThread):
            handlers.start_login(tenant, {}, delegated=False)
            assert str(runner) in calls.pop()[0][-1]
            handlers._cleanup_login(tenant, handlers.pending_logins[tenant])
            handlers.start_login(tenant, {}, delegated=True)
            delegate_command = calls.pop()[0][-1]
            assert "--kind main --product delegate" in delegate_command
            handlers._cleanup_login(tenant, handlers.pending_logins[tenant])
        provision.write_text("#!/bin/sh\nexit 7\n")
        runtime._tenant_cache.clear()
        chat_process.chat_procs.clear()
        with patch.object(subprocess, "Popen", original_popen):
            with patch.object(handlers, "send_message", side_effect=lambda cid, msg: messages.append(msg)):
                assert handlers.start_login(tenant, {}) is False
                assert messages[-1] == handlers.t("tenant_unavailable")
            try:
                chat_process._start_chat_process(tenant, None, None, None, "workspace", None, None, {})
                raise AssertionError("turn started after provision failure")
            except runtime.TenantUnavailable:
                pass
            with patch.object(handlers, "send_message", side_effect=lambda cid, msg: messages.append(msg)), \
                    patch.object(handlers, "send_typing", return_value=None):
                handlers._run_turn_thread(tenant, "hello", {tenant: {"account_status": "ready"}})
                assert messages[-1] == handlers.t("tenant_unavailable")
    for name in ("account-limits", "list-sessions"):
        assert (ROOT / "isolation/tools" / name).read_text().startswith("#!/usr/bin/python3")
    project = home / ".claude-bridge" / "projects" / str(home / "workspace").replace("/", "-")
    project.mkdir(parents=True)
    (project / "aabbccdd.jsonl").write_text(json.dumps({"type": "user", "content": "hello world"}) + "\n")
    result = subprocess.run([sys.executable, str(ROOT / "isolation/tools/list-sessions")],
                            cwd=home / "workspace", env={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude-bridge")},
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["sessions"][0]["preview"] == "hello world"
    claude = TMP / "claude"
    claude.write_text("#!/usr/bin/python3\nimport json\nprint(json.dumps({'type':'result','result':'Current session: 50% used\\nCurrent week: 20% used\\nsecret-token'}))\n")
    claude.chmod(0o755)
    result = subprocess.run([sys.executable, str(ROOT / "isolation/tools/account-limits")],
                            env={"CLAUDE_BIN": str(claude)}, capture_output=True, text=True, check=True)
    limits = json.loads(result.stdout)
    assert len(limits["lines"]) == 2 and "secret-token" not in result.stdout


if __name__ == "__main__":
    main()
    print("CLOSED: flagged tenants use the runner and fail closed; tools return safe JSON.")
