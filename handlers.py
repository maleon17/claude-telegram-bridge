import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit

from runtime import (
    BATCH_DEBOUNCE_S, CLAUDE_BIN, DRAINING_TEXT, OWNER_ID, SERVICE_NAME, WORKDIR, account_dir,
    batch_timers, busy_chats, claude_env, draining, intake_active, intake_queues, intake_lock, load_whitelist,
    pending_batch_generations, pending_batches, pending_batches_lock, pending_logins,
)
from state_store import (
    clear_pending_prompt, clear_session, delegate_key, fetch_account_limits,
    get_account_status, get_delegate_resume_selected, get_effort, get_model, get_pending_prompt, get_permission_mode,
    get_session, get_usage, get_workspace, list_sessions, pop_delegate_request_id,
    projects_dir_for,
    request_restart, session_message_count, set_account_status, set_effort, set_model,
    set_delegate_request_id, set_delegate_resume_selected, set_pending_delegator, set_permission_mode, set_session,
    set_workspace,
)
from chat_process import (
    _stop_chat_process, send_turn_to_chat_process, write_last_turn, write_request_result,
)
from telegram_api import edit_message, send_message, send_typing, tg_call
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
        raise ValueError(f"Неизвестное семейство «{family}». Доступно: {', '.join(MODEL_ALIASES)}, default")
    version = ".".join(parts[1:]) if len(parts) > 1 else None
    candidates = [model for model in MODEL_CATALOG if model["family"] == family]
    if version:
        model = next((model for model in candidates if model["version"] == version), None)
        if model is None:
            raise ValueError(f"У {family} нет версии {version}.")
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
    name = model["name"] if model else "модели по умолчанию CLI"
    raise ValueError(f"Мощность «{spec}» недоступна для {name}.")


def _effort_label(model_id, effort):
    if not supported_efforts(model_id):
        return "не поддерживается"
    if effort is None:
        return "по умолчанию"
    return effort


def render_model_picker(current_model, current_effort):
    lines = ["🧠 Модели Claude:"]
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
    lines.append(f"{marker} По умолчанию CLI — `/model default`")
    lines.append(f"⚡ Мощность: {_effort_label(current_model, current_effort)}. Выбрать: /effort")
    return "\n".join(lines)


def render_effort_picker(model_id, current_effort):
    model = _model_for_id(model_id)
    if model and not model["efforts"]:
        return f"⚡ {model['name']} не поддерживает настройку мощности."
    name = model["name"] if model else "по умолчанию CLI"
    lines = [f"⚡ Мощность модели {name}:"]
    for effort in supported_efforts(model_id):
        marker = "●" if effort == current_effort else "○"
        lines.append(f"{marker} {effort} — `/effort {effort}`")
    marker = "●" if current_effort is None else "○"
    lines.append(f"{marker} По умолчанию CLI — `/effort default`")
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

COMMANDS = [
    ("new", "Начать новую сессию"),
    ("sessions", "Список последних сессий"),
    ("resume", "Продолжить сессию по id"),
    ("status", "Текущее состояние: сессия/модель/режим/workspace"),
    ("stop", "Прервать текущий запрос"),
    ("compact", "Сжать контекст текущей сессии (экономит токены/деньги)"),
    ("usage", "Токены, стоимость и лимиты аккаунта"),
    ("model", "Модель: /model opus, /model claude-sonnet-5, /model default"),
    ("effort", "Мощность модели: /effort high, /effort default"),
    ("mode", "Режим подтверждений: bypass/default/acceptEdits/plan"),
    ("workspace", "Рабочая директория для этой сессии"),
    ("approve", "Разрешить заблокированное действие (once/session)"),
    ("deny", "Отклонить заблокированное действие"),
    ("login", "Переподключить свой аккаунт Claude"),
    ("restart", "Перезапустить бота (только для владельца)"),
    ("update", "Обновить бота из git и перезапустить (только для владельца)"),
]


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
        raise ValueError("Некорректное имя юнита локального Bot API.")
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
            "⚠️ Локальный сервер настроен, но его systemd-юнит не установлен. "
            "Не запускаю установку из бота; прогони scripts/install-local-bot-api.sh вручную."
        )
    systemctl_bin = shutil.which("systemctl") or "systemctl"
    try:
        result = subprocess.run(
            ["sudo", "-n", systemctl_bin, "restart", f"{unit}.service"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        return f"⚠️ Не смог перезапустить локальный сервер: {exc}"
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        return f"⚠️ Не смог перезапустить локальный сервер: {detail or 'команда завершилась с ошибкой'}"
    return "🔧 Локальный сервер был неактивен; отправил рестарт юнита."


def _write_telegram_api_url(url):
    """Atomically replace TELEGRAM_API_URL in bridge.env after a successful switch."""
    if "\n" in url or "\r" in url:
        raise RuntimeError("Установщик вернул недопустимый адрес локального сервера")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Установщик вернул недопустимый адрес локального сервера")
    path = _bridge_env_file()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        raise RuntimeError(f"Не найден env-файл бриджа: {path}")
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
        _send_local_bot_api_progress(status, f"⚠️ Не удалось включить локальный сервер: {error[-500:]}")
    else:
        _send_local_bot_api_progress(status, "✅ Локальный сервер включён. Перезапускаю бота…")
    request_restart(chat_id)


def _run_local_bot_api_install(chat_id, api_id=None, api_hash=None):
    """Run the installer and irreversible switch off the intake worker thread."""
    status = {"chat_id": chat_id, "message_id": None}
    output_tail = []
    stage_text = {
        "dependencies": "⏳ Проверяю зависимости сборки…",
        "build": "⏳ Собираю локальный Bot API сервер (это может занять несколько минут)…",
        "install": "⏳ Устанавливаю локальный Bot API сервер…",
        "done": "⏳ Локальный сервер готов, переключаю бота…",
    }
    try:
        _send_local_bot_api_progress(status, "⏳ Готовлю локальный Bot API сервер…")
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
                _send_local_bot_api_progress(status, "⏳ Использую уже настроенный локальный сервер…")
            elif line.startswith("LOCAL_BOT_API_URL="):
                local_url = line[len("LOCAL_BOT_API_URL="):].strip()
        if process.wait() != 0:
            raise RuntimeError("\n".join(output_tail) or "установщик завершился с ошибкой")
        if not local_url:
            raise RuntimeError("установщик не сообщил адрес локального сервера")
        switch_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "switch-to-local-bot-api.sh")
        switch_env = dict(os.environ)
        switch_env["BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        switch_env["LOCAL_BOT_API_LOGOUT_CONFIRM"] = "yes"
        switched = subprocess.run(
            [switch_script, local_url], cwd=os.path.dirname(os.path.abspath(__file__)),
            env=switch_env, capture_output=True, text=True, timeout=60, check=False,
        )
        if switched.returncode:
            raise RuntimeError((switched.stderr or switched.stdout or "переключатель завершился с ошибкой")[-500:])
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
                "не хватает зависимостей сборки; прогони scripts/install-local-bot-api.sh вручную.",
            )
            return False
    with pending_local_bot_api_setups_lock:
        flow = pending_local_bot_api_setups.get(chat_id)
        if not flow or flow.get("stage") == "installing":
            return False
        pending_local_bot_api_setups[chat_id] = {"stage": "installing"}
    threading.Thread(
        target=_run_local_bot_api_install, args=(chat_id, api_id, api_hash), daemon=True,
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
            send_message(chat_id, "Установка локального Bot API уже идёт, дождись её завершения.")
        return True
    if stage == "awaiting_yes_no":
        if text.strip().lower() in ("да", "yes", "ага"):
            if os.path.exists(_telegram_bot_api_env_file()):
                _start_local_bot_api_install(chat_id)
            else:
                with pending_local_bot_api_setups_lock:
                    pending_local_bot_api_setups[chat_id] = {"stage": "awaiting_api_id"}
                send_message(chat_id, "Пришли api_id.")
            return True
        with pending_local_bot_api_setups_lock:
            pending_local_bot_api_setups.pop(chat_id, None)
        send_message(chat_id, "Ладно. Перезапускаю бота, чтобы обновление вступило в силу…")
        request_restart(chat_id)
        return True
    if stage == "awaiting_api_id":
        api_id = text.strip()
        if not re.fullmatch(r"[0-9]+", api_id):
            send_message(chat_id, "Пришли api_id (только цифры).")
            return True
        with pending_local_bot_api_setups_lock:
            pending_local_bot_api_setups[chat_id] = {"stage": "awaiting_api_hash", "api_id": api_id}
        send_message(chat_id, "Пришли api_hash.")
        return True
    if stage == "awaiting_api_hash":
        api_hash = text.strip()
        if not api_hash:
            send_message(chat_id, "Пришли непустой api_hash.")
            return True
        with pending_local_bot_api_setups_lock:
            api_id = pending_local_bot_api_setups.get(chat_id, {}).get("api_id")
        _start_local_bot_api_install(chat_id, api_id, api_hash)
        return True
    return False


def handle_command(chat_id, text, state, offset=None):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().strip().lstrip("/.")
    arg = arg.strip()

    if cmd == "start":
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
        send_message(chat_id, "Начинаю новую сессию.")
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
            send_message(chat_id, "Сессий не найдено.")
            return True
        lines = ["Последние сессии:"]
        current = get_session(state, chat_id)
        for sid, mtime, preview in sessions:
            marker = " ← текущая" if sid == current else ""
            lines.append(f"`{sid[:8]}` {mtime} {preview}{marker}")
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "resume":
        if not arg:
            send_message(chat_id, "Использование: /resume <session_id или префикс>")
            return True
        # arg is untrusted (whitelisted-chat-controlled). Session ids are
        # UUIDs, so accept only a hex/dash prefix and match it against the
        # directory listing -- no path joining or glob patterns built from
        # user input at all.
        wanted = arg.lower()
        if not SESSION_PREFIX_RE.fullmatch(wanted):
            send_message(chat_id, f"Сессия {arg} не найдена.")
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
            send_message(chat_id, f"Сессия {arg} не найдена.")
            return True
        if len(matches) > 1:
            lines = [f"Префикс {arg} подходит к нескольким сессиям, уточни id:"]
            lines.extend(f"`{sid}`" for _, sid in matches[:10])
            if len(matches) > 10:
                lines.append(f"…и ещё {len(matches) - 10}")
            send_message(chat_id, "\n".join(lines))
            return True
        process_key, sid = matches[0]
        cancel_pending_batch(process_key)
        _stop_chat_process(process_key)  # see /new -- same reason
        clear_pending_prompt(state, process_key)
        set_session(state, process_key, sid)
        delegated = process_key == delegate_process
        set_delegate_resume_selected(state, chat_id, delegated)
        send_message(chat_id, f"Продолжаю {'делегированную ' if delegated else ''}сессию {sid[:8]}.")
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
            "📊 **Session**",
            f"`{session_id[:8] if session_id else 'нет активной'}`  •  Model: {model}  •  Мощность: {_effort_label(get_model(state, chat_id), effort)}",
            f"Messages: {msg_count if msg_count is not None else '—'}",
            (
                f"Context: ~{fmt(context_tokens)} tokens"
                if context_tokens
                else "Context: no data yet"
            ),
            "",
            "🔢 **Tokens (this session)**",
            f"{u['calls']} calls",
            f"in {fmt(u['input_tokens'])}  ·  out {fmt(u['output_tokens'])}  ·  "
            f"cache-r {fmt(u['cache_read_tokens'])}  ·  cache-w {fmt(u['cache_creation_tokens'])}",
            f"(~${u['cost_usd']:.4f} эквивалент по API-тарифу)",
        ]

        by_model = u.get("by_model") or {}
        if by_model:
            lines.append("")
            lines.append("**By model**")
            for name, mu in by_model.items():
                lines.append(
                    f"`{name}`  {fmt(mu['input_tokens'])}/{fmt(mu['output_tokens'])} in/out  "
                    f"(~${mu['cost_usd']:.4f})"
                )

        limits = fetch_account_limits(account_dir(chat_id))
        lines.append("")
        lines.append("📈 **Account limits** (subscription, not credits)")
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
                f"Модель «{arg}» недоступна.\n\n"
                f"{render_model_picker(get_model(state, chat_id), get_effort(state, chat_id))}",
            )
            return True

        previous_effort = get_effort(state, chat_id)
        set_model(state, chat_id, model_id)
        effort_reset = previous_effort is not None and previous_effort not in supported_efforts(model_id)
        if effort_reset:
            set_effort(state, chat_id, None)
        model = _model_for_id(model_id)
        name = model["name"] if model else "По умолчанию CLI"
        lines = [
            f"🧠 Модель: {name}" + (f" (`{model_id}`)" if model_id else ""),
            f"Мощность: {_effort_label(model_id, None if effort_reset else previous_effort)}",
        ]
        if effort_reset:
            lines.append("Выбранная мощность не поддерживается новой моделью и сброшена.")
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
            name = model["name"] if model else "модели по умолчанию CLI"
            send_message(
                chat_id,
                f"Мощность «{arg}» недоступна для {name}.\n\n"
                f"{render_effort_picker(model_id, current_effort)}",
            )
            return True
        set_effort(state, chat_id, effort)
        name = model["name"] if model else "по умолчанию CLI"
        send_message(chat_id, f"⚡ Мощность {name}: {_effort_label(model_id, effort)}")
        return True

    if cmd == "mode":
        if not arg:
            current = get_permission_mode(state, chat_id) or "bypass"
            send_message(
                chat_id,
                f"Текущий режим: `{current}`\nДоступно: {', '.join(PERMISSION_MODES)}\n\n"
                "bypass — без подтверждений (по умолчанию)\n"
                "default — каждое опасное действие требует /approve\n"
                "acceptEdits — правки файлов авто, остальное требует /approve\n"
                "plan — только чтение, ничего не меняет",
            )
            return True
        choice = {mode.lower(): mode for mode in PERMISSION_MODES}.get(arg.lower().strip())
        if choice is None:
            send_message(chat_id, f"Неизвестный режим. Доступно: {', '.join(PERMISSION_MODES)}")
            return True
        set_permission_mode(state, chat_id, choice)
        send_message(chat_id, f"Режим переключён на {choice}.")
        return True

    if cmd == "workspace":
        if not arg:
            current = get_workspace(state, chat_id)
            send_message(chat_id, f"Текущий workspace: `{current}`\nИспользование: /workspace <путь>, /workspace default")
            return True
        if arg.lower() == "default":
            set_workspace(state, chat_id, None)
            send_message(chat_id, f"Workspace сброшен на {WORKDIR}.")
            return True
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.isdir(path):
            send_message(chat_id, f"Директория не существует: `{path}`")
            return True
        set_workspace(state, chat_id, path)
        send_message(chat_id, f"Workspace переключён на `{path}`.")
        return True

    if cmd == "status":
        session_id = get_session(state, chat_id)
        model = get_model(state, chat_id) or "default"
        effort = _effort_label(get_model(state, chat_id), get_effort(state, chat_id))
        mode = get_permission_mode(state, chat_id) or "bypass"
        workspace = get_workspace(state, chat_id)
        busy = "да, выполняется запрос (можно /stop)" if chat_id in busy_chats else "нет"
        acc = get_account_status(state, chat_id) or "не начат"
        lines = [
            "ℹ️ **Статус**",
            f"Сессия: `{session_id[:8] if session_id else 'нет активной'}`",
            f"Модель: `{model}` · Мощность: `{effort}`",
            f"Режим: `{mode}`",
            f"Workspace: `{workspace}`",
            f"Занят: {busy}",
            f"Аккаунт Claude: {acc}",
        ]
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "login":
        start_login(chat_id, state)
        send_message(chat_id, "Начинаю переподключение аккаунта Claude...")
        return True

    if cmd == "restart":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, "Перезапуск доступен только владельцу.")
            return True
        if not SERVICE_NAME:
            send_message(chat_id, "SERVICE_NAME не задан в systemd-юните — автоперезапуск недоступен.")
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
                "🔁 Перезапуск запланирован — выполнится, как только текущие запросы завершатся.",
            )
        return True

    if cmd == "update":
        if str(chat_id) != str(OWNER_ID):
            send_message(chat_id, "Обновление доступно только владельцу.")
            return True
        if not SERVICE_NAME:
            send_message(chat_id, "SERVICE_NAME не задан в systemd-юните — автоперезапуск недоступен.")
            return True
        with pending_local_bot_api_setups_lock:
            if pending_local_bot_api_setups.get(chat_id):
                send_message(chat_id, "Настройка локального Bot API уже начата; закончи текущий диалог.")
                return True
        send_message(chat_id, "⬇️ Обновляю из git...")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.sh")
        try:
            result = subprocess.run(
                [script], capture_output=True, text=True, timeout=900, check=False,
            )
        except Exception as e:
            send_message(chat_id, f"❌ Не смог запустить update.sh: {e}")
            return True
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()[-2000:]
            send_message(chat_id, f"❌ Обновление не удалось:\n```\n{output}\n```")
            return True
        summary = (result.stdout or "").strip().splitlines()[-1:] or ["обновлено"]
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
                "Включить приём файлов до 2 ГБ через локальный Bot API сервер? Да/Нет",
            )
            return True
        repair_status = _repair_local_bot_api_service()
        # Same deferred-restart mechanism as /restart: never kill a turn
        # (possibly this very one) mid-answer, only restart once idle.
        request_restart(chat_id)
        note = " Перезапуск — как только текущие запросы завершатся." if busy_chats else " Перезапуск — между ходами."
        suffix = f"\n{repair_status}" if repair_status else ""
        send_message(chat_id, f"✅ {summary[0]}.{suffix}{note}")
        return True

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def register_commands():
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    tg_call("setMyCommands", payload)
    # Some hosts (e.g. this bot token was previously used by the official
    # Channels plugin) have a stale all_private_chats scope registered,
    # which takes precedence over the default scope in private chats and
    # would otherwise mask our command list. Overwrite it explicitly.
    tg_call("setMyCommands", {**payload, "scope": {"type": "all_private_chats"}})


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
        error_text = f"Ошибка моста:\n```\n{err}\n```"
        send_message(output_chat_id or chat_id, error_text)
        if delegated:
            write_last_turn(output_chat_id or chat_id, error_text, delegated=True)
            write_request_result(pop_delegate_request_id(state, chat_id), error_text, ok=False)
        busy_chats.discard(chat_id)


def _refuse_while_draining(output_chat_id, delegated):
    send_message(output_chat_id, DRAINING_TEXT)
    if delegated:
        write_last_turn(output_chat_id, DRAINING_TEXT, delegated=True, ok=False)


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
            "Уже выполняю предыдущий запрос. Дождись ответа или используй /stop.",
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
        _delegate_error(chat_id, "Уже выполняю предыдущую делегированную задачу.", request_id)
        return False

    if requested_session_id and requested_env:
        _delegate_error(chat_id, "Нельзя использовать --env вместе с --resume.", request_id)
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
                "Нельзя продолжить эту делегацию: resume_session_id не совпадает "
                "с последней сессией делегатора.",
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
        write_request_result(refused_id, "Делегированная задача не запущена.", ok=False)
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
        send_message(output_chat_id or chat_id, f"Ошибка моста:\n```\n{err}\n```")


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
        f"Вы не внесены в белый список.\n"
        f"Ваш Telegram ID: `{chat_id}`\n\n"
        f"Добавьте его в конфиг через запятую и нажмите на кнопку снизу:"
    )
    tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": format_message(text),
        "parse_mode": "MarkdownV2",
        "reply_markup": {
            "inline_keyboard": [[{"text": "Готово ✅", "callback_data": "check_whitelist"}]]
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


def start_login(chat_id, state):
    """Start Claude's interactive OAuth flow and relay it through Telegram.

    Non-owner chats use their isolated account directory.  The owner uses
    the normal ``~/.claude`` directory, but the login process and its FIFO
    live in this bridge's private runtime directory so ``/login`` works
    remotely without requiring an SSH shell on the host.
    """
    config_dir = account_dir(chat_id)
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
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        # The service environment may have been customized.  The owner
        # login must always target the ordinary ~/.claude account.
        env.pop("CLAUDE_CONFIG_DIR", None)
    shell_cmd = f'exec script -qefc "{CLAUDE_BIN} auth login --claudeai" /dev/null 0<>{fifo_path}'
    proc = subprocess.Popen(
        ["bash", "-c", shell_cmd],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    info = {"proc": proc, "fifo": fifo_path, "config_dir": config_dir}
    pending_logins[chat_id] = info
    set_account_status(state, chat_id, "awaiting_code")

    def reader():
        deadline = time.time() + LOGIN_TIMEOUT_S
        url_seen = False
        try:
            for line in proc.stdout:
                m = re.search(r"https://\S+", line.strip())
                if m:
                    url_seen = True
                    send_message(
                        chat_id,
                        "1. Нажми на кнопку ниже\n"
                        "2. Войди в свой аккаунт Claude\n"
                        "3. Пришли мне сюда код, который дадут после входа\n\n"
                        "Примечание: для входа нужна подписка Pro или выше.",
                    )
                    # A URL button instead of a raw pasted link -- keeps the
                    # giant OAuth URL out of the chat text entirely.
                    tg_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": "🔗 Войти в Claude",
                        "reply_markup": {
                            "inline_keyboard": [[{"text": "🔗 Войти в Claude", "url": m.group(0)}]]
                        },
                    })
                    break
                if time.time() > deadline:
                    break
        except Exception:
            pass
        if not url_seen and pending_logins.get(chat_id) is info:
            set_account_status(state, chat_id, "login_failed")
            _cleanup_login(chat_id, info)
            send_message(chat_id, "❌ Не удалось запустить вход Claude. Попробуй /login ещё раз.")

    threading.Thread(target=reader, daemon=True).start()


def feed_login_code(chat_id, code, state):
    info = pending_logins.get(chat_id)
    if not info:
        return False
    try:
        with open(info["fifo"], "w") as f:
            f.write(code.strip() + "\n")
    except Exception:
        if pending_logins.get(chat_id) is info:
            set_account_status(state, chat_id, "login_failed")
            _cleanup_login(chat_id, info)
        send_message(chat_id, "Не смог передать код процессу логина. Попробуй /login заново.")
        return False

    def check():
        time.sleep(3)
        for _ in range(10):
            try:
                r = subprocess.run(
                    [CLAUDE_BIN, "auth", "status"],
                    env=claude_env(info["config_dir"]),
                    capture_output=True, text=True, timeout=15,
                )
                d = json.loads(r.stdout)
                if d.get("loggedIn"):
                    if str(chat_id) == str(OWNER_ID):
                        set_account_status(state, chat_id, "ready")
                        success_message = "✅ Аккаунт подключён. Можно пользоваться ботом."
                    else:
                        set_account_status(state, chat_id, "awaiting_display_name")
                        success_message = "✅ Аккаунт подключён.\n\nКак к тебе обращаться?"
                    _cleanup_login(chat_id, info)
                    # A persistent Claude process may have cached the expired
                    # OAuth session.  Recreate it on the next prompt so the
                    # fresh credentials are definitely used.
                    _stop_chat_process(chat_id)
                    send_message(chat_id, success_message)
                    return
            except Exception:
                pass
            time.sleep(2)
        if pending_logins.get(chat_id) is info:
            set_account_status(state, chat_id, "login_failed")
            _cleanup_login(chat_id, info)
        send_message(chat_id, "Не удалось подтвердить вход. Проверь код и попробуй /login ещё раз.")

    threading.Thread(target=check, daemon=True).start()
    return True


def handle_onboarding(chat_id, user_id, text, state, whitelist):
    """Returns True if this update was fully handled here (whitelist prompt /
    login kickoff / code consumption) and the main loop should move on.
    Returns False if the account is ready and normal dispatch should proceed."""
    if str(user_id) not in whitelist:
        send_whitelist_prompt(chat_id)
        return True

    # This is called before normal command/prompt routing by bridge.py, so
    # api_id/api_hash replies cannot be persisted as a Claude conversation.
    if _handle_local_bot_api_setup_reply(chat_id, text or ""):
        return True

    status = get_account_status(state, chat_id)
    if status == "ready":
        return False

    if status == "awaiting_code":
        if text and text.strip().lower().lstrip("/.").split()[0:1] == ["login"]:
            start_login(chat_id, state)
            send_message(chat_id, "Перезапускаю вход Claude — сейчас пришлю новую ссылку.")
            return True
        if text and not text.startswith(("/", ".")):
            feed_login_code(chat_id, text.strip(), state)
        else:
            send_message(chat_id, "Жду код авторизации (пришли его текстом, без команд).")
        return True

    elif status == "awaiting_display_name":
        tenant_dir = account_dir(chat_id)
        if tenant_dir:
            claude_md = os.path.join(tenant_dir, "CLAUDE.md")
            if os.path.exists(claude_md):
                with open(claude_md, encoding="utf-8") as f:
                    personality = f.read()
                with open(claude_md, "w", encoding="utf-8") as f:
                    f.write(personality.replace("<user>", (text or "").strip()))
        set_account_status(state, chat_id, "ready")
        send_message(chat_id, "✅ Готово. Можно пользоваться ботом.")
        return True

    start_login(chat_id, state)
    send_message(chat_id, "Ты в списке — начинаю подключение твоего аккаунта Claude...")
    return True


def handle_callback_query(cq, state):
    data = cq.get("data")
    from_id = cq.get("from", {}).get("id")
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    if not chat_id or data != "check_whitelist":
        answer_callback_query(cq["id"])
        return

    whitelist = load_whitelist()
    if str(from_id) not in whitelist:
        answer_callback_query(cq["id"], "Ещё не добавлен в список.", show_alert=True)
        return

    answer_callback_query(cq["id"], "Принято!")
    status = get_account_status(state, chat_id)
    if status == "ready":
        send_message(chat_id, "Аккаунт уже подключён.")
    elif status != "awaiting_code":
        start_login(chat_id, state)
        send_message(chat_id, "Ты в списке — начинаю подключение твоего аккаунта Claude...")
