"""Telegram actions are offered only to the owner of a userbot instance.

Who owns what is the relay's decision (its owners map); the bridge only asks.
A stand-in for telegram-ai-core's CLI plays the relay here.
"""

import json
import os
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_tg_actions_")
os.environ["HOME"] = TMP
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["TELEGRAM_AI_CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import runtime  # noqa: E402
import telegram_actions_link as link  # noqa: E402

CORE = os.path.join(TMP, "telegram-ai-core")
OWNERS = os.path.join(TMP, "owners.json")
MINTED = os.path.join(TMP, "minted.log")
FAKE_CLI = '''
import json, sys
owners = json.load(open({owners!r}))
if sys.argv[1] == "--owned-by":
    print("\\n".join(sorted(i for i, o in owners.items() if str(o) == sys.argv[2])))
elif sys.argv[1] == "--add-bridge-token":
    assert sys.argv[2] in owners
    with open({minted!r}, "a") as log:
        log.write(sys.argv[2] + "\\n")
    print("bridge-token-for-" + sys.argv[2])
else:
    sys.exit(2)
'''


def _install_core(owners):
    os.makedirs(CORE, exist_ok=True)
    with open(OWNERS, "w") as handle:
        json.dump(owners, handle)
    with open(os.path.join(CORE, "cmd_queue.py"), "w") as handle:
        handle.write(FAKE_CLI.format(owners=OWNERS, minted=MINTED))
    open(os.path.join(CORE, "telegram_actions_mcp.py"), "w").close()


def _register(components):
    os.makedirs(os.environ["TELEGRAM_AI_CONFIG_DIR"], exist_ok=True)
    with open(os.path.join(os.environ["TELEGRAM_AI_CONFIG_DIR"], "registry.json"), "w") as handle:
        json.dump({"version": 1, "components": components}, handle)


def _minted():
    try:
        return open(MINTED).read().split()
    except FileNotFoundError:
        return []


def _tenant(name):
    path = os.path.join(TMP, name)
    os.makedirs(path, exist_ok=True)
    return path


def _servers(config_dir):
    with open(os.path.join(config_dir, ".claude.json"), encoding="utf-8") as handle:
        return json.load(handle)["mcpServers"]


def main():
    # No telegram-ai-core on the host: nothing to offer, nothing breaks.
    assert link.resolve_binding(111, _tenant("t0"), "claude-ask") is None

    _install_core({"alice-bot": 111, "alice-codex": 111, "bob-bot": 222})
    _register({
        "core": {"path": CORE, "python": sys.executable},
        "relay": {"path": CORE, "url": "http://127.0.0.1:9092", "config_dir": os.environ["TELEGRAM_AI_CONFIG_DIR"]},
        "codex-ask": {"path": CORE, "instances": "alice-codex"},
    })

    # The owner of an instance is bound to an instance of their own.
    alice = _tenant("alice")
    binding = link.resolve_binding(111, alice, "claude-ask")
    assert binding["instance"] == "alice-bot", binding
    assert binding["env"] == {
        "JARVIS_RELAY_URL": "http://127.0.0.1:9092", "JARVIS_RELAY_TOKEN": "bridge-token-for-alice-bot",
        "INSTANCE_ID": "alice-bot", "JARVIS_RELAY_DIRECT": "1",
    }
    assert binding["args"] == [os.path.join(CORE, "telegram_actions_mcp.py")]
    # The product that serves an instance is preferred when a person owns several.
    assert link.resolve_binding(111, _tenant("alice-x"), "codex-ask")["instance"] == "alice-codex"
    # The token is kept privately and reused, not minted again on every start.
    cache = os.path.join(alice, link.CACHE_FILE)
    assert oct(os.stat(cache).st_mode & 0o777) == "0o600"
    assert link.resolve_binding(111, alice, "claude-ask")["env"]["JARVIS_RELAY_TOKEN"] == "bridge-token-for-alice-bot"
    assert _minted().count("alice-bot") == 1

    # Someone who owns no instance gets nothing -- never someone else's.
    assert link.resolve_binding(333, _tenant("carol"), "claude-ask") is None
    assert "bob-bot" not in _minted()

    # ---- the bridge's own MCP configuration follows the binding ----
    runtime._ensure_tenant_mcp_config(alice, 111)
    entry = _servers(alice)[link.SERVER_NAME]
    assert entry["env"]["INSTANCE_ID"] == "alice-bot" and entry["command"] == sys.executable
    carol = _tenant("carol")
    runtime._ensure_tenant_mcp_config(carol, 333)
    assert link.SERVER_NAME not in _servers(carol)

    # A server of that name the tenant configured themselves is left alone.
    dave = _tenant("dave")
    with open(os.path.join(dave, ".claude.json"), "w") as handle:
        json.dump({"mcpServers": {link.SERVER_NAME: {"command": "mine", "args": []}}}, handle)
    _install_core({"alice-bot": 111, "dave-bot": 444})
    runtime._ensure_tenant_mcp_config(dave, 444)
    assert _servers(dave)[link.SERVER_NAME] == {"command": "mine", "args": []}

    # Ownership moved away: after a restart the tools are withdrawn.
    _install_core({"alice-bot": 999})
    runtime._telegram_actions_checked.clear()
    runtime._ensure_tenant_mcp_config(alice, 111)
    assert link.SERVER_NAME not in _servers(alice)
    assert not os.path.exists(cache)


if __name__ == "__main__":
    main()
    print("CLOSED: Telegram actions are bound only to an instance the chat's owner owns.")
