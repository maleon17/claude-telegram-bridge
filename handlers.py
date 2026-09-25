import json
import glob
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

from strings import COMMAND_DESCRIPTIONS, current_language, t

from runtime import (
    ACCOUNTS_DIR, BATCH_DEBOUNCE_S, CLAUDE_BIN, MAX_MSG_LEN, OWNER_ID, SERVICE_NAME, WORKDIR, account_dir,
    batch_timers, busy_chats, claude_env, draining, intake_active, intake_queues, intake_lock, load_whitelist,
    pending_batch_generations, pending_batches, pending_batches_lock, pending_logins,
    _write_default_persona_marker,
)
from state_store import (
    _set_chat_language, clear_pending_prompt, clear_session, delegate_key, fetch_account_limits,
    get_account_status, get_delegate_resume_selected, get_effort, get_language, get_model, get_pending_prompt, get_permission_mode,
    get_session, get_usage, get_workspace, list_sessions, pop_delegate_request_id,
    projects_dir_for,
    request_restart, session_message_count, set_account_status, set_effort, set_model,
    set_delegate_request_id, set_delegate_resume_selected, set_pending_delegator, set_permission_mode, set_session,
    set_workspace, set_language,
)
from chat_process import (
    _stop_chat_process, send_turn_to_chat_process, write_last_turn, write_request_result,
)
from telegram_api import (
    download_telegram_file, edit_message, send_document, send_message, send_typing, tg_call,
)
from telegram_format import format_message

MODEL_CATALOG = [
    {"id": "claude-fable-5", "name": "Fable 5", "family": "fable", "version": "5", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-fable-5-1", "name": "Fable 5.1", "family": "fable", "version": "5.1", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-opus-4-5", "name": "Opus 4.5", "family": "opus", "version": "4.5", "efforts": ("low", "medium", "high")},
    {"id": "claude-opus-4-6", "name": "Opus 4.6", "family": "opus", "version": "4.6", "efforts": ("low", "medium", "high", "max")},
    {"id": "claude-opus-4-7", "name": "Opus 4.7", "family": "opus", "version": "4.7", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-opus-4-8", "name": "Opus 4.8", "family": "opus", "version": "4.8", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-opus-5", "name": "Opus 5", "family": "opus", "version": "5", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-sonnet-4-5", "name": "Sonnet 4.5", "family": "sonnet", "version": "4.5", "efforts": ()},
    {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6", "family": "sonnet", "version": "4.6", "efforts": ("low", "medium", "high", "max")},
    {"id": "claude-sonnet-5", "name": "Sonnet 5", "family": "sonnet", "version": "5", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "claude-haiku-4-5", "name": "Haiku 4.5", "family": "haiku", "version": "4.5", "efforts": ()},
]
MODEL_ALIASES = tuple(dict.fromkeys(model["family"] for model in MODEL_CATALOG))
ALL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_PICKER_FAMILIES = ("opus", "sonnet", "fable", "haiku")

PERMISSION_MODES = ("bypass", "default", "acceptEdits", "plan")
SESSION_PREFIX_RE = re.compile(r"[0-9a-f][0-9a-f-]{0,35}")

pending_batch_output_chats = {}
# A short retry keeps a debounce batch intact while the serial intake worker
# is still downloading/transcribing a later message from this same chat.
BATCH_RETRY_S = 0.2

# Owner-only /update local-Bot-API setup.  This deliberately lives only in
# memory: api_id/api_hash must never reach state.json or a Claude prompt.
pending_local_bot_api_setups = {}
pending_local_bot_api_setups_lock = threading.Lock()
# chat_id -> ids of messages/files sent by /persona.  Reply identity, rather
# than a "next message" state machine, is the authorization boundary.
persona_message_ids = {}


def _model_for_id(model_id):
    return next((model for model in MODEL_CATALOG if model["id"] == model_id), None)


def _model_from_spec(spec):
    wanted = " ".join(str(spec or "").strip().lower().split())
    if not wanted or wanted == "default":
        return None
    compact = wanted.replace(" ", "-").replace(".", "-")
    if compact.startswith("claude-"):
        compact = compact[7:]
    parts = compact.split("-")
    family = parts[0]
    if family not in MODEL_ALIASES:
        raise ValueError(t('handlers_model_from_spec_1', value0=family, value1=', '.join(MODEL_ALIASES)))
    version = ".".join(parts[1:]) if len(parts) > 1 else None
    candidates = [model for model in MODEL_CATALOG if model["family"] == family]
    if version:
        model = next((model for model in candidates if model["version"] == version), None)
        if model is None:
            raise ValueError(t('handlers_model_from_spec_2', value0=family, value1=version))
        return model
    return candidates[-1]


def resolve_model_spec(spec):
    """Resolve a user-facing model choice to a CLI id, or None for default."""
    model = _model_from_spec(spec)
    return model["id"] if model else None


def supported_efforts(model_id):
    """Return effort choices for a model; the unpinned CLI default accepts all."""
    if model_id is None:
        return ALL_EFFORTS
    model = _model_for_id(model_id)
    return model["efforts"] if model else ()


def resolve_effort_spec(model_id, spec):
    """Validate an effort for a resolved model, returning None for default."""
    wanted = str(spec or "").strip().lower()
    if not wanted or wanted == "default":
        return None
    if wanted in supported_efforts(model_id):
        return wanted
    model = _model_for_id(model_id)
    name = model["name"] if model else t('handlers_resolve_effort_spec_1')
    raise ValueError(t('handlers_resolve_effort_spec_2', value0=spec, value1=name))


def _effort_label(model_id, effort):
    if not supported_efforts(model_id):
        return t('handlers_effort_label_1')
    if effort is None:
        return t('handlers_effort_label_2')
    return effort


def render_model_picker(current_model, current_effort):
    lines = [t('handlers_render_model_picker_1')]
    models = sorted(
        MODEL_CATALOG,
        key=lambda model: tuple(int(part) for part in model["version"].split(".")),
        reverse=True,
    )
    models.sort(key=lambda model: MODEL_PICKER_FAMILIES.index(model["family"]))
    for model in models:
        marker = "●" if model["id"] == current_model else "○"
        lines.append(f"{marker} {model['name']} — `/model {model['id']}`")
    marker = "●" if current_model is None else "○"
    lines.append(t('handlers_render_model_picker_2', value0=marker))
    lines.append(t('handlers_render_model_picker_3', value0=_effort_label(current_model, current_effort)))
    return "\n".join(lines)


def render_effort_picker(model_id, current_effort):
    model = _model_for_id(model_id)
    if model and not model["efforts"]:
        return t('handlers_render_effort_picker_1', value0=model['name'])
    name = model["name"] if model else t('handlers_render_effort_picker_2')
    lines = [t('handlers_render_effort_picker_3', value0=name)]
    for effort in supported_efforts(model_id):
        marker = "●" if effort == current_effort else "○"
        lines.append(f"{marker} {effort} — `/effort {effort}`")
    marker = "●" if current_effort is None else "○"
    lines.append(t('handlers_render_effort_picker_4', value0=marker))
    return "\n".join(lines)


def process_key_for_incoming(chat_id, state):
    """Route a real Telegram message to the active delegated process."""
    delegate_process = delegate_key(chat_id)
    return delegate_process if (
        delegate_process in busy_chats or get_delegate_resume_selected(state, chat_id)
    ) else chat_id


def process_key_for_command(chat_id, state):
    """Like process_key_for_incoming, also preserve delegated denial state."""
    delegate_process = delegate_key(chat_id)
    if (
        delegate_process in busy_chats
        or get_pending_prompt(state, delegate_process)
        or get_delegate_resume_selected(state, chat_id)
    ):
        return delegate_process
    return chat_id


def _delegate_error(chat_id, text, request_id=None):
    send_message(chat_id, text)
    if request_id:
        # Only the caller that made this request sees the rejection; another
        # caller's in-flight delegation keeps its own result channel.
        write_request_result(request_id, text, ok=False)
    else:
        write_last_turn(chat_id, text, delegated=True)

COMMANDS = tuple(COMMAND_DESCRIPTIONS["ru"].items())


def _bridge_env_file():
    """The service environment file generated by setup.sh (overrideable in tests)."""
    return os.environ.get(
        "BRIDGE_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.env")
    )


def _env_file_value(path, name):
    """Read the final plain KEY=value value without importing it into env."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"Could not read {path}: {exc}", flush=True)
        return ""
    prefix = f"{name}="
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    return values[-1].strip() if values else ""


def _configured_local_bot_api_url():
    return _env_file_value(_bridge_env_file(), "TELEGRAM_API_URL") or os.environ.get(
        "TELEGRAM_API_URL", ""
    ).strip()


def _telegram_bot_api_unit():
    unit = os.environ.get("TELEGRAM_BOT_API_UNIT", "telegram-bot-api").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", unit):
        raise ValueError(t('handlers_telegram_bot_api_unit_1'))
    return unit


def _telegram_bot_api_env_file():
    return os.path.expanduser(
        os.environ.get("TELEGRAM_BOT_API_ENV_FILE", "~/.config/telegram-bot-api/env")
    )


def _systemctl_is_active(unit):
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{unit}.service"],
            capture_output=True, text=True, timeout=15, check=False,
        ).returncode == 0
    except OSError as exc:
        print(f"Could not check {unit}.service: {exc}", flush=True)
        return False


def _systemctl_unit_exists(unit):
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", f"{unit}.service", "--no-legend"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return bool((result.stdout or "").strip())
    except OSError as exc:
        print(f"Could not list {unit}.service: {exc}", flush=True)
        return False


def _repair_local_bot_api_service():
    """Restart only an existing configured local-server unit, never install it."""
    try:
        unit = _telegram_bot_api_unit()
    except ValueError as exc:
        return f"⚠️ {exc}"
    if _systemctl_is_active(unit):
        return ""
    if not _systemctl_unit_exists(unit):
        return (
            t('handlers_repair_local_bot_api_service_1')
        )
    systemctl_bin = shutil.which("systemctl") or "systemctl"
    try:
        result = subprocess.run(
            ["sudo", "-n", systemctl_bin, "restart", f"{unit}.service"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        return t('handlers_repair_local_bot_api_service_2', value0=exc)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        return t('handlers_repair_local_bot_api_service_2', value0=detail or t('handlers_repair_local_bot_api_service_3'))
    return t('handlers_repair_local_bot_api_service_4')


def _write_telegram_api_url(url):
    """Atomically replace TELEGRAM_API_URL in bridge.env after a successful switch."""
    if "\n" in url or "\r" in url:
        raise RuntimeError(t('handlers_write_telegram_api_url_1'))
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(t('handlers_write_telegram_api_url_1'))
    path = _bridge_env_file()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        raise RuntimeError(t('handlers_write_telegram_api_url_2', value0=path))
    prefix = "TELEGRAM_API_URL="
    lines = [line for line in lines if not line.startswith(prefix)]
    lines.append(prefix + url)
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".bridge.env.", dir=directory, text=True)
    try:
        os.fchmod(fd, mode or 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _send_local_bot_api_progress(status, text):
    message_id = status.get("message_id")
    if message_id is not None:
        edit_message(status["chat_id"], message_id, text)
        return
    result = send_message(status["chat_id"], text)
    if result.get("ok"):
        status["message_id"] = (result.get("result") or {}).get("message_id")


def _finish_local_bot_api_setup(chat_id, status, error=None):
    with pending_local_bot_api_setups_lock:
        pending_local_bot_api_setups.pop(chat_id, None)
    if error:
        _send_local_bot_api_progress(status, t('handlers_finish_local_bot_api_setup_1', value0=error[-500:]))
    else:
        _send_local_bot_api_progress(status, t('handlers_finish_local_bot_api_setup_2'))
    request_restart(chat_id)


def _run_local_bot_api_install(chat_id, api_id=None, api_hash=None, language="ru"):
    """Run the installer and irreversible switch off the intake worker thread."""
    current_language.set(language)
    status = {"chat_id": chat_id, "message_id": None}
    output_tail = []
    stage_text = {
        "dependencies": t('handlers_run_local_bot_api_install_1'),
        "build": t('handlers_run_local_bot_api_install_2'),
        "install": t('handlers_run_local_bot_api_install_3'),
        "done": t('handlers_run_local_bot_api_install_4'),
    }
    try:
        _send_local_bot_api_progress(status, t('handlers_run_local_bot_api_install_5'))
        env = dict(os.environ)
        if api_id is not None:
            env["TELEGRAM_API_ID"] = api_id
        if api_hash is not None:
            env["TELEGRAM_API_HASH"] = api_hash
        install_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "install-local-bot-api.sh")
        process = subprocess.Popen(
            [install_script, "--yes"], cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env, text=True, bufsize=1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        # The credentials now exist only in the child's environment; do not
        # retain them in the in-memory dialog record either.
        api_id = api_hash = None
        local_url = ""
        for line in process.stdout:
            line = line.rstrip("\n")
            output_tail.append(line)
            output_tail = output_tail[-20:]
            if line.startswith("STAGE:"):
                message = stage_text.get(line[len("STAGE:"):])
                if message:
                    _send_local_bot_api_progress(status, message)
            elif line.startswith("REUSE:existing"):
                _send_local_bot_api_progress(status, t('handlers_run_local_bot_api_install_6'))
            elif line.startswith("LOCAL_BOT_API_URL="):
                local_url = line[len("LOCAL_BOT_API_URL="):].strip()
        if process.wait() != 0:
            raise RuntimeError("\n".join(output_tail) or t('handlers_run_local_bot_api_install_7'))
        if not local_url:
            raise RuntimeError(t('handlers_run_local_bot_api_install_8'))
        switch_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "switch-to-local-bot-api.sh")
        switch_env = dict(os.environ)
        switch_env["BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        switch_env["LOCAL_BOT_API_LOGOUT_CONFIRM"] = "yes"
        switched = subprocess.run(
            [switch_script, local_url], cwd=os.path.dirname(os.path.abspath(__file__)),
            env=switch_env, capture_output=True, text=True, timeout=60, check=False,
        )
        if switched.returncode:
            raise RuntimeError((switched.stderr or switched.stdout or t('handlers_run_local_bot_api_install_9'))[-500:])
        _write_telegram_api_url(local_url)
    except Exception as exc:
        _finish_local_bot_api_setup(chat_id, status, str(exc))
    else:
        _finish_local_bot_api_setup(chat_id, status)


def _start_local_bot_api_install(chat_id, api_id=None, api_hash=None):
    """Start exactly one local-server installer; credentials stay in memory."""
    if not _systemctl_unit_exists(_telegram_bot_api_unit()):
        dependencies = ("git", "cmake", "gperf", "g++", "make")
        if not all(shutil.which(name) for name in dependencies) or not (
            os.path.isfile("/usr/include/openssl/ssl.h") and os.path.isfile("/usr/include/zlib.h")
        ):
            status = {"chat_id": chat_id, "message_id": None}
            _finish_local_bot_api_setup(
                chat_id, status,
                t('handlers_start_local_bot_api_install_1'),
            )
            return False
    with pending_local_bot_api_setups_lock:
        flow = pending_local_bot_api_setups.get(chat_id)
        if not flow or flow.get("stage") == "installing":
            return False
        pending_local_bot_api_setups[chat_id] = {"stage": "installing"}
    threading.Thread(
        target=_run_local_bot_api_install,
        args=(chat_id, api_id, api_hash, current_language.get()), daemon=True,
    ).start()
    return True


def _handle_local_bot_api_setup_reply(chat_id, text):
    """Consume the owner-only /update dialog before ordinary prompt routing."""
    if str(chat_id) != str(OWNER_ID):
        return False
    with pending_local_bot_api_setups_lock:
        flow = pending_local_bot_api_setups.get(chat_id)
        stage = flow.get("stage") if flow else None
    if not stage:
        return False
    if stage == "installing":
        if text.strip().lower().split("@", 1)[0] == "/update":
            send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_1'))
        return True
    if stage == "awaiting_yes_no":
        if text.strip().lower() in ("да", "yes", "ага"):
            if os.path.exists(_telegram_bot_api_env_file()):
                _start_local_bot_api_install(chat_id)
            else:
                with pending_local_bot_api_setups_lock:
                    pending_local_bot_api_setups[chat_id] = {"stage": "awaiting_api_id"}
                send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_2'))
            return True
        with pending_local_bot_api_setups_lock:
            pending_local_bot_api_setups.pop(chat_id, None)
        send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_3'))
        request_restart(chat_id)
        return True
    if stage == "awaiting_api_id":
        api_id = text.strip()
        if not re.fullmatch(r"[0-9]+", api_id):
            send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_4'))
            return True
        with pending_local_bot_api_setups_lock:
            pending_local_bot_api_setups[chat_id] = {"stage": "awaiting_api_hash", "api_id": api_id}
        send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_5'))
        return True
    if stage == "awaiting_api_hash":
        api_hash = text.strip()
        if not api_hash:
            send_message(chat_id, t('handlers_handle_local_bot_api_setup_reply_6'))
            return True
        with pending_local_bot_api_setups_lock:
            api_id = pending_local_bot_api_setups.get(chat_id, {}).get("api_id")
        _start_local_bot_api_install(chat_id, api_id, api_hash)
        return True
    return False


def _persona_path(chat_id):
    return os.path.join(account_dir(chat_id), "CLAUDE.md")


def _language_persona_path(chat_id):
    """Read an existing persona without account_dir's legacy instruction append."""
    path = os.path.join(ACCOUNTS_DIR, str(chat_id), "CLAUDE.md")
    if not os.path.exists(path):
        account_dir(chat_id)
    return path


LANGUAGE_NAMES = {
    "en": "English", "ru": "Russian", "uk": "Ukrainian",
    "kk": "Kazakh", "de": "German",
}


def _persona_template(language):
    name = "personality.example.md" if language == "ru" else f"personality.example.{language}.md"
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8") as handle:
        return handle.read()


def _persona_is_default(chat_id):
    path = _language_persona_path(chat_id)
    marker = os.path.join(os.path.dirname(path), ".persona_default_sha256")
    try:
        with open(path, "rb") as handle:
            contents = handle.read()
        with open(marker, encoding="ascii") as handle:
            digest = handle.read().strip()
    except (OSError, UnicodeError):
        return False
    return hashlib.sha256(contents).hexdigest() == digest


def _seed_default_persona(chat_id, language):
    path = _language_persona_path(chat_id)
    contents = _persona_template(language)
    _write_persona(path, contents)
    _write_default_persona_marker(os.path.dirname(path), contents.encode("utf-8"))


def _translate_persona_file(chat_id, target_lang, state):
    """Translate with the tenant's Claude account and remove only this call's session."""
    path = _language_persona_path(chat_id)
    with open(path, encoding="utf-8") as handle:
        original = handle.read()
    if not original.strip():
        raise ValueError("persona is empty")
    tenant_dir = os.path.dirname(path)
    language = LANGUAGE_NAMES[target_lang]
    prompt = (
        f"Translate the following CLAUDE.md content into {language}. Preserve its "
        "structure, tone and meaning faithfully; do not summarize or reword. "
        f"If it instructs the agent to always answer in a named language, change "
        f"that instruction to always answer in {language}. Output only the "
        "translated file content, with no commentary, code fences or extra text. "
        "Do not use tools or access files. The source text follows:\n\n" + original
    )
    session_id = str(uuid.uuid4())
    command = [
        CLAUDE_BIN, "-p", "--output-format", "json",
        "--permission-mode", "plan", "--permission-prompts", "none",
        "--strict-mcp-config", "--tools", "",
        "--session-id", session_id,
    ]
    model = get_model(state, chat_id)
    if model:
        command.extend(("--model", model))
    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            timeout=180, check=False, cwd=tenant_dir, env=claude_env(tenant_dir),
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "Claude failed").strip()[-500:])
        translated = json.loads(result.stdout).get("result", "").strip()
    finally:
        # Claude can choose its own sanitized project directory. The UUID is
        # unique to this invocation; never remove another session's file.
        for session_path in glob.glob(
            os.path.join(tenant_dir, "projects", "**", f"{session_id}.jsonl"),
            recursive=True,
        ):
            os.unlink(session_path)
    refusal = re.match(r"(?i)^(?:sorry|i cannot|i can't|unable to)\b", translated)
    original_has_headings = any(line.startswith("#") for line in original.splitlines())
    translated_has_headings = any(line.startswith("#") for line in translated.splitlines())
    if (len(translated) < max(12, len(original.strip()) // 3)
            or translated.startswith("```") or translated.endswith("```")
            or refusal or (original_has_headings and not translated_has_headings)):
        raise ValueError("Claude returned incomplete persona content")
    _write_persona(path, translated + "\n")


def _finish_language_message(chat_id, progress, message):
    message_id = (progress.get("result") or {}).get("message_id") if progress and progress.get("ok") else None
    if isinstance(message_id, int):
        try:
            if edit_message(chat_id, message_id, message).get("ok"):
                return
        except Exception as exc:
            print(f"chat={chat_id} could not edit language progress: {exc}", flush=True)
    send_message(chat_id, message)


def _write_persona(path, contents):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".persona.", dir=directory, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _remember_persona_message(chat_id, result):
    if result.get("ok"):
        message_id = (result.get("result") or {}).get("message_id")
        if isinstance(message_id, int):
            persona_message_ids.setdefault(str(chat_id), set()).add(message_id)


def send_persona(chat_id):
    """Send the current persona as one text message or a UTF-8 Markdown file."""
    path = _persona_path(chat_id)
    with open(path, encoding="utf-8") as handle:
        contents = handle.read()
    if len(format_message(contents)) <= MAX_MSG_LEN:
        result = send_message(chat_id, contents)
    else:
        fd, temporary = tempfile.mkstemp(prefix="persona-", suffix=".md", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(contents)
            result = send_document(chat_id, temporary, caption=t('handlers_send_persona_1'))
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    _remember_persona_message(chat_id, result)
    return result


def handle_persona_reply(chat_id, message):
    """Replace an owner persona only for a reply to a /persona snapshot."""
    if str(chat_id) != str(OWNER_ID):
        return False
    reply = message.get("reply_to_message") or {}
    reply_id = reply.get("message_id") or message.get("reply_to_message_id")
    if reply_id not in persona_message_ids.get(str(chat_id), set()):
        return False
    document = message.get("document") or {}
    if document:
        try:
            local_path = download_telegram_file(
                chat_id, document["file_id"], filename_hint=document.get("file_name"),
                file_size=document.get("file_size"),
            )
            with open(local_path, encoding="utf-8") as handle:
                contents = handle.read()
        except UnicodeDecodeError:
            send_message(chat_id, t('handlers_handle_persona_reply_1'))
            return True
        except Exception as exc:
            send_message(chat_id, t('handlers_handle_persona_reply_2', value0=exc))
            return True
    else:
        contents = message.get("text")
        if not isinstance(contents, str):
            send_message(chat_id, t('handlers_handle_persona_reply_3'))
            return True
    if not contents.strip():
        send_message(chat_id, t('handlers_handle_persona_reply_4'))
        return True
    _write_persona(_persona_path(chat_id), contents)
    send_message(chat_id, t('handlers_handle_persona_reply_5'))
    return True


def handle_command(chat_id, text, state, offset=None):
    _set_chat_language(state, chat_id)
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().strip().lstrip("/.")
    arg = arg.strip()

    if cmd == "start":
        return True

    if cmd == "language":
        code = arg.lower()
        if code not in LANGUAGE_NAMES:
            send_message(chat_id, t('language_usage'))
            return True
        set_language(state, chat_id, code)
        current_language.set(code)
        set_chat_commands(chat_id, code)
        progress = send_message(chat_id, t('language_progress'))
        try:
            if _persona_is_default(chat_id):
                _seed_default_persona(chat_id, code)
            else:
                _translate_persona_file(chat_id, code, state)
        except Exception as exc:
            print(f"chat={chat_id} persona language switch failed: {exc}", flush=True)
            _finish_language_message(chat_id, progress, t('language_persona_failed', error=str(exc)[-500:]))
            return True
        _finish_language_message(chat_id, progress, t('language_changed'))
        return True

    if cmd == "persona":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, t('handlers_handle_command_1'))
            return True
        if arg.lower() == "reset":
            _seed_default_persona(chat_id, get_language(state, chat_id))
            send_message(chat_id, t('handlers_handle_command_2'))
        elif arg:
            send_message(chat_id, t('handlers_handle_command_3'))
        else:
            send_persona(chat_id)
        return True

    if cmd == "new":
        # Session id isn't part of a chat process's restart signature (see
        # _ensure_chat_process) -- has to be torn down explicitly here, or
        # the next message would land on the OLD live process/session
        # instead of picking up the cleared one. If a turn happens to be
        # active right now, this doubles as an implicit /stop -- treated
        # as reasonable given the user explicitly asked to start fresh.
        cancel_pending_batch(chat_id)
        _stop_chat_process(chat_id)
        clear_pending_prompt(state, chat_id)
        clear_session(state, chat_id)
        send_message(chat_id, t('handlers_handle_command_4'))
        return True

    if cmd == "compact":
        # Real CLI slash command, sent as an ordinary prompt onto the
        # chat's persistent process -- see _chat_reader_loop's
        # "system"/"status" handling for how the result gets reported.
        cancel_pending_batch(chat_id)
        spawn_turn(chat_id, "/compact", state)
        return True

    pdir = projects_dir_for(account_dir(chat_id), get_workspace(state, chat_id))

    if cmd == "sessions":
        sessions = list_sessions(pdir)
        if not sessions:
            send_message(chat_id, t('handlers_handle_command_5'))
            return True
        lines = [t('handlers_handle_command_6')]
        current = get_session(state, chat_id)
        for sid, mtime, preview in sessions:
            marker = t('handlers_handle_command_7') if sid == current else ""
            lines.append(f"`{sid[:8]}` {mtime} {preview}{marker}")
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "resume":
        if not arg:
            send_message(chat_id, t('handlers_handle_command_8'))
            return True
        # arg is untrusted (whitelisted-chat-controlled). Session ids are
        # UUIDs, so accept only a hex/dash prefix and match it against the
        # directory listing -- no path joining or glob patterns built from
        # user input at all.
        wanted = arg.lower()
        if not SESSION_PREFIX_RE.fullmatch(wanted):
            send_message(chat_id, t('handlers_handle_command_9', value0=arg))
            return True
        delegate_process = delegate_key(chat_id)
        delegate_pdir = projects_dir_for(
            account_dir(chat_id, state_key=delegate_process),
            get_workspace(state, delegate_process),
        )

        def matching_sessions(directory):
            try:
                names = os.listdir(directory)
            except OSError:
                names = []
            session_ids = sorted(
                name[:-6] for name in names
                if name.endswith(".jsonl") and os.path.isfile(os.path.join(directory, name))
            )
            exact = [sid for sid in session_ids if sid.lower() == wanted]
            return exact or [sid for sid in session_ids if sid.lower().startswith(wanted)]

        matches = [(chat_id, sid) for sid in matching_sessions(pdir)]
        matches += [(delegate_process, sid) for sid in matching_sessions(delegate_pdir)]
        if not matches:
            send_message(chat_id, t('handlers_handle_command_9', value0=arg))
            return True
        if len(matches) > 1:
            lines = [t('handlers_handle_command_10', value0=arg)]
            lines.extend(f"`{sid}`" for _, sid in matches[:10])
            if len(matches) > 10:
                lines.append(t('handlers_handle_command_11', value0=len(matches) - 10))
            send_message(chat_id, "\n".join(lines))
            return True
        process_key, sid = matches[0]
        cancel_pending_batch(process_key)
        _stop_chat_process(process_key)  # see /new -- same reason
        clear_pending_prompt(state, process_key)
        set_session(state, process_key, sid)
        delegated = process_key == delegate_process
        set_delegate_resume_selected(state, chat_id, delegated)
        send_message(chat_id, t('handlers_handle_command_12', value0=t('handlers_handle_command_13') if delegated else '', value1=sid[:8]))
        return True

    if cmd == "usage":
        session_id = get_session(state, chat_id)
        u = get_usage(state, chat_id, session_id)
        msg_count = session_message_count(session_id, pdir)
        context_tokens = u.get("last_context_tokens")
        model = get_model(state, chat_id) or "default"
        effort = get_effort(state, chat_id)

        def fmt(n):
            return f"{n:,}".replace(",", " ")

        lines = [
            t('usage_session_header'),
            t('handlers_handle_command_14', value0=session_id[:8] if session_id else t('handlers_handle_command_15'), value1=model, value2=_effort_label(get_model(state, chat_id), effort)),
            t('usage_messages', count=msg_count if msg_count is not None else '—'),
            (
                t('usage_context', count=fmt(context_tokens))
                if context_tokens
                else t('usage_context_empty')
            ),
            "",
            t('usage_tokens_header'),
            t('usage_calls', count=u['calls']),
            t('usage_tokens_line', input=fmt(u['input_tokens']), output=fmt(u['output_tokens']),
              cache_read=fmt(u['cache_read_tokens']), cache_write=fmt(u['cache_creation_tokens'])),
            t('handlers_handle_command_16', value0=u['cost_usd']),
        ]

        by_model = u.get("by_model") or {}
        if by_model:
            lines.append("")
            lines.append(t('usage_by_model'))
            for name, mu in by_model.items():
                lines.append(
                    t('usage_model_line', name=name, input=fmt(mu['input_tokens']),
                      output=fmt(mu['output_tokens']), cost=mu['cost_usd'])
                )

        limits = fetch_account_limits(account_dir(chat_id))
        lines.append("")
        lines.append(t('usage_limits_header'))
        for ln in limits.splitlines():
            lines.append(ln)

        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "model":
        if not arg:
            send_message(chat_id, render_model_picker(get_model(state, chat_id), get_effort(state, chat_id)))
            return True

        try:
            model_id = resolve_model_spec(arg)
        except ValueError:
            send_message(
                chat_id,
                t('handlers_handle_command_17', value0=arg, value1=render_model_picker(get_model(state, chat_id), get_effort(state, chat_id))),
            )
            return True

        previous_effort = get_effort(state, chat_id)
        set_model(state, chat_id, model_id)
        effort_reset = previous_effort is not None and previous_effort not in supported_efforts(model_id)
        if effort_reset:
            set_effort(state, chat_id, None)
        model = _model_for_id(model_id)
        name = model["name"] if model else t('handlers_handle_command_18')
        lines = [
            t('handlers_handle_command_19', value0=name) + (f" (`{model_id}`)" if model_id else ""),
            t('handlers_handle_command_20', value0=_effort_label(model_id, None if effort_reset else previous_effort)),
        ]
        if effort_reset:
            lines.append(t('handlers_handle_command_21'))
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "effort":
        model_id = get_model(state, chat_id)
        current_effort = get_effort(state, chat_id)
        model = _model_for_id(model_id)
        if not arg:
            send_message(chat_id, render_effort_picker(model_id, current_effort))
            return True
        try:
            effort = resolve_effort_spec(model_id, arg)
        except ValueError:
            name = model["name"] if model else t('handlers_resolve_effort_spec_1')
            send_message(
                chat_id,
                t('handlers_handle_command_22', value0=arg, value1=name, value2=render_effort_picker(model_id, current_effort)),
            )
            return True
        set_effort(state, chat_id, effort)
        name = model["name"] if model else t('handlers_render_effort_picker_2')
        send_message(chat_id, t('handlers_handle_command_23', value0=name, value1=_effort_label(model_id, effort)))
        return True

    if cmd == "mode":
        if not arg:
            current = get_permission_mode(state, chat_id) or "bypass"
            send_message(
                chat_id,
                t('handlers_handle_command_24', value0=current, value1=', '.join(PERMISSION_MODES)),
            )
            return True
        choice = {mode.lower(): mode for mode in PERMISSION_MODES}.get(arg.lower().strip())
        if choice is None:
            send_message(chat_id, t('handlers_handle_command_25', value0=', '.join(PERMISSION_MODES)))
            return True
        set_permission_mode(state, chat_id, choice)
        send_message(chat_id, t('handlers_handle_command_26', value0=choice))
        return True

    if cmd == "workspace":
        if not arg:
            current = get_workspace(state, chat_id)
            send_message(chat_id, t('handlers_handle_command_27', value0=current))
            return True
        if arg.lower() == "default":
            set_workspace(state, chat_id, None)
            send_message(chat_id, t('handlers_handle_command_28', value0=WORKDIR))
            return True
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.isdir(path):
            send_message(chat_id, t('handlers_handle_command_29', value0=path))
            return True
        set_workspace(state, chat_id, path)
        send_message(chat_id, t('handlers_handle_command_30', value0=path))
        return True

    if cmd == "status":
        session_id = get_session(state, chat_id)
        model = get_model(state, chat_id) or "default"
        effort = _effort_label(get_model(state, chat_id), get_effort(state, chat_id))
        mode = get_permission_mode(state, chat_id) or "bypass"
        workspace = get_workspace(state, chat_id)
        busy = t('handlers_handle_command_31') if chat_id in busy_chats else t('handlers_handle_command_32')
        acc = get_account_status(state, chat_id) or t('handlers_handle_command_33')
        lines = [
            t('handlers_handle_command_34'),
            t('handlers_handle_command_35', value0=session_id[:8] if session_id else t('handlers_handle_command_15')),
            t('handlers_handle_command_36', value0=model, value1=effort),
            t('handlers_handle_command_37', value0=mode),
            t('status_workspace', workspace=workspace),
            t('handlers_handle_command_38', value0=busy),
            t('handlers_handle_command_39', value0=acc),
        ]
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "login":
        if arg not in ("", "delegate"):
            send_message(chat_id, t('handlers_handle_command_40'))
            return True
        start_login(chat_id, state, delegated=(arg == "delegate"))
        send_message(chat_id, t('handlers_handle_command_41'))
        return True

    if cmd == "restart":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, t('handlers_handle_command_42'))
            return True
        if not SERVICE_NAME:
            send_message(chat_id, t('handlers_handle_command_43'))
            return True
        # Don't restart immediately -- if a turn (possibly this very one) is
        # still in flight, killing the process now would cut it off mid-
        # answer. Just record the request; main()'s loop performs the
        # actual restart once busy_chats is empty, so it always happens
        # between turns, never in the middle of one.
        cancel_pending_batch(chat_id)
        request_restart(chat_id)
        if busy_chats:
            send_message(
                chat_id,
                t('handlers_handle_command_44'),
            )
        return True

    if cmd == "update":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, t('handlers_handle_command_45'))
            return True
        if not SERVICE_NAME:
            send_message(chat_id, t('handlers_handle_command_43'))
            return True
        with pending_local_bot_api_setups_lock:
            if pending_local_bot_api_setups.get(chat_id):
                send_message(chat_id, t('handlers_handle_command_46'))
                return True
        send_message(chat_id, t('handlers_handle_command_47'))
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.sh")
        try:
            result = subprocess.run(
                [script], capture_output=True, text=True, timeout=900, check=False,
            )
        except Exception as e:
            send_message(chat_id, t('handlers_handle_command_48', value0=e))
            return True
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()[-2000:]
            send_message(chat_id, t('handlers_handle_command_49', value0=output))
            return True
        summary = (result.stdout or "").strip().splitlines()[-1:] or [t('handlers_handle_command_50')]
        # Preserve /update's existing promise: anything awaiting its old
        # debounce window must not become a turn while setup/restart follows.
        cancel_pending_batch(chat_id)
        configured_url = _configured_local_bot_api_url()
        if not configured_url:
            with pending_local_bot_api_setups_lock:
                pending_local_bot_api_setups[chat_id] = {"stage": "awaiting_yes_no"}
            send_message(chat_id, f"✅ {summary[0]}.")
            send_message(
                chat_id,
                t('handlers_handle_command_51'),
            )
            return True
        repair_status = _repair_local_bot_api_service()
        # Same deferred-restart mechanism as /restart: never kill a turn
        # (possibly this very one) mid-answer, only restart once idle.
        request_restart(chat_id)
        note = t('handlers_handle_command_52') if busy_chats else t('handlers_handle_command_53')
        suffix = f"\n{repair_status}" if repair_status else ""
        send_message(chat_id, f"✅ {summary[0]}.{suffix}{note}")
        return True

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _command_payload(language):
    descriptions = COMMAND_DESCRIPTIONS[language]
    return {"commands": [
        {"command": command, "description": descriptions[command]} for command, _ in COMMANDS
    ]}


def set_chat_commands(chat_id, language):
    tg_call("setMyCommands", {
        **_command_payload(language), "scope": {"type": "chat", "chat_id": chat_id},
    })


def register_commands(state=None):
    for language in COMMAND_DESCRIPTIONS:
        payload = _command_payload(language)
        for scope in (None, {"type": "all_private_chats"}):
            params = {**payload, "language_code": language}
            if scope:
                params["scope"] = scope
            tg_call("setMyCommands", params)
            if language == "ru":
                tg_call("setMyCommands", {**payload, **({"scope": scope} if scope else {})})
    if state is not None:
        for chat_id, data in state.items():
            if str(chat_id).isdecimal() and isinstance(data, dict):
                language = data.get("language", "ru")
                if language in COMMAND_DESCRIPTIONS:
                    set_chat_commands(int(chat_id), language)


def _run_turn_thread(
    chat_id, prompt, state, force_permission_mode=None, output_chat_id=None, delegated=False,
    extra_env=None,
):
    """dispatch_turn() now only ensures the chat's persistent process and
    writes the prompt to its stdin -- it returns almost immediately, long
    before the turn is actually done. So busy_chats is only cleared HERE
    on a failure that happens before/during that write (nothing will ever
    reach the reader thread to clear it in that case); on success,
    clearing it is _chat_reader_loop's job once it sees this turn's
    "result" event (or the process dying mid-turn)."""
    _set_chat_language(state, chat_id)
    try:
        dispatch_turn(
            chat_id,
            prompt,
            state,
            force_permission_mode=force_permission_mode,
            output_chat_id=output_chat_id,
            delegated=delegated,
            extra_env=extra_env,
        )
    except Exception:
        err = traceback.format_exc()[-1500:]
        print(err, flush=True)
        error_text = t('bridge_process_message_12', value0=err)
        send_message(output_chat_id or chat_id, error_text)
        if delegated:
            write_last_turn(output_chat_id or chat_id, error_text, delegated=True)
            write_request_result(pop_delegate_request_id(state, chat_id), error_text, ok=False)
        busy_chats.discard(chat_id)


def _refuse_while_draining(output_chat_id, delegated):
    message = t('runtime_module_1')
    send_message(output_chat_id, message)
    if delegated:
        write_last_turn(output_chat_id, message, delegated=True, ok=False)


def spawn_turn(
    chat_id, prompt, state, force_permission_mode=None, output_chat_id=None, delegated=False,
    extra_env=None,
):
    """Run a turn in the background so the poll loop stays responsive to
    /stop and other commands while `claude` is running. Returns True if the
    turn was started."""
    with intake_lock:
        if draining.is_set():
            refused, busy = True, False
        else:
            refused, busy = False, chat_id in busy_chats
            if not busy:
                busy_chats.add(chat_id)
    if refused:
        _refuse_while_draining(output_chat_id or chat_id, delegated)
        return False
    if busy:
        send_message(
            output_chat_id or chat_id,
            t('handlers_spawn_turn_1'),
        )
        return False
    _start_turn_thread(
        chat_id, prompt, state, force_permission_mode=force_permission_mode,
        output_chat_id=output_chat_id, delegated=delegated, extra_env=extra_env,
    )
    return True


def _start_turn_thread(
    chat_id, prompt, state, force_permission_mode=None, output_chat_id=None, delegated=False,
    extra_env=None,
):
    threading.Thread(
        target=_run_turn_thread,
        args=(chat_id, prompt, state),
        kwargs={
            "force_permission_mode": force_permission_mode,
            "output_chat_id": output_chat_id,
            "delegated": delegated,
            "extra_env": extra_env,
        },
        daemon=True,
    ).start()


def start_delegate_turn(
    chat_id, prompt, state, resume_session_id=None, workspace=None,
    model_spec=None, effort_spec=None, env=None, request_id=None,
):
    """Start one isolated, persistent turn for bridge_exec.py.

    The delegate has its own bookkeeping key and Claude process, while all
    Telegram output is still addressed to the real owner chat. A missing
    resume id deliberately tears down any idle delegate process and clears
    its session so every default delegation starts a fresh conversation.
    """
    _set_chat_language(state, chat_id)
    delegate_process = delegate_key(chat_id)
    requested_session_id = str(resume_session_id or "").strip() or None
    requested_env = dict(env or {})
    model_requested = model_spec is not None
    effort_requested = effort_spec is not None
    try:
        requested_model = resolve_model_spec(model_spec) if model_requested else None
    except ValueError as exc:
        _delegate_error(chat_id, str(exc), request_id)
        return False

    if delegate_process in busy_chats:
        _delegate_error(chat_id, t('handlers_start_delegate_turn_1'), request_id)
        return False

    if get_account_status(state, delegate_process) != "ready":
        _delegate_error(
            chat_id, t('handlers_start_delegate_turn_2'), request_id,
        )
        return False

    if requested_session_id and requested_env:
        _delegate_error(chat_id, t('handlers_start_delegate_turn_3'), request_id)
        return False

    cancel_pending_batch(delegate_process)
    owner_session_id = get_session(state, chat_id)
    delegate_session_id = get_session(state, delegate_process)
    inherited_model = get_model(state, chat_id)
    current_delegate_model = get_model(state, delegate_process)
    final_model = (
        requested_model if model_requested else
        (current_delegate_model if requested_session_id else inherited_model)
    )
    try:
        requested_effort = (
            resolve_effort_spec(final_model, effort_spec) if effort_requested else None
        )
    except ValueError as exc:
        _delegate_error(chat_id, str(exc), request_id)
        return False

    if requested_session_id:
        is_matching_delegate = bool(
            delegate_session_id
            and (
                delegate_session_id == requested_session_id
                or delegate_session_id.startswith(requested_session_id)
            )
        )
        if not is_matching_delegate or (
            owner_session_id and delegate_session_id == owner_session_id
        ):
            _delegate_error(
                chat_id,
                t('handlers_start_delegate_turn_4'),
                request_id,
            )
            return False
        if workspace:
            set_workspace(state, delegate_process, workspace)
        if model_requested:
            set_model(state, delegate_process, requested_model)
        stored_effort = get_effort(state, delegate_process)
        if effort_requested:
            set_effort(state, delegate_process, requested_effort)
        elif stored_effort not in supported_efforts(final_model):
            set_effort(state, delegate_process, None)
    else:
        # A default delegation is always fresh. Stop only the idle delegate
        # slot; the owner's independent process is never touched here.
        _stop_chat_process(delegate_process)
        clear_session(state, delegate_process)
        set_delegate_resume_selected(state, chat_id, False)
        clear_pending_prompt(state, delegate_process)
        set_model(state, delegate_process, final_model)
        inherited_effort = get_effort(state, chat_id)
        if effort_requested:
            set_effort(state, delegate_process, requested_effort)
        elif inherited_effort in supported_efforts(final_model):
            set_effort(state, delegate_process, inherited_effort)
        else:
            set_effort(state, delegate_process, None)
        set_permission_mode(state, delegate_process, get_permission_mode(state, chat_id))
        set_workspace(state, delegate_process, workspace or get_workspace(state, chat_id))

    # This marker is stored on the delegate entry, so the footer can expose
    # the owner's pre-delegation session without ever overwriting the owner
    # entry when the delegate result arrives.
    set_pending_delegator(state, delegate_process, owner_session_id or "")
    set_delegate_request_id(state, delegate_process, request_id)
    started = spawn_turn(
        delegate_process,
        prompt,
        state,
        output_chat_id=chat_id,
        delegated=True,
        extra_env=requested_env or None,
    )
    if not started:
        refused_id = pop_delegate_request_id(state, delegate_process)
        write_request_result(refused_id, t('handlers_start_delegate_turn_5'), ok=False)
    return started


# Forwarding a batch of messages (or just typing several in quick succession)
# is one logical user turn.  Keep the same debounce while a persistent Claude
# process is busy too: the eventual combined prompt is either a fresh turn or
# one mid-turn stream-json injection, depending on the process state at flush
# time.  A generation number makes cancellation safe even if an old Timer
# wakes up after cancel() has already been called.
def cancel_pending_batch(chat_id):
    with pending_batches_lock:
        timer = batch_timers.pop(chat_id, None)
        pending_batches.pop(chat_id, None)
        pending_batch_output_chats.pop(chat_id, None)
        pending_batch_generations[chat_id] = pending_batch_generations.get(chat_id, 0) + 1
    if timer:
        timer.cancel()


def _flush_pending_batch(chat_id, state, generation):
    _set_chat_language(state, chat_id)
    # Taking the batch and reserving the chat happen under intake_lock, so a
    # deferred restart can never observe the gap between them as "idle".
    retry_timer = None
    old_timer = None
    with intake_lock:
        with pending_batches_lock:
            if pending_batch_generations.get(chat_id) != generation:
                return
            # A slow attachment can still be in this chat's serial intake
            # worker long after an earlier text prompt scheduled the timer.
            # Do not consume the partial batch: a later queue_prompt must
            # join it before the first Claude turn starts.
            if intake_queues.get(chat_id) or chat_id in intake_active:
                retry_generation = generation + 1
                pending_batch_generations[chat_id] = retry_generation
                old_timer = batch_timers.pop(chat_id, None)
                retry_timer = threading.Timer(
                    BATCH_RETRY_S, _flush_pending_batch,
                    args=(chat_id, state, retry_generation),
                )
                retry_timer.daemon = True
                batch_timers[chat_id] = retry_timer
            else:
                prompts = pending_batches.pop(chat_id, [])
                batch_timers.pop(chat_id, None)
                output_chat_id = pending_batch_output_chats.pop(chat_id, None)
                pending_batch_generations.pop(chat_id, None)
        if retry_timer is None:
            if not prompts:
                return
            refused = draining.is_set()
            inject = not refused and chat_id in busy_chats
            if not refused and not inject:
                busy_chats.add(chat_id)

    if retry_timer is not None:
        if old_timer:
            old_timer.cancel()
        retry_timer.start()
        return

    delegated = output_chat_id is not None
    if refused:
        _refuse_while_draining(output_chat_id or chat_id, delegated=False)
        return
    combined = prompts[0] if len(prompts) == 1 else "\n\n---\n\n".join(prompts)
    try:
        if inject:
            dispatch_turn(
                chat_id, combined, state, output_chat_id=output_chat_id, delegated=delegated,
            )
        else:
            _start_turn_thread(
                chat_id, combined, state, output_chat_id=output_chat_id, delegated=delegated,
            )
    except Exception:
        if not inject:
            busy_chats.discard(chat_id)
        err = traceback.format_exc()[-1500:]
        print(err, flush=True)
        send_message(output_chat_id or chat_id, t('bridge_process_message_12', value0=err))


def queue_prompt(chat_id, prompt, state, output_chat_id=None):
    with intake_lock:
        if draining.is_set():
            _refuse_while_draining(output_chat_id or chat_id, delegated=False)
            return False
        with pending_batches_lock:
            pending_batches.setdefault(chat_id, []).append(prompt)
            if output_chat_id is not None:
                pending_batch_output_chats[chat_id] = output_chat_id
            generation = pending_batch_generations.get(chat_id, 0) + 1
            pending_batch_generations[chat_id] = generation
            old_timer = batch_timers.get(chat_id)
            timer = threading.Timer(
                BATCH_DEBOUNCE_S, _flush_pending_batch, args=(chat_id, state, generation)
            )
            timer.daemon = True
            batch_timers[chat_id] = timer
    if old_timer:
        old_timer.cancel()
    timer.start()
    return True


def route_prompt(chat_id, prompt, state):
    process_key = process_key_for_incoming(chat_id, state)
    if process_key == chat_id:
        queue_prompt(process_key, prompt, state)
    else:
        queue_prompt(process_key, prompt, state, output_chat_id=chat_id)


def dispatch_turn(
    chat_id, prompt, state, force_permission_mode=None, output_chat_id=None, delegated=False,
    extra_env=None,
):
    """Write one prompt onto chat_id's persistent process. Non-blocking --
    see send_turn_to_chat_process's docstring. Delivery (final answer,
    attachments, denial handling via /approve|/approve session|/deny) all
    happens later, asynchronously, in _chat_reader_loop / _deliver_turn_result."""
    telegram_chat_id = chat_id if output_chat_id is None else output_chat_id
    settings_chat_id = chat_id if delegated else telegram_chat_id
    send_typing(telegram_chat_id)

    _set_chat_language(state, output_chat_id or chat_id)
    model = get_model(state, settings_chat_id)
    effort = get_effort(state, settings_chat_id)
    permission_mode = force_permission_mode or get_permission_mode(state, settings_chat_id)
    workspace = get_workspace(state, settings_chat_id)
    config_dir = account_dir(
        telegram_chat_id,
        state_key=chat_id if delegated else None,
    )

    send_turn_to_chat_process(
        chat_id,
        prompt,
        state,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        workspace=workspace,
        config_dir=config_dir,
        output_chat_id=telegram_chat_id,
        delegated=delegated,
        extra_env=extra_env,
    )


LOGIN_TIMEOUT_S = 180


def send_whitelist_prompt(chat_id):
    text = (
        t('handlers_send_whitelist_prompt_1', value0=chat_id)
    )
    tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": format_message(text),
        "parse_mode": "MarkdownV2",
        "reply_markup": {
            "inline_keyboard": [[{"text": t('handlers_send_whitelist_prompt_2'), "callback_data": "check_whitelist"}]]
        },
    })


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
        params["show_alert"] = show_alert
    tg_call("answerCallbackQuery", params)


def _cleanup_login(chat_id, info, terminate=True):
    """Stop one login attempt and remove only its private FIFO."""
    if pending_logins.get(chat_id) is info:
        pending_logins.pop(chat_id, None)
    proc = info.get("proc") if isinstance(info, dict) else None
    if terminate and proc is not None and proc.poll() is None:
        proc.terminate()
    fifo_path = info.get("fifo") if isinstance(info, dict) else None
    if fifo_path and os.path.exists(fifo_path):
        try:
            os.remove(fifo_path)
        except OSError:
            pass


def _login_credentials_ready(info):
    """Require credentials written by this login, not stale `auth status`."""
    path = os.path.join(info["config_dir"], ".credentials.json")
    try:
        if os.path.getmtime(path) < info["started_at"] - 1:
            return False
        with open(path, encoding="utf-8") as handle:
            oauth = (json.load(handle).get("claudeAiOauth") or {})
        return bool(
            oauth.get("accessToken") and oauth.get("refreshToken")
            and int(oauth.get("expiresAt") or 0) > (time.time() + 30) * 1000
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def start_login(chat_id, state, delegated=False):
    """Start Claude's interactive OAuth flow and relay it through Telegram.

    Every chat logs into its isolated account directory. ``/login delegate``
    authenticates the separate home used by delegated turns.
    """
    _set_chat_language(state, chat_id)
    target_key = delegate_key(chat_id) if delegated else chat_id
    config_dir = account_dir(chat_id, state_key=target_key)
    login_dir = config_dir or os.path.join(os.path.dirname(__file__), "login")
    os.makedirs(login_dir, mode=0o700, exist_ok=True)
    fifo_path = os.path.join(login_dir, f"login_stdin_{chat_id}.fifo")

    previous = pending_logins.pop(chat_id, None)
    if previous:
        old_proc = previous.get("proc")
        if old_proc is not None and old_proc.poll() is None:
            old_proc.terminate()
    if os.path.exists(fifo_path):
        os.remove(fifo_path)
    os.mkfifo(fifo_path)

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    shell_cmd = f'exec script -qefc "{CLAUDE_BIN} auth login --claudeai" /dev/null 0<>{fifo_path}'
    proc = subprocess.Popen(
        ["bash", "-c", shell_cmd],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    info = {
        "proc": proc, "fifo": fifo_path, "config_dir": config_dir,
        "state_key": target_key, "delegated": delegated, "started_at": time.time(),
    }
    pending_logins[chat_id] = info
    set_account_status(state, target_key, "awaiting_code")

    def reader():
        _set_chat_language(state, chat_id)
        deadline = time.time() + LOGIN_TIMEOUT_S
        url_seen = False
        try:
            for line in proc.stdout:
                m = re.search(r"https://\S+", line.strip())
                if m:
                    url_seen = True
                    send_message(
                        chat_id,
                        t('handlers_reader_1'),
                    )
                    # A URL button instead of a raw pasted link -- keeps the
                    # giant OAuth URL out of the chat text entirely.
                    tg_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": t('handlers_reader_2'),
                        "reply_markup": {
                            "inline_keyboard": [[{"text": t('handlers_reader_2'), "url": m.group(0)}]]
                        },
                    })
                    break
                if time.time() > deadline:
                    break
        except Exception:
            pass
        if not url_seen and pending_logins.get(chat_id) is info:
            set_account_status(state, target_key, "login_failed")
            _cleanup_login(chat_id, info)
            send_message(chat_id, t('handlers_reader_3'))

    threading.Thread(target=reader, daemon=True).start()


def feed_login_code(chat_id, code, state):
    info = pending_logins.get(chat_id)
    if not info:
        return False
    target_key = info["state_key"]
    try:
        with open(info["fifo"], "w") as f:
            f.write(code.strip() + "\n")
    except Exception:
        if pending_logins.get(chat_id) is info:
            set_account_status(state, target_key, "login_failed")
            _cleanup_login(chat_id, info)
        send_message(chat_id, t('handlers_feed_login_code_1'))
        return False

    def check():
        _set_chat_language(state, chat_id)
        time.sleep(3)
        for _ in range(10):
            try:
                r = subprocess.run(
                    [CLAUDE_BIN, "auth", "status"],
                    env=claude_env(info["config_dir"]),
                    capture_output=True, text=True, timeout=15,
                )
                d = json.loads(r.stdout)
                if d.get("loggedIn") and _login_credentials_ready(info):
                    if info["delegated"]:
                        set_account_status(state, target_key, "ready")
                        success_message = t('handlers_check_1')
                    elif str(chat_id) == str(OWNER_ID):
                        set_account_status(state, target_key, "ready")
                        success_message = t('handlers_check_2')
                    else:
                        set_account_status(state, target_key, "awaiting_display_name")
                        success_message = t('handlers_check_3')
                    _cleanup_login(chat_id, info)
                    # A persistent Claude process may have cached the expired
                    # OAuth session.  Recreate it on the next prompt so the
                    # fresh credentials are definitely used.
                    _stop_chat_process(target_key)
                    send_message(chat_id, success_message)
                    return
            except Exception:
                pass
            time.sleep(2)
        if pending_logins.get(chat_id) is info:
            set_account_status(state, target_key, "login_failed")
            _cleanup_login(chat_id, info)
        send_message(chat_id, t('handlers_check_4'))

    threading.Thread(target=check, daemon=True).start()
    return True


def handle_onboarding(chat_id, user_id, text, state, whitelist):
    """Returns True if this update was fully handled here (whitelist prompt /
    login kickoff / code consumption) and the main loop should move on.
    Returns False if the account is ready and normal dispatch should proceed."""
    _set_chat_language(state, chat_id)
    if str(user_id) not in whitelist:
        send_whitelist_prompt(chat_id)
        return True

    # This is called before normal command/prompt routing by bridge.py, so
    # api_id/api_hash replies cannot be persisted as a Claude conversation.
    if _handle_local_bot_api_setup_reply(chat_id, text or ""):
        return True

    active_login = pending_logins.get(chat_id)
    if active_login and active_login.get("delegated"):
        if text and text.strip().lower().lstrip("/.").split()[0:1] == ["login"]:
            start_login(chat_id, state, delegated=True)
            send_message(chat_id, t('handlers_handle_onboarding_1'))
        elif text and not text.startswith(("/", ".")):
            feed_login_code(chat_id, text.strip(), state)
        else:
            send_message(chat_id, t('handlers_handle_onboarding_2'))
        return True

    status = get_account_status(state, chat_id)
    if status == "ready":
        return False

    if status == "awaiting_code":
        if text and text.strip().lower().lstrip("/.").split()[0:1] == ["login"]:
            start_login(chat_id, state)
            send_message(chat_id, t('handlers_handle_onboarding_3'))
            return True
        if text and not text.startswith(("/", ".")):
            feed_login_code(chat_id, text.strip(), state)
        else:
            send_message(chat_id, t('handlers_handle_onboarding_2'))
        return True

    elif status == "awaiting_display_name":
        tenant_dir = account_dir(chat_id)
        if tenant_dir:
            claude_md = os.path.join(tenant_dir, "CLAUDE.md")
            if os.path.exists(claude_md):
                was_default = _persona_is_default(chat_id)
                with open(claude_md, encoding="utf-8") as f:
                    personality = f.read()
                updated = personality.replace("<user>", (text or "").strip())
                _write_persona(claude_md, updated)
                if was_default:
                    _write_default_persona_marker(tenant_dir, updated.encode("utf-8"))
        set_account_status(state, chat_id, "ready")
        send_message(chat_id, t('handlers_handle_onboarding_4'))
        return True

    start_login(chat_id, state)
    send_message(chat_id, t('handlers_handle_onboarding_5'))
    return True


def handle_callback_query(cq, state):
    data = cq.get("data")
    from_id = cq.get("from", {}).get("id")
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    if chat_id:
        _set_chat_language(state, chat_id)
    if not chat_id or data != "check_whitelist":
        answer_callback_query(cq["id"])
        return

    whitelist = load_whitelist()
    if str(from_id) not in whitelist:
        answer_callback_query(cq["id"], t('handlers_handle_callback_query_1'), show_alert=True)
        return

    answer_callback_query(cq["id"], t('handlers_handle_callback_query_2'))
    status = get_account_status(state, chat_id)
    if status == "ready":
        send_message(chat_id, t('handlers_handle_callback_query_3'))
    elif status != "awaiting_code":
        start_login(chat_id, state)
        send_message(chat_id, t('handlers_handle_onboarding_5'))
