import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import urllib.parse
import urllib.error
import uuid
import glob
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_format import format_message, strip_mdv2, escape_mdv2

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = os.environ["OWNER_ID"]
WORKDIR = os.environ.get("BRIDGE_WORKDIR", "/home/mishin")
STATE_FILE = os.environ.get(
    "BRIDGE_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)
STATE_INSTANCE_NAME = os.path.splitext(os.path.basename(STATE_FILE))[0]
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
SERVICE_NAME = os.environ.get("SERVICE_NAME")  # this instance's own systemd unit, for /restart
PROJECTS_DIR = os.path.join(
    os.path.expanduser("~/.claude/projects"), WORKDIR.replace("/", "-")
)

def env_positive_int(name, default):
    """Read a positive integer setting, retaining a safe default on errors."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Ignoring invalid {name}; using {default}", flush=True)
        return default
    if value <= 0:
        print(f"Ignoring non-positive {name}; using {default}", flush=True)
        return default
    return value


def telegram_api_config(value):
    """Return the normalized Bot API root and whether it is a local server."""
    root = (value or "https://api.telegram.org").strip().rstrip("/")
    parsed = urlsplit(root)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("claude-telegram-bridge: TELEGRAM_API_URL must be an absolute URL")
    return root, bool(value and (parsed.hostname or "").lower() != "api.telegram.org")


TELEGRAM_API_URL, LOCAL_BOT_API = telegram_api_config(os.environ.get("TELEGRAM_API_URL"))
API_BASE = f"{TELEGRAM_API_URL}/bot{BOT_TOKEN}"
FILE_API_BASE = f"{TELEGRAM_API_URL}/file/bot{BOT_TOKEN}"
EDIT_THROTTLE_S = 1.3
# Same Braille frame set as jarvis-ask's THINKING_SPINNER_FRAMES (claude_ask.py)
# -- a purely cosmetic "still alive" cue for the live-progress message (see
# _flush_draft), advanced at most once per EDIT_THROTTLE_S/spinner-ticker tick,
# nowhere near the ~0.5s dedicated-timer cadence that got jarvis-ask's account
# banned from a group once -- this only ever rides on the same throttled edit
# calls that already existed, never a faster loop of its own.
THINKING_SPINNER_FRAMES = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"
MAX_MSG_LEN = 4000
COST_WARNING_USD = 100.0  # one-time per-session heads-up, see add_usage()
RICH_MAX_CHARS = 32000  # Bot API 10.1 cap is 32768; leave headroom.

# Per-instance uploads dir, derived from STATE_FILE so the two bridge
# instances (different bot tokens) never share incoming-file storage.
UPLOADS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)),
    "uploads_" + os.path.splitext(os.path.basename(STATE_FILE))[0],
)

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
# This bridge has one document limit for both incoming and outgoing files, so
# use one explicit setting instead of two potentially divergent limits.
MAX_DOCUMENT_BYTES = env_positive_int(
    "TELEGRAM_FILE_MAX_BYTES",
    (2000 if LOCAL_BOT_API else 20) * 1024 * 1024,
)
TELEGRAM_CLOUD_FILE_MAX_BYTES = 20 * 1024 * 1024
GET_FILE_TIMEOUT_S = env_positive_int(
    "TELEGRAM_GET_FILE_TIMEOUT_S", 600 if LOCAL_BOT_API else 20,
)
SEND_TIMEOUT_S = env_positive_int(
    "TELEGRAM_SEND_TIMEOUT_S", 600 if LOCAL_BOT_API else 60,
)
FILE_PATH_RE = re.compile(
    r"(/(?:[\w.\-]+/)+[\w.\-]+\.(?:"
    r"png|jpe?g|gif|webp|bmp|svg|pdf|zip|tar|gz|txt|md|csv|json"
    r"|py|js|ts|html|mp3|mp4|wav|docx?|xlsx?|pptx?"
    r")\b)"
)

# chat_id -> {"proc", "signature", "reader_thread", "last_activity",
# "original_prompt"} for that chat's PERSISTENT `claude --input-format=
# stream-json` process (2026-08-18 migration off spawn-per-message -- see
# _ensure_chat_process/_chat_reader_loop). Only 2 real users on this
# instance (owner + father), so holding one live process per chat
# indefinitely is cheap and buys back the per-message CLI cold-start
# (~6s measured) and MCP reconnect churn spawn-per-message paid every
# single turn, plus fixes background-task notifications structurally
# (confirmed live: a backgrounded Bash/Agent task's completion arrives as
# a spontaneous new stream event on the SAME open stdout, no new stdin
# message needed -- the old WAKEUP_SIGNAL_DIR file-signal convention
# below was a workaround for exactly this gap and is no longer load-
# bearing, kept only as a harmless legacy fallback).
chat_procs = {}
chat_procs_lock = threading.Lock()
# Reap a chat's persistent process after this long with no activity --
# not for cost (RAM is not the constraint here), just hygiene against an
# unbounded number of long-idle MCP connections/fds piling up forever.
CHAT_PROC_IDLE_TIMEOUT_S = 6 * 3600
# chat_ids with a turn currently in flight (guards against overlapping
# --resume calls onto the same session).
busy_chats = set()
# Deferred restart (see _restart_watcher_loop): once `draining` is set no new
# turn or message is accepted. `intake_lock` makes "is the bridge idle?" +
# "start draining" atomic relative to accepting a message or starting a turn.
draining = threading.Event()
intake_lock = threading.RLock()
DRAINING_TEXT = "🔄 Бот перезапускается — повтори сообщение через минуту."
# Per-chat serial processing of incoming messages (downloads, transcription,
# commands) off the getUpdates thread: chat_id -> deque of messages, plus the
# set of chats whose intake worker is currently running.
intake_queues = {}
intake_active = set()
# A local-mode download status bubble is created by bridge.py before the
# debounced prompt reaches Claude.  The reader consumes this ID when it makes
# the next turn accumulator, so its normal progress card edits that bubble.
pending_progress_msg_ids = {}
pending_progress_lock = threading.Lock()
# Mutable box so the restart-watcher background thread (see
# _restart_watcher_loop) can read main()'s current getUpdates offset
# without needing it passed in explicitly.
current_offset = [0]

# Guards every read-modify-write on the shared `state` dict + its on-disk
# save. Without this, two chats messaging concurrently could each load,
# mutate, and save `state` in an interleaved order, silently losing one
# side's update (last writer wins on the whole file, not just their key).
state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Multi-tenant accounts: each chat, including OWNER_ID, gets an isolated
# CLAUDE_CONFIG_DIR.  The owner's first account creation migrates their live
# persona/MCP configuration and seeds its own OAuth credentials.
# ---------------------------------------------------------------------------

WHITELIST_FILE = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)), "whitelist.txt"
)
RESTART_SIGNAL_FILE = STATE_FILE + ".restart_signal"
EXTERNAL_REQUEST_FILE = os.environ.get(
    "BRIDGE_EXTERNAL_REQUEST_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(STATE_FILE)),
        f"external_request_{STATE_INSTANCE_NAME}.json",
    ),
)
# bridge_exec.py writes one file per request here (keyed by a unique
# request_id) and waits for the matching file in EXTERNAL_RESULT_DIR, so
# concurrent callers can never overwrite or read each other's request/result.
# EXTERNAL_REQUEST_FILE is still accepted as a legacy single-request channel.
EXTERNAL_REQUEST_DIR = EXTERNAL_REQUEST_FILE + ".d"
EXTERNAL_RESULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)),
    f"external_result_{STATE_INSTANCE_NAME}.d",
)
ACCOUNTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)), "accounts"
)
DELEGATED_ACCOUNTS_DIR = os.path.join(ACCOUNTS_DIR, "delegated")
FILE_SEND_QUEUE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "file_send_queue"
)
FILE_SEND_RESULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "file_send_result"
)
FILE_SEND_MAX_CAPTION_CHARS = 1024
FILE_SEND_AGENTS_MARKER = "## Отправка файлов в Telegram"
FILE_SEND_AGENTS_SECTION = """
## Отправка файлов в Telegram

Чтобы отправить пользователю готовый документ, сначала создай или скопируй его
в каталог из переменной `CLAUDE_TELEGRAM_OUTBOX`, затем вызови MCP-тул
`send_telegram_file` с абсолютным путём к файлу и, при необходимости, `caption`.
Не пытайся искать или использовать токен Telegram: этот тул отправляет файл
только в текущий чат и не раскрывает секреты бота.
""".strip()

# These are bridge control-plane values, not Claude Code configuration.  Do
# not let them cross the process boundary into a Claude child process.
BRIDGE_ENV_VARS = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "BRIDGE_WORKDIR",
    "BRIDGE_STATE_FILE",
    "CLAUDE_BIN",
    "SERVICE_NAME",
    "BRIDGE_EXTERNAL_REQUEST_FILE",
    "BRIDGE_EXEC_EXTERNAL_REQUEST_FILE",
    "BRIDGE_EXEC_STATE_FILE",
    "CHAT_ID",
    "WAKEUP_SIGNAL_DIR",
})

# Background-task wakeup (LEGACY, kept as a harmless fallback -- see 2026-
# 08-18 migration note on `chat_procs` above): originally built because a
# `claude -p --resume` turn's process exited the instant its reply was
# sent, so a backgrounded shell command finishing AFTER that had nothing
# left alive to notice. Under the persistent-process model this no longer
# happens -- confirmed live that a backgrounded task's completion arrives
# as a spontaneous new event on the chat's still-open stdout, no signal
# file needed. Left in place (env vars still injected into every turn,
# watcher thread still runs) in case anything still relies on the
# convention, but nothing should need to reach for it going forward.
WAKEUP_SIGNAL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(STATE_FILE)),
    "wakeup_signals_" + os.path.splitext(os.path.basename(STATE_FILE))[0],
)
os.makedirs(WAKEUP_SIGNAL_DIR, exist_ok=True)

# chat_id -> {"proc": Popen, "fifo": path} for a login flow in progress.
pending_logins = {}


def load_whitelist():
    ids = {str(OWNER_ID)}
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE) as f:
                raw = f.read()
            for part in raw.replace("\n", ",").split(","):
                part = part.strip()
                if part:
                    ids.add(part)
        except Exception:
            pass
    return ids


def default_claude_config_dir():
    return os.path.abspath(
        os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    )


def _ensure_tenant_credentials(path, source):
    """Seed a new delegated home once; never overwrite its OAuth state."""
    def refreshable(candidate):
        try:
            with open(candidate, encoding="utf-8") as handle:
                oauth = json.load(handle).get("claudeAiOauth") or {}
            return bool(oauth.get("refreshToken"))
        except (OSError, ValueError, AttributeError):
            return False

    if (os.path.exists(path) and not os.path.islink(path)) or not refreshable(source):
        return
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _ensure_tenant_mcp_config(config_dir):
    config_path = os.path.join(config_dir, ".claude.json")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError(f"Claude config root is not an object: {config_path}")
    else:
        config = {}

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"Claude mcpServers is not an object: {config_path}")
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    changed = False
    for name, script in (
        ("delegate-to-codex", "delegate_to_codex_mcp.py"),
        ("send-telegram-file", "send_telegram_file_mcp.py"),
    ):
        if name in servers:
            continue
        servers[name] = {
            "type": "stdio",
            "command": os.path.abspath(sys.executable),
            "args": [os.path.join(repo_dir, script)],
            # Leave this empty: the child must inherit the per-process CHAT_ID
            # injected by claude_env(), never a persisted or caller-chosen id.
            "env": {},
        }
        changed = True
    if not changed:
        return
    temporary = f"{config_path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def ensure_owner_mcp_config():
    _ensure_tenant_mcp_config(account_dir(OWNER_ID))


def _migrate_owner_projects(owner_dir):
    """Copy legacy Claude project sessions once into the owner's tenant home.

    Session UUIDs in bridge state reference JSONL files below
    ``~/.claude/projects``.  Moving only config/persona makes every existing
    ``--resume`` fail after the bridge begins passing a tenant
    ``CLAUDE_CONFIG_DIR``.  Preserve the legacy source and never overwrite a
    tenant project directory that has already been created.
    """
    source = os.path.join(default_claude_config_dir(), "projects")
    destination = os.path.join(owner_dir, "projects")
    if os.path.isdir(source) and not os.path.exists(destination):
        shutil.copytree(source, destination, copy_function=shutil.copy2)


def account_dir(chat_id, state_key=None):
    delegated = state_key is not None and str(state_key) != str(chat_id)
    if delegated:
        d = os.path.join(DELEGATED_ACCOUNTS_DIR, str(chat_id))
        os.makedirs(d, mode=0o700, exist_ok=True)
        shared_dir = account_dir(chat_id)
        # A delegate needs independent credentials because Claude CLI may
        # atomically replace its credentials file during token refresh.
        credentials = os.path.join(shared_dir, ".credentials.json")
        _ensure_tenant_credentials(os.path.join(d, ".credentials.json"), credentials)
        return d
    d = os.path.join(ACCOUNTS_DIR, str(chat_id))
    os.makedirs(d, mode=0o700, exist_ok=True)
    claude_md = os.path.join(d, "CLAUDE.md")
    owner = str(chat_id) == str(OWNER_ID)
    if not os.path.exists(claude_md):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        persona_source = (
            os.path.join(default_claude_config_dir(), "CLAUDE.md")
            if owner else os.path.join(repo_dir, "personality.example.md")
        )
        if os.path.exists(persona_source):
            shutil.copyfile(persona_source, claude_md)
        else:
            # Keep a usable owner account even if the prior optional persona
            # file did not exist; ordinary tenants retain the existing template.
            open(claude_md, "w", encoding="utf-8").close()
        if owner:
            source_config = os.path.expanduser("~/.claude.json")
            if os.path.exists(source_config):
                shutil.copyfile(source_config, os.path.join(d, ".claude.json"))
        else:
            shutil.copyfile(os.path.join(repo_dir, "HANDOFF.md"), os.path.join(d, "handoff.md"))
    if owner:
        _migrate_owner_projects(d)
    # Idempotent (marker-guarded, append-only if missing) for everyone,
    # owner included -- without it, the owner's migrated CLAUDE.md has no
    # working knowledge of the send-telegram-file MCP tool it was just
    # given access to. This does not touch the rest of a /persona rewrite.
    _ensure_tenant_file_send_instructions(claude_md)
    _ensure_tenant_mcp_config(d)
    return d


def tenant_file_outbox(chat_id):
    path = os.path.join(ACCOUNTS_DIR, str(chat_id), "outbox")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _ensure_tenant_file_send_instructions(claude_md):
    try:
        with open(claude_md, encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        return
    if FILE_SEND_AGENTS_MARKER in content:
        return
    with open(claude_md, "w", encoding="utf-8") as handle:
        handle.write(content.rstrip() + "\n\n" + FILE_SEND_AGENTS_SECTION + "\n")


def claude_env(config_dir, chat_id=None, extra_env=None):
    parent_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    env = {
        key: value for key, value in os.environ.items()
        if key not in BRIDGE_ENV_VARS
    }
    if extra_env:
        env.update(extra_env)
    for key in BRIDGE_ENV_VARS:
        env.pop(key, None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    elif parent_config_dir:
        env["CLAUDE_CONFIG_DIR"] = parent_config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    if chat_id is not None:
        # Lets a backgrounded shell command self-report completion without
        # Claude needing to already know/hardcode its own chat_id -- see
        # WAKEUP_SIGNAL_DIR above.
        env["CHAT_ID"] = str(chat_id)
        env["WAKEUP_SIGNAL_DIR"] = WAKEUP_SIGNAL_DIR
        env["CLAUDE_TELEGRAM_OUTBOX"] = tenant_file_outbox(chat_id)
    return env

# ---------------------------------------------------------------------------
BATCH_DEBOUNCE_S = 1.5
pending_batches = {}
batch_timers = {}
pending_batch_generations = {}
pending_batches_lock = threading.Lock()
