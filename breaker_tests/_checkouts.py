"""Locate the sibling repositories some breaker tests exercise.

These tests deliberately load the *canonical* checkouts the live services run
from, not historical copies. Override the locations with CLAUDE_JARVIS_DIR /
CODEX_JARVIS_DIR; when a checkout is absent the test is skipped explicitly
instead of failing with an ImportError.
"""
import os
import sys


def checkout(env_var, default):
    return os.path.abspath(os.path.expanduser(os.environ.get(env_var, default)))


CLAUDE_JARVIS_DIR = checkout("CLAUDE_JARVIS_DIR", "~/claude-jarvis")
CODEX_JARVIS_DIR = checkout("CODEX_JARVIS_DIR", "~/codex-jarvis")


def require(*paths):
    """Exit with a SKIP (status 0) if any required file is missing."""
    missing = [path for path in paths if not os.path.exists(path)]
    if missing:
        print("SKIP: required checkout file(s) not found: " + ", ".join(missing))
        sys.exit(0)
