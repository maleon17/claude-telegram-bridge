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
import hashlib
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strings import t
from telegram_format import format_message, strip_mdv2, escape_mdv2
from delegate_to_codex_mcp import codex_bridge_dir

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
FILE_PATH_RE = re.compile(r"(/(?:[\w.\-]+/)+[\w.\-]+)")
FILE_PATH_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "pdf", "zip", "tar",
    "gz", "txt", "md", "csv", "json", "py", "js", "ts", "html", "mp3",
    "mp4", "wav", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
})

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
DRAINING_TEXT = t('runtime_module_1')
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

# One-time guidance appended to every non-owner persona: keep work out of the
# shared home directory, and tidy up only what the agent itself created.
WORKSPACE_RULES = {
    "ru": """
## Рабочая папка

Не складывай файлы прямо в домашнюю директорию. Если твоя текущая рабочая
папка — не домашняя директория, работай в ней. Иначе для каждой задачи или
проекта используй отдельную папку: ту, что назвал пользователь, или уже
существующую папку проекта; если такой нет — создай
`~/projects/<короткое-имя>` и работай там.

Если в домашней директории остались файлы, которые ты сам создал раньше (ты
точно знаешь это из своей истории или памяти), при случае перенеси их в папку
соответствующего проекта и сообщи пользователю, что и куда перенёс. Чужие
файлы, репозитории, скрытые файлы (имя начинается с точки) и всё, в чём не
уверен, не трогай — спроси пользователя.
""".strip(),
    "en": """
## Working folder

Do not drop files directly into the home directory. If your current working
directory is not the home directory, work there. Otherwise use a separate
folder for each task or project: the one the user named or an existing project
folder; if there is none, create `~/projects/<short-name>` and work there.

If the home directory still holds files you created earlier yourself (you know
this for certain from your own history or memory), move them into the matching
project folder when convenient and tell the user what went where. Do not touch
other people's files, repositories, hidden files (names starting with a dot)
or anything you are unsure about — ask the user.
""".strip(),
    "uk": """
## Робоча папка

Не складай файли прямо в домашню директорію. Якщо твоя поточна робоча папка —
не домашня директорія, працюй у ній. Інакше для кожного завдання чи проєкту
використовуй окрему папку: ту, яку назвав користувач, або вже наявну папку
проєкту; якщо такої немає — створи `~/projects/<коротка-назва>` і працюй там.

Якщо в домашній директорії залишилися файли, які ти сам створив раніше (ти
точно знаєш це зі своєї історії чи пам'яті), за нагоди перенеси їх у папку
відповідного проєкту й повідом користувачеві, що і куди перенесено. Чужі
файли, репозиторії, приховані файли (назва починається з крапки) і все, у чому
не впевнений, не чіпай — запитай користувача.
""".strip(),
    "kk": """
## Жұмыс бумасы

Файлдарды тікелей үй каталогына салма. Егер ағымдағы жұмыс бумаң үй каталогы
болмаса, сонда жұмыс істе. Әйтпесе әр тапсырма немесе жоба үшін бөлек буманы
қолдан: пайдаланушы атаған немесе бұрыннан бар жоба бумасын; ондай бума
болмаса, `~/projects/<қысқа-атау>` бумасын жасап, сонда жұмыс істе.

Егер үй каталогында бұрын өзің жасаған файлдар қалса (мұны өз тарихыңнан
немесе жадыңнан нақты білесің), реті келгенде оларды тиісті жоба бумасына
көшіріп, пайдаланушыға нені қайда көшіргеніңді айт. Бөтен файлдарға,
репозиторийлерге, жасырын файлдарға (атауы нүктеден басталады) және сенімді
емес нәрсенің бәріне тиіспе — пайдаланушыдан сұра.
""".strip(),
    "de": """
## Arbeitsordner

Lege Dateien nicht direkt im Home-Verzeichnis ab. Wenn dein aktuelles
Arbeitsverzeichnis nicht das Home-Verzeichnis ist, arbeite dort. Andernfalls
nutze für jede Aufgabe oder jedes Projekt einen eigenen Ordner: den vom Nutzer
genannten oder einen bereits vorhandenen Projektordner; gibt es keinen, lege
`~/projects/<kurzer-name>` an und arbeite dort.

Liegen im Home-Verzeichnis noch Dateien, die du früher selbst erstellt hast
(du weißt das sicher aus deinem eigenen Verlauf oder Gedächtnis), verschiebe
sie bei Gelegenheit in den passenden Projektordner und sag dem Nutzer, was
wohin verschoben wurde. Fremde Dateien, Repositories, versteckte Dateien (Name
beginnt mit einem Punkt) und alles, bei dem du unsicher bist, rührst du nicht
an — frag den Nutzer.
""".strip(),
}
# The localized 'User' heading of the stock persona tells which language an
# existing persona file is written in.
WORKSPACE_RULES_LANGUAGE_HINTS = (
    ("## User\n", "en"),
    ("## Користувач\n", "uk"),
    ("## Пайдаланушы\n", "kk"),
    ("## Nutzer\n", "de"),
)

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
BRIDGE_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE",
    "LC_MESSAGES", "LC_NUMERIC", "LC_TIME", "TERM", "SHELL", "TMPDIR", "TZ",
    "USER", "LOGNAME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
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
        script_path = os.path.join(repo_dir, script)
        ours = (servers.get(name) or {}).get("args") == [script_path]
        # CHAT_ID is deliberately never persisted here: the child must inherit
        # the per-process id injected by claude_env(), not a stored or
        # caller-chosen one.
        server_env = {}
        if name == "delegate-to-codex":
            peer_dir = codex_bridge_dir()
            if peer_dir is None:
                # No Codex bridge on this host: do not offer a tool that can
                # only fail, and withdraw the entry this bridge seeded earlier
                # (never one the tenant pointed somewhere else).
                if ours:
                    del servers[name]
                    changed = True
                continue
            # The MCP server runs with Claude's filtered environment, so it
            # is told where the peer lives explicitly.
            server_env = {"CODEX_TELEGRAM_BRIDGE_DIR": str(peer_dir)}
            if ours and servers[name].get("env") != server_env:
                servers[name]["env"] = server_env
                changed = True
        if name in servers:
            continue
        servers[name] = {
            "type": "stdio",
            "command": os.path.abspath(sys.executable),
            "args": [script_path],
            "env": server_env,
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
        os.chmod(d, 0o700)
        legacy_link = os.path.join(d, ".credentials.json")
        if os.path.islink(legacy_link):
            os.unlink(legacy_link)
        return d
    d = os.path.join(ACCOUNTS_DIR, str(chat_id))
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    claude_md = os.path.join(d, "CLAUDE.md")
    owner = str(chat_id) == str(OWNER_ID)
    seeded_default = False
    if not os.path.exists(claude_md):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        persona_source = (
            os.path.join(default_claude_config_dir(), "CLAUDE.md")
            if owner else os.path.join(repo_dir, "personality.example.md")
        )
        if os.path.exists(persona_source):
            shutil.copyfile(persona_source, claude_md)
            seeded_default = not owner
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
    if seeded_default:
        with open(claude_md, "rb") as handle:
            _write_default_persona_marker(d, handle.read())
    if not owner:
        _ensure_tenant_workspace_rules(d, claude_md)
    _ensure_tenant_mcp_config(d)
    return d


def _write_default_persona_marker(tenant_dir, contents):
    marker = os.path.join(tenant_dir, ".persona_default_sha256")
    temporary = f"{marker}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="ascii") as handle:
            handle.write(hashlib.sha256(contents).hexdigest() + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ensure_tenant_workspace_rules(tenant_dir, persona_path):
    """Append the working-folder guidance to an existing persona, exactly once.

    New tenants get it from the stock persona; this reaches the ones created
    before it existed. A flag file records that it was offered, so a tenant
    who later removes the section does not get it back. A persona that was
    still the unmodified default stays recognised as the default.
    """
    flag = os.path.join(tenant_dir, ".workspace_rules_seeded")
    if os.path.exists(flag):
        return
    try:
        with open(persona_path, encoding="utf-8") as handle:
            content = handle.read()
    except (FileNotFoundError, UnicodeError):
        return
    if not any(rules.splitlines()[0] in content for rules in WORKSPACE_RULES.values()):
        language = next(
            (lang for hint, lang in WORKSPACE_RULES_LANGUAGE_HINTS if hint in content), "ru",
        )
        marker = os.path.join(tenant_dir, ".persona_default_sha256")
        try:
            with open(marker, encoding="ascii") as handle:
                was_default = (
                    handle.read().strip()
                    == hashlib.sha256(content.encode("utf-8")).hexdigest()
                )
        except (OSError, UnicodeError):
            was_default = False
        updated = content.rstrip() + "\n\n" + WORKSPACE_RULES[language] + "\n"
        temporary = f"{persona_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                handle.write(updated)
            os.chmod(temporary, 0o600)
            os.replace(temporary, persona_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if was_default:
            _write_default_persona_marker(tenant_dir, updated.encode("utf-8"))
    with open(flag, "w", encoding="ascii"):
        pass
    os.chmod(flag, 0o600)


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
    if FILE_SEND_AGENTS_MARKER in content or (
        "CLAUDE_TELEGRAM_OUTBOX" in content and "send_telegram_file" in content
    ):
        return
    with open(claude_md, "w", encoding="utf-8") as handle:
        handle.write(content.rstrip() + "\n\n" + FILE_SEND_AGENTS_SECTION + "\n")


def claude_env(config_dir, chat_id=None, extra_env=None):
    parent_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    env = {key: os.environ[key] for key in BRIDGE_ENV_ALLOWLIST if key in os.environ}
    if extra_env:
        env.update(extra_env)
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
