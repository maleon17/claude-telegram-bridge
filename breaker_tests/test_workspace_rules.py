"""Working-folder guidance reaches every non-owner persona exactly once."""

import hashlib
import os
import sys
import tempfile


TMP = tempfile.mkdtemp(prefix="breaker_workspace_rules_")
os.environ["HOME"] = TMP
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TMP, "state.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import runtime  # noqa: E402
import state_store  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _template(language):
    name = "personality.example.md" if language == "ru" else f"personality.example.{language}.md"
    return _read(os.path.join(ROOT, name))


def _without_rules(text, language):
    stripped = text.replace("\n" + runtime.WORKSPACE_RULES[language] + "\n", "")
    assert stripped != text
    return stripped


def _old_tenant(chat_id, persona, default_marker):
    """A tenant home as it looked before the guidance existed."""
    tenant = os.path.join(runtime.ACCOUNTS_DIR, str(chat_id))
    os.makedirs(tenant, mode=0o700)
    with open(os.path.join(tenant, "CLAUDE.md"), "w", encoding="utf-8") as handle:
        handle.write(persona)
    if default_marker:
        with open(os.path.join(tenant, ".persona_default_sha256"), "w", encoding="ascii") as handle:
            handle.write(_sha(persona) + "\n")
    return tenant


def main():
    # Every stock persona carries the guidance in its own language, verbatim.
    for language, rules in runtime.WORKSPACE_RULES.items():
        assert _template(language).count(rules) == 1, language

    # A brand-new tenant gets it from the stock persona, not appended twice.
    tenant = runtime.account_dir(5151)
    persona = _read(os.path.join(tenant, "CLAUDE.md"))
    assert persona.count("## Рабочая папка") == 1
    assert os.path.exists(os.path.join(tenant, ".workspace_rules_seeded"))
    assert _read(os.path.join(tenant, ".persona_default_sha256")).strip() == _sha(persona)

    # A tenant created earlier with the unmodified English default: appended in
    # English, once, and still recognised as the default persona.
    old_default = _without_rules(_template("en"), "en")
    tenant = _old_tenant(6161, old_default, default_marker=True)
    runtime.account_dir(6161)
    persona = _read(os.path.join(tenant, "CLAUDE.md"))
    assert persona.endswith(runtime.WORKSPACE_RULES["en"] + "\n")
    assert persona.count("## Working folder") == 1
    assert _read(os.path.join(tenant, ".persona_default_sha256")).strip() == _sha(persona)
    runtime.account_dir(6161)
    assert _read(os.path.join(tenant, "CLAUDE.md")) == persona

    # A customised persona is extended too, without being marked as default.
    tenant = _old_tenant(7171, "My own rules.\n", default_marker=False)
    runtime.account_dir(7171)
    persona = _read(os.path.join(tenant, "CLAUDE.md"))
    assert persona.startswith("My own rules.\n") and runtime.WORKSPACE_RULES["ru"] in persona
    assert not os.path.exists(os.path.join(tenant, ".persona_default_sha256"))

    # Removing the section afterwards is respected: it is offered only once.
    with open(os.path.join(tenant, "CLAUDE.md"), "w", encoding="utf-8") as handle:
        handle.write("My own rules.\n")
    runtime.account_dir(7171)
    assert "## Рабочая папка" not in _read(os.path.join(tenant, "CLAUDE.md"))

    # The owner's home directory is the owner's business: never appended.
    os.makedirs(os.path.join(TMP, ".claude"))
    with open(os.path.join(TMP, ".claude", "CLAUDE.md"), "w", encoding="utf-8") as handle:
        handle.write("OWNER PERSONA\n")
    owner_dir = runtime.account_dir(runtime.OWNER_ID)
    assert not any(
        rules.splitlines()[0] in _read(os.path.join(owner_dir, "CLAUDE.md"))
        for rules in runtime.WORKSPACE_RULES.values()
    )

    # A fresh owner home has no credentials yet, so the owner is taken through
    # /login like any other chat; an existing install keeps working unchanged.
    state = {}
    assert state_store.get_account_status(state, runtime.OWNER_ID) is None
    assert state_store.get_account_status(state, 5151) is None
    open(os.path.join(owner_dir, ".credentials.json"), "w").close()
    assert state_store.get_account_status(state, runtime.OWNER_ID) == "ready"
    state[str(runtime.OWNER_ID)] = {"account_status": "awaiting_code"}
    assert state_store.get_account_status(state, runtime.OWNER_ID) == "awaiting_code"


if __name__ == "__main__":
    main()
    print("CLOSED: working-folder guidance is seeded once per non-owner tenant; a fresh owner must log in.")
