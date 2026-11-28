#!/usr/bin/env python3
"""Tenant-scoped MCP tool for returning an outbox file to Telegram."""

import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BRIDGE_DIR / "file_send_queue"
RESULT_DIR = BRIDGE_DIR / "file_send_result"
POLL_TIMEOUT_S = 45
POLL_INTERVAL_S = 0.2


def _directories():
    exchange = os.environ.get("TENANT_EXCHANGE_DIR")
    if exchange:
        return Path(exchange) / "queue", Path(exchange) / "result"
    return QUEUE_DIR, RESULT_DIR

SERVER_INFO = {"name": "send-telegram-file", "version": "1.0.0"}
TOOL = {
    "name": "send_telegram_file",
    "description": (
        "Отправить готовый файл в текущий Telegram-чат. Telegram chat_id жёстко "
        "привязан к этому MCP-процессу и не задаётся аргументом. Разрешены только "
        "обычные файлы из каталога CLAUDE_TELEGRAM_OUTBOX; скопируй или создай файл "
        "там, затем передай абсолютный путь."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Абсолютный путь внутри CLAUDE_TELEGRAM_OUTBOX."},
            "caption": {"type": "string", "description": "Необязательная подпись к документу."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _write_request(request_id, chat_id, path, caption):
    queue_dir, result_dir = _directories()
    if not os.environ.get("TENANT_EXCHANGE_DIR"):
        queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    request_path = queue_dir / f"{request_id}.json"
    temporary = queue_dir / f".{request_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            request = {"path": path, "caption": caption}
            if not os.environ.get("TENANT_EXCHANGE_DIR"):
                request["chat_id"] = chat_id
            json.dump(request, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, request_path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _share_with_bridge(path):
    """In an isolated tenant the bridge reads the file as another user (the group of the exchange directory).

    Make the tenant's own regular file group-readable instead of failing later with a vague error. A file that
    was copied with `cp -p`/`copy2` from a private location keeps its 0600 mode otherwise. Never follows symlinks
    and touches nothing that is not a regular file owned by this user.
    """
    if not os.environ.get("TENANT_EXCHANGE_DIR"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return  # the bridge reports the real problem
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and (info.st_mode & 0o060) != 0o060:
            os.fchmod(fd, (info.st_mode & 0o7777) | 0o060)
    except OSError:
        pass
    finally:
        os.close(fd)


def _send_file(path, caption=""):
    try:
        chat_id = int(os.environ.get("CHAT_ID", ""))
    except (TypeError, ValueError):
        return False, "Отклонено локально: MCP-процесс не получил корректный CHAT_ID."
    if not isinstance(path, str) or not path.strip():
        return False, "Отклонено локально: путь к файлу пуст."
    if not Path(path).is_absolute():
        return False, "Отклонено локально: передай абсолютный путь к файлу."
    if not isinstance(caption, str):
        return False, "Отклонено локально: подпись должна быть строкой."

    _share_with_bridge(path)
    request_id = str(uuid.uuid4())
    result_path = _directories()[1] / f"{request_id}.json"
    try:
        _write_request(request_id, chat_id, path, caption)
    except OSError as exc:
        return False, f"Не удалось записать запрос на отправку файла: {exc}"
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            raw_result = result_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            time.sleep(POLL_INTERVAL_S)
            continue
        except OSError as exc:
            return False, f"Не удалось прочитать результат отправки файла: {exc}"
        try:
            result = json.loads(raw_result)
        except (TypeError, ValueError) as exc:
            return False, f"Получен повреждённый результат отправки файла: {exc}"
        finally:
            try:
                result_path.unlink()
            except FileNotFoundError:
                pass
        if not isinstance(result, dict) or result.get("done") is not True:
            return False, "Мост вернул результат отправки файла неизвестного формата."
        text = result.get("text") or "Мост не указал результат отправки файла."
        return bool(result.get("ok")), str(text)
    return False, "Таймаут отправки файла: запрос мог быть обработан позднее."


def _jsonrpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle_message(request):
    if not isinstance(request, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        protocol_version = (request.get("params") or {}).get("protocolVersion", "2025-06-18")
        return _jsonrpc_result(request_id, {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": [TOOL]})
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return _jsonrpc_error(request_id, -32602, "Unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict) or set(arguments) - {"path", "caption"} or "path" not in arguments:
            return _jsonrpc_error(request_id, -32602, "send_telegram_file accepts path and optional caption")
        ok, text = _send_file(arguments["path"], arguments.get("caption", ""))
        return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": text}], "isError": not ok})
    if request_id is None:
        return None
    return _jsonrpc_error(request_id, -32601, "Method not found")


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except (TypeError, ValueError):
            response = _jsonrpc_error(None, -32700, "Parse error")
        else:
            try:
                response = _handle_message(request)
            except Exception as exc:
                response = _jsonrpc_error(request.get("id"), -32603, str(exc))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
