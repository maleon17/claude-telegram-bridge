#!/usr/bin/python3
"""Tenant-side, dependency-free MCP proxy over the isolated file exchange."""
import json
import os
import stat
import sys
import time
import uuid

DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
MAX_REQUEST = 64 * 1024
MAX_RESULT = 256 * 1024 + 4096
WAIT_SECONDS = 120
_tools = None


def _dir(exchange, child):
    current = os.open('/', DIR_FLAGS)
    try:
        for component in exchange.split('/'):
            if component:
                following = os.open(component, DIR_FLAGS, dir_fd=current)
                os.close(current)
                current = following
        following = os.open(child, DIR_FLAGS, dir_fd=current)
        return following
    finally:
        os.close(current)


def _exchange(method, params):
    exchange = os.environ.get('TENANT_EXCHANGE_DIR')
    server = os.environ.get('BROKERED_SERVER')
    if not exchange or not os.path.isabs(exchange) or not server:
        raise ValueError('Не настроен брокерный MCP-сервер.')
    name = f'mcpcall-{uuid.uuid4()}.json'
    raw = (json.dumps({'server': server, 'method': method, 'params': params}, ensure_ascii=False) + '\n').encode()
    if len(raw) > MAX_REQUEST:
        raise ValueError('Запрос MCP больше 64 КБ.')
    queue = _dir(exchange, 'queue')
    temporary = f'.mcpcall-{uuid.uuid4()}.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=queue)
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o660)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=queue, dst_dir_fd=queue)
    finally:
        try:
            os.unlink(temporary, dir_fd=queue)
        except FileNotFoundError:
            pass
        os.close(queue)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        directory = _dir(exchange, 'result')
        try:
            try:
                fd = os.open(name, FILE_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                time.sleep(0.1)
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT:
                    raise ValueError('Недопустимый файл ответа MCP.')
                with os.fdopen(fd, 'rb', closefd=False) as handle:
                    data = handle.read(MAX_RESULT + 1)
                if len(data) > MAX_RESULT:
                    raise ValueError('Ответ MCP слишком большой.')
                result = json.loads(data)
                if not isinstance(result, dict) or result.get('done') is not True or not isinstance(result.get('ok'), bool):
                    raise ValueError('Неверный формат ответа MCP.')
                if result['ok'] and not isinstance(result.get('result'), dict):
                    raise ValueError('Неверный формат результата MCP.')
                if not result['ok'] and not isinstance(result.get('text'), str):
                    raise ValueError('Неверный формат отказа MCP.')
                os.unlink(name, dir_fd=directory)
                return result
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    directory = _dir(exchange, 'queue')
    try:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
    finally:
        os.close(directory)
    raise TimeoutError('Бридж не ответил на вызов MCP за 120 секунд.')


def _reply(identifier, result=None, error=None):
    return {'jsonrpc': '2.0', 'id': identifier, 'error' if error else 'result': error or result}


def _handle(request):
    global _tools
    if not isinstance(request, dict):
        return _reply(None, error={'code': -32600, 'message': 'Invalid Request'})
    identifier = request.get('id')
    method = request.get('method')
    if method == 'initialize':
        return _reply(identifier, {'protocolVersion': (request.get('params') or {}).get('protocolVersion', '2025-06-18'),
                                   'capabilities': {'tools': {'listChanged': False}},
                                   'serverInfo': {'name': os.environ.get('BROKERED_SERVER', 'brokered-mcp'), 'version': '1.0.0'}})
    if method == 'notifications/initialized':
        return None
    if method == 'ping':
        return _reply(identifier, {})
    if method not in ('tools/list', 'tools/call'):
        return None if identifier is None else _reply(identifier, error={'code': -32601, 'message': 'Method not found'})
    params = request.get('params') or {}
    if not isinstance(params, dict) or (method == 'tools/call' and not isinstance(params.get('name'), str)):
        return _reply(identifier, error={'code': -32602, 'message': 'Invalid params'})
    try:
        if method == 'tools/list' and not params and _tools is not None:
            result = _tools
        else:
            response = _exchange(method, params)
            if not response['ok']:
                raise ValueError(response['text'])
            result = response['result']
            if method == 'tools/list':
                if not isinstance(result.get('tools'), list):
                    raise ValueError('Некорректный список инструментов MCP.')
                if not params:
                    _tools = result
        return _reply(identifier, result)
    except (OSError, ValueError, UnicodeError, TimeoutError) as exc:
        return _reply(identifier, error={'code': -32000, 'message': str(exc)})


def main():
    for line in sys.stdin:
        try:
            response = _handle(json.loads(line))
        except (ValueError, UnicodeError, TypeError):
            response = _reply(None, error={'code': -32700, 'message': 'Parse error'})
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
