"""Hermetic brokered MCP permission, transport and exchange checks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix='brokered_mcp_'))
os.environ.update(TELEGRAM_BOT_TOKEN='fake', OWNER_ID='1000000001',
                  BRIDGE_STATE_FILE=str(TMP / 'state.json'), BRIDGE_ISOLATION='off',
                  BRIDGE_TENANT_CONTROL_DIR=str(TMP / 'control'),
                  BRIDGE_ISOLATED_MCP_DIR=str(TMP / 'installed'))
sys.path.insert(0, str(ROOT))
import brokered_mcp_exchange_mcp as proxy
import tenant_mcp_broker as broker
import tenant_exchange as files
import runtime

TENANT = '1000000002'
EXCHANGE = TMP / '.exchange'
for part in ('queue', 'result'):
    (EXCHANGE / part).mkdir(parents=True)
STDIO = TMP / 'fake_stdio.py'
STDIO.write_text('''import json, os, sys, time
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    if method == 'notifications/initialized': continue
    if method == 'tools/list':
        result = {'tools': [{'name':'allowed','inputSchema':{'type':'object'}}, {'name':'denied','inputSchema':{'type':'object'}}]}
    elif method == 'tools/call':
        name = req['params']['name']
        args = req['params'].get('arguments', {})
        if name == 'slow': time.sleep(1)
        if name == 'crash': sys.exit(1)
        if name == 'envcheck':
            print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'content':[{'type':'text','text':'token=' + os.getenv('TELEGRAM_BOT_TOKEN','absent')}]}}), flush=True)
            continue
        result = {'content':[{'type':'text','text': ('x'*300000 if name == 'big' else os.getenv('BROKER_SECRET', '') + ':' + str(args.get('value','ok')))}]}
    else: result = {'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}
    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}), flush=True)
''')


def put(body, name=None):
    name = name or f'mcpcall-{uuid.uuid4()}.json'
    path = EXCHANGE / 'queue' / name
    path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
    return name, path


def answer(name, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        path = EXCHANGE / 'result' / name
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(.01)
    raise AssertionError(f'No reply for {name}')


def call(server='fake', method='tools/call', tool='allowed', arguments=None, extra=None):
    body = {'server': server, 'method': method,
            'params': {'name': tool, 'arguments': arguments or {}} if method == 'tools/call' else {}}
    body.update(extra or {})
    name, path = put(body)
    broker.process_tenant_mcp_queue(TENANT, str(EXCHANGE))
    result = answer(name)
    assert not path.exists()
    return result


class HTTP(BaseHTTPRequestHandler):
    session_seen = []
    def log_message(self, *args):
        pass
    def do_POST(self):
        HTTP.session_seen.append(self.headers.get('Mcp-Session-Id'))
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(200)
        self.send_header('Mcp-Session-Id', 'session-1')
        self.send_header('Content-Type', 'text/event-stream' if body['method'] == 'tools/call' else 'application/json')
        self.end_headers()
        if 'id' not in body:
            return
        result = ({'tools': [{'name': 'allowed'}]} if body['method'] == 'tools/list' else
                  {'content': [{'type': 'text', 'text': 'http-ok'}]} if body['method'] == 'tools/call' else
                  {'protocolVersion': '2025-06-18', 'capabilities': {}, 'serverInfo': {'name': 'http', 'version': '1'}})
        packet = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
        self.wfile.write(b'data: ' + packet + b'\n\n' if body['method'] == 'tools/call' else packet)


def main():
    http = ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    defs = {'fake': {'type': 'stdio', 'command': sys.executable, 'args': [str(STDIO)],
                     'env': {'BROKER_SECRET': 'bridge-only'}},
            'http': {'type': 'http', 'url': f'http://127.0.0.1:{http.server_port}/mcp', 'headers': {'X-Fake': 'bridge-only'}}}
    grants = {'fake': {'tools': ['allowed', 'big', 'slow', 'crash', 'envcheck']}, 'http': {'tools': ['allowed']}}
    with patch.object(broker, '__file__', str(TMP / 'tenant_mcp_broker.py')):
        assert broker.server_definitions() == {}
        assert broker.tenant_grants(TENANT) == {}
        definitions_file = TMP / 'mcp_servers.json'
        definitions_file.write_text(json.dumps(defs))
        definitions_file.chmod(0o600)
        control = TMP / 'control' / TENANT
        control.mkdir(parents=True)
        grant_file = control / 'mcp_grants.json'
        grant_file.write_text(json.dumps({'servers': grants}))
        grant_file.chmod(0o600)
        assert broker.server_definitions() == defs
        assert broker.tenant_grants(TENANT) == grants
        definitions_file.chmod(0o644)
        assert broker.server_definitions() == {}
        definitions_file.chmod(0o600)
        grant_file.rename(control / 'real_grants.json')
        grant_file.symlink_to(control / 'real_grants.json')
        assert broker.tenant_grants(TENANT) == {}
        grant_file.unlink()
        (control / 'real_grants.json').rename(grant_file)
    with patch.object(broker, 'server_definitions', return_value=defs), \
         patch.object(broker, 'tenant_grants', return_value=grants):
        result = call(method='tools/list')
        assert [tool['name'] for tool in result['result']['tools']] == ['allowed']
        assert call(tool='allowed', arguments={'value': 'sql select 1'})['result']['content'][0]['text'] == 'bridge-only:sql select 1'
        assert call(tool='denied')['ok'] is False
        # the bridge's own variables (the test sets TELEGRAM_BOT_TOKEN) must not reach a stdio server
        assert call(tool='envcheck')['result']['content'][0]['text'] == 'token=absent'
        assert call(extra={'chat_id': 'other'})['ok'] is False
        assert call(server='http')['result']['content'][0]['text'] == 'http-ok'
        assert 'session-1' in HTTP.session_seen
        big = call(tool='big')
        assert big['ok'] is True and 'сокращён' in json.dumps(big, ensure_ascii=False)
        assert len(json.dumps(big).encode()) <= broker.MAX_RESULT
        with patch.object(broker, 'tenant_grants', return_value={}):
            assert 'Нет прав' in call()['text']
        with patch.object(broker, 'CALL_TIMEOUT', .15):
            assert '110 секунд' in call(tool='slow')['text']
        assert call(tool='allowed')['ok'] is True  # process restarted after timeout
        assert call(tool='crash')['ok'] is False
        assert call(tool='allowed')['ok'] is True  # process restarted after crash
        foreign, path = put({'server': 'fake', 'method': 'tools/list', 'params': {}}, 'not-mcpcall.json')
        broker.process_tenant_mcp_queue(TENANT, str(EXCHANGE))
        files.process_tenant_queue(int(TENANT), str(EXCHANGE), lambda *x: {'ok': True}, 100)
        assert path.exists() and not (EXCHANGE / 'result' / foreign).exists()
        path.unlink()
        # Four calls can wait; the fifth is refused without reaching the server.
        names = [put({'server': 'fake', 'method': 'tools/call', 'params': {'name': 'slow', 'arguments': {}}})[0] for _ in range(5)]
        broker.process_tenant_mcp_queue(TENANT, str(EXCHANGE))
        outcomes = [answer(name, 7) for name in names]
        assert sum(item['ok'] for item in outcomes) == 4
        assert sum('4' in item.get('text', '') for item in outcomes) == 1
        # Proxy receives only the exchange location and logical name.
        with patch.dict(os.environ, {'TENANT_EXCHANGE_DIR': str(EXCHANGE), 'BROKERED_SERVER': 'fake'}, clear=True):
            assert 'BROKER_SECRET' not in os.environ
            output = {}
            def serve():
                until = time.monotonic() + 2
                while time.monotonic() < until and not output:
                    broker.process_tenant_mcp_queue(TENANT, str(EXCHANGE))
                    time.sleep(.01)
            worker = threading.Thread(target=serve)
            worker.start()
            result = proxy._exchange('tools/call', {'name': 'allowed', 'arguments': {}})
            output['done'] = True
            worker.join()
            assert result['ok'] and result['result']['content'][0]['text'].startswith('bridge-only:')
            with patch.object(proxy, '_exchange', return_value={'done': True, 'ok': True, 'result': {'tools': [{'name': 'allowed'}]}}) as routed:
                proxy._tools = None
                assert proxy._handle({'id': 1, 'method': 'tools/list', 'params': {}})['result']['tools'][0]['name'] == 'allowed'
                assert proxy._handle({'id': 2, 'method': 'tools/list', 'params': {}})['result']['tools'][0]['name'] == 'allowed'
                assert routed.call_count == 1
        installed = TMP / 'installed'
        installed.mkdir()
        (installed / 'brokered_mcp_exchange_mcp.py').write_text('# installed')
        provision = lambda *args, **kwargs: {'exchange': str(EXCHANGE)}
        with patch.object(runtime, 'ensure_tenant', side_effect=provision), \
             patch.object(runtime, 'codex_bridge_dir', return_value=None), \
             patch.object(runtime, 'BRIDGE_ISOLATION', 'on'), \
             patch.object(runtime.people, 'is_allowed', side_effect=lambda value: str(value) == TENANT):
            servers = runtime._isolated_mcp_servers(TENANT)
            assert set(servers) == {'fake', 'http'}
            assert servers['fake']['env'] == {'BROKERED_SERVER': 'fake', 'TENANT_EXCHANGE_DIR': str(EXCHANGE)}
            assert 'BROKER_SECRET' not in json.dumps(servers)
    http.shutdown()
    for client in broker._clients.values():
        client._stop()


if __name__ == '__main__':
    main()
    print('CLOSED: brokered MCP permissions, transports, bounds and isolation.')
