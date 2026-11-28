"""Bridge-owned MCP clients for isolated tenants; tenant identity is directory-bound."""
import json
import os
import re
import select
import stat
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from tenant_exchange import ExchangeRefusal, FILE_FLAGS, REQUEST_MAX_BYTES, _unique_object, open_exchange_dir

NAME_RE = re.compile(r'mcpcall-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.json\Z')
SERVER_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
MAX_RESULT = 256 * 1024
MAX_UPSTREAM = 8 * 1024 * 1024
CALL_TIMEOUT = 110
_clients = {}
_clients_lock = threading.Lock()
_active = set()
_active_lock = threading.Lock()


def _read_json_file(path, limit=64 * 1024):
    fd = os.open(path, FILE_FLAGS)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ExchangeRefusal('Недоступная конфигурация MCP.')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ExchangeRefusal('Конфигурация MCP слишком большая.')
        value = json.loads(raw.decode(), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ExchangeRefusal('Некорректная конфигурация MCP.')
        return value
    finally:
        os.close(fd)


def server_definitions():
    path = Path(__file__).resolve().parent / 'mcp_servers.json'
    try:
        return _read_json_file(path)
    except (OSError, ValueError, UnicodeError):
        return {}


def direct_definition(definition):
    """A server an administrator tenant runs itself, without the broker: a validated copy, or None."""
    if not isinstance(definition, dict):
        return None

    def strings(value):
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())

    kind = definition.get("type")
    if kind == "stdio":
        command, args, env = definition.get("command"), definition.get("args", []), definition.get("env", {})
        if (isinstance(command, str) and command.startswith("/") and isinstance(args, list)
                and all(isinstance(a, str) for a in args) and strings(env)):
            return {"type": "stdio", "command": command, "args": list(args), "env": dict(env)}
    elif kind == "http":
        url, headers = definition.get("url"), definition.get("headers", {})
        if isinstance(url, str) and url.startswith(("http://", "https://")) and strings(headers):
            return {"type": "http", "url": url, **({"headers": dict(headers)} if headers else {})}
    return None


def tenant_grants(tenant_id):
    if not str(tenant_id).isdecimal():
        return {}
    root = os.environ.get('BRIDGE_TENANT_CONTROL_DIR') or str(Path(__file__).resolve().parent / 'tenant_control')
    try:
        data = _read_json_file(os.path.join(root, str(tenant_id), 'mcp_grants.json'))
        return data.get('servers', {}) if isinstance(data.get('servers'), dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _request(queue_fd, name):
    if not NAME_RE.fullmatch(name):
        raise ExchangeRefusal('Недопустимое имя запроса MCP.')
    fd = os.open(name, FILE_FLAGS, dir_fd=queue_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > REQUEST_MAX_BYTES:
            raise ExchangeRefusal('Запрос MCP должен быть обычным файлом до 64 КБ.')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            raw = handle.read(REQUEST_MAX_BYTES + 1)
        if len(raw) > REQUEST_MAX_BYTES:
            raise ExchangeRefusal('Запрос MCP больше 64 КБ.')
        data = json.loads(raw.decode(), object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != {'server', 'method', 'params'}:
            raise ExchangeRefusal('Недопустимые поля запроса MCP.')
        if not isinstance(data['server'], str) or not SERVER_RE.fullmatch(data['server']):
            raise ExchangeRefusal('Недопустимое имя MCP-сервера.')
        if data['method'] not in ('tools/list', 'tools/call') or not isinstance(data['params'], dict):
            raise ExchangeRefusal('Недопустимый метод или параметры MCP.')
        if data['method'] == 'tools/call' and (not isinstance(data['params'].get('name'), str)
                                                or not isinstance(data['params'].get('arguments', {}), dict)):
            raise ExchangeRefusal('Недопустимое имя инструмента или аргументы.')
        return data
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ExchangeRefusal('Некорректный JSON запроса MCP.') from exc
    finally:
        os.close(fd)


class MCPClient:
    def __init__(self, definition):
        self.definition = definition
        self.lock = threading.Lock()
        self.process = None
        self.buffer = b''
        self.session = None
        self.serial = 0
        self.initialized = False

    def _stop(self):
        if self.process is not None:
            self.process.kill() if self.process.poll() is None else None
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self.process = None
        self.buffer = b''
        self.initialized = False
        self.session = None

    def _line(self, deadline):
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Вызов MCP превысил 110 секунд.')
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError('Вызов MCP превысил 110 секунд.')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('Процесс MCP завершился.')
            self.buffer += chunk
            if len(self.buffer) > MAX_UPSTREAM:
                raise ValueError('Ответ MCP слишком большой.')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return line

    def _rpc_stdio(self, method, params, deadline, notification=False):
        self.serial += 1
        identifier = self.serial
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = identifier
        if params is not None:
            message['params'] = params
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        self.process.stdin.flush()
        if notification:
            return {}
        while True:
            response = json.loads(self._line(deadline))
            if response.get('id') != identifier:
                continue
            if 'error' in response:
                raise ValueError(str(response['error'].get('message', 'Ошибка MCP-сервера.')))
            if not isinstance(response.get('result'), dict):
                raise ValueError('Некорректный ответ MCP-сервера.')
            return response['result']

    def _rpc_http(self, method, params, deadline, notification=False):
        self.serial += 1
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = self.serial
        if params is not None:
            message['params'] = params
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        headers.update(self.definition.get('headers', {}))
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.definition['url'], json.dumps(message).encode(), headers, method='POST')
        with urllib.request.urlopen(request, timeout=max(0.01, deadline - time.monotonic())) as response:
            self.session = response.headers.get('Mcp-Session-Id', self.session)
            raw = response.read(MAX_UPSTREAM + 1)
            if len(raw) > MAX_UPSTREAM:
                raise ValueError('Ответ MCP слишком большой.')
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].lower()
        if notification:
            return {}
        if content_type == 'application/json':
            packet = json.loads(raw)
        elif content_type == 'text/event-stream':
            packet = None
            for event in raw.decode().replace('\r\n', '\n').split('\n\n'):
                data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                if data:
                    candidate = json.loads(data)
                    if candidate.get('id') == self.serial:
                        packet = candidate
                        break
            if packet is None:
                raise ValueError('Ответ MCP не содержит ожидаемое событие.')
        else:
            raise ValueError('Неподдерживаемый тип ответа MCP.')
        if packet.get('id') != self.serial:
            raise ValueError('Неверный ID ответа MCP.')
        if 'error' in packet:
            raise ValueError(str(packet['error'].get('message', 'Ошибка MCP-сервера.')))
        if not isinstance(packet.get('result'), dict):
            raise ValueError('Некорректный ответ MCP-сервера.')
        return packet['result']

    def call(self, method, params):
        deadline = time.monotonic() + CALL_TIMEOUT
        if not self.lock.acquire(timeout=CALL_TIMEOUT):
            raise TimeoutError('Вызов MCP превысил 110 секунд.')
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('Вызов MCP превысил 110 секунд.')
            try:
                if self.definition['type'] == 'stdio':
                    if self.process is None or self.process.poll() is not None:
                        self._stop()
                        # Minimal environment: the bridge's own variables (bot tokens, API keys) are not the server's business.
                        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ') if key in os.environ}
                        env.update(self.definition.get('env', {}))
                        self.process = subprocess.Popen([self.definition['command'], *self.definition.get('args', [])],
                                                        cwd=self.definition.get('cwd') or None, env=env,
                                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    rpc = self._rpc_stdio
                elif self.definition['type'] == 'http':
                    rpc = self._rpc_http
                else:
                    raise ValueError('Неизвестный тип MCP-сервера.')
                if not self.initialized:
                    rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                       'clientInfo': {'name': 'tenant-mcp-broker', 'version': '1.0.0'}}, deadline)
                    rpc('notifications/initialized', None, deadline, notification=True)
                    self.initialized = True
                return rpc(method, params, deadline)
            except Exception:
                self._stop()
                raise
        finally:
            self.lock.release()


def _client(server, definition):
    with _clients_lock:
        current = _clients.get(server)
        if current is None or current.definition != definition:
            if current is not None:
                with current.lock:
                    current._stop()
            current = MCPClient(definition)
            _clients[server] = current
        return current


def _bounded(result):
    raw = json.dumps(result, ensure_ascii=False).encode()
    if len(raw) <= MAX_RESULT - 1024:
        return result
    note = '\n[Ответ MCP сокращён до 256 КБ.]'
    if isinstance(result.get('tools'), list):
        trimmed = {'tools': [], '_truncated': note.strip()}
        for tool in result['tools']:
            candidate = {'tools': [*trimmed['tools'], tool], '_truncated': note.strip()}
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_RESULT - 1024:
                break
            trimmed['tools'].append(tool)
        return trimmed
    if isinstance(result.get('content'), list):
        trimmed = {'content': [], 'isError': result.get('isError', False)}
        budget = MAX_RESULT - 4096
        for item in result['content']:
            if not isinstance(item, dict):
                continue
            value = dict(item)
            if isinstance(value.get('text'), str):
                value['text'] = value['text'][:budget // 4] + note
            if len(json.dumps(value, ensure_ascii=False).encode()) > budget:
                break
            trimmed['content'].append(value)
            budget -= len(json.dumps(value, ensure_ascii=False).encode())
        trimmed['content'].append({'type': 'text', 'text': note})
        return trimmed
    return {'content': [{'type': 'text', 'text': raw[:MAX_RESULT // 4].decode('utf-8', 'ignore') + note}], 'isError': True}


def _write_result(result_fd, name, response):
    temporary = f'.mcp-result-{uuid.uuid4()}.tmp'
    raw = (json.dumps(response, ensure_ascii=False) + '\n').encode()
    if len(raw) > MAX_RESULT:
        response = {'done': True, 'ok': False, 'text': 'Ответ MCP слишком большой.'}
        raw = (json.dumps(response, ensure_ascii=False) + '\n').encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=result_fd)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=result_fd, dst_dir_fd=result_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=result_fd)
        except FileNotFoundError:
            pass


def _execute(tenant_id, exchange, name, request):
    started = time.monotonic()
    server = request['server']
    tool = request['params'].get('name', '') if request['method'] == 'tools/call' else 'tools/list'
    arguments = request['params'].get('arguments', {})
    try:
        grants = tenant_grants(tenant_id)
        grant = grants.get(server)
        allowed = grant.get('tools') if isinstance(grant, dict) else None
        if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
            raise ExchangeRefusal('Нет прав на MCP-сервер.')
        definitions = server_definitions()
        if server not in definitions:
            raise ExchangeRefusal('MCP-сервер недоступен.')
        if request['method'] == 'tools/call' and '*' not in allowed and tool not in allowed:
            raise ExchangeRefusal('Нет прав на инструмент MCP.')
        result = _client(server, definitions[server]).call(request['method'], request['params'])
        if request['method'] == 'tools/list':
            result = dict(result)
            result['tools'] = [item for item in result.get('tools', [])
                               if isinstance(item, dict) and ('*' in allowed or item.get('name') in allowed)]
        response = {'done': True, 'ok': True, 'result': _bounded(result)}
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ExchangeRefusal, TimeoutError, ValueError)) else 'Не удалось вызвать MCP-сервер.'
        response = {'done': True, 'ok': False, 'text': message}
    size = len(json.dumps(response, ensure_ascii=False).encode())
    audit = json.dumps(arguments, ensure_ascii=False, default=str)[:200]
    print(f'MCP tenant={tenant_id} server={server} tool={tool} bytes={size} duration={time.monotonic()-started:.3f}s args={audit}', flush=True)
    try:
        result_fd = open_exchange_dir(exchange, 'result')
        try:
            _write_result(result_fd, name, response)
        finally:
            os.close(result_fd)
        queue_fd = open_exchange_dir(exchange, 'queue')
        try:
            os.unlink(name, dir_fd=queue_fd)
        finally:
            os.close(queue_fd)
    except OSError as exc:
        print(f'MCP result unavailable for tenant {tenant_id}: {type(exc).__name__}', flush=True)
    finally:
        with _active_lock:
            _active.discard((tenant_id, name))


def process_tenant_mcp_queue(tenant_id, exchange):
    """Scan only mcpcall UUID names; tenant_id must come from the watcher."""
    queue_fd = open_exchange_dir(exchange, 'queue')
    try:
        result_fd = open_exchange_dir(exchange, 'result')
        try:
            with os.scandir(queue_fd) as entries:
                names = [entry.name for entry in entries if NAME_RE.fullmatch(entry.name)][:16]
            for name in names:
                with _active_lock:
                    if (tenant_id, name) in _active:
                        continue
                try:
                    request = _request(queue_fd, name)
                    with _active_lock:
                        count = sum(key[0] == tenant_id for key in _active)
                        if count >= 4:
                            raise ExchangeRefusal('Слишком много ожидающих вызовов MCP (не больше 4).')
                        _active.add((tenant_id, name))
                    try:
                        threading.Thread(target=_execute, args=(tenant_id, exchange, name, request), daemon=True).start()
                    except Exception:
                        with _active_lock:
                            _active.discard((tenant_id, name))
                        raise
                    continue
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ExchangeRefusal) else 'Не удалось прочитать запрос MCP.'
                    _write_result(result_fd, name, {'done': True, 'ok': False, 'text': message})
                    try:
                        os.unlink(name, dir_fd=queue_fd)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(result_fd)
    finally:
        os.close(queue_fd)
