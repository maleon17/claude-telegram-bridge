"""Locate the sibling repositories some breaker tests exercise.

These tests deliberately load the *canonical* checkouts the live services run
from, not historical copies. Override the locations with CLAUDE_JARVIS_DIR /
CODEX_JARVIS_DIR / TELEGRAM_AI_CORE_DIR; when a checkout is absent the test is skipped explicitly
instead of failing with an ImportError.
"""
import os
import sys


def checkout(env_var, *defaults):
    """The env override if set, else the first default that exists."""
    if env_var in os.environ:
        return os.path.abspath(os.path.expanduser(os.environ[env_var]))
    paths = [os.path.abspath(os.path.expanduser(default)) for default in defaults]
    return next((path for path in paths if os.path.isdir(path)), paths[0])


CLAUDE_JARVIS_DIR = checkout("CLAUDE_JARVIS_DIR", "~/.claude-ask", "~/claude-ask", "~/.claude-jarvis", "~/claude-jarvis")
CODEX_JARVIS_DIR = checkout("CODEX_JARVIS_DIR", "~/.codex-ask", "~/codex-ask", "~/.codex-jarvis", "~/codex-jarvis")
# The relay and the userbot module source live in telegram-ai-core.
TELEGRAM_AI_CORE_DIR = checkout("TELEGRAM_AI_CORE_DIR", "~/.telegram-ai-core", "~/telegram-ai-core")


def require(*paths):
    """Exit with a SKIP (status 0) if any required file is missing."""
    missing = [path for path in paths if not os.path.exists(path)]
    if missing:
        print("SKIP: required checkout file(s) not found: " + ", ".join(missing))
        sys.exit(0)
