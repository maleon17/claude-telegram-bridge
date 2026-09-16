"""Regression test for keeping permission denials in Jarvis's context only."""

import importlib.util
import os
import sys

from _checkouts import CLAUDE_JARVIS_DIR, CODEX_JARVIS_DIR, require

CLAUDE_WATCHER = os.path.join(CLAUDE_JARVIS_DIR, "claude_watcher.py")
CODEX_WATCHER = os.path.join(CODEX_JARVIS_DIR, "codex_ask_watcher.py")
# Personas moved out of the watchers into per-instance files; the tracked
# template is what teaches every instance how to treat the internal marker.
CLAUDE_PERSONA_TEMPLATE = os.path.join(CLAUDE_JARVIS_DIR, "personas", "default.md.example")
CODEX_PERSONA_TEMPLATE = os.path.join(CODEX_JARVIS_DIR, "personas", "default.md.example")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main():
    require(CLAUDE_WATCHER, CODEX_WATCHER, CLAUDE_PERSONA_TEMPLATE, CODEX_PERSONA_TEMPLATE)
    sys.path.insert(0, CODEX_JARVIS_DIR)
    claude = _load(CLAUDE_WATCHER, "claude_watcher_visibility_test")
    codex = _load(CODEX_WATCHER, "codex_ask_watcher_visibility_test")

    claude_denial = f"{claude.INTERNAL_TOOL_RESULT_PREFIX} действие заблокировано"
    codex_denial = f"{codex.INTERNAL_TOOL_RESULT_PREFIX} действие заблокировано"
    assert claude._is_internal_tool_result(claude_denial)
    assert codex._is_internal_tool_result(codex_denial)
    assert claude.INTERNAL_TOOL_RESULT_PREFIX in _read(CLAUDE_PERSONA_TEMPLATE)
    assert codex.INTERNAL_TOOL_RESULT_PREFIX in _read(CODEX_PERSONA_TEMPLATE)
    assert codex._item_result_blocks({
        "type": "mcp_tool_call", "result": codex_denial,
    }) == []
    print("CLOSED: permission denial stays in the model context, not progress")


if __name__ == "__main__":
    main()
