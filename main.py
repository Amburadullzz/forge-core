#!/usr/bin/env python3
# One process, standard library only: no database, git binary, or external service is involved.
"""A GitHub-compatible git forge: one process, its own store, four surfaces.

    git smart HTTP   /{owner}/{repo}.git/{info/refs,git-upload-pack,git-receive-pack}
    REST v3          /api/v3/...
    GraphQL v4       /api/graphql
    MCP              /mcp

The process binds an ephemeral port, registers it with the trial's env
(MAKEFUN_ENV_URL/register), serves until the env answers GET /done with {"done": true}, then
exits 0. Nothing outside the standard library is used and no git binary is called.
"""

import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import gitwire, gql, mcp, rest, store  # noqa: E402

VERSION = 'forge-1.0.0'

# The trial's authenticated login: the one scenario viewer every check writes as. A forge run
# outside a trial may name its own with FORGE_LOGIN.
DEFAULT_LOGIN = 'adaiversen'
DEFAULT_EMAIL = 'adaiversen@example.invalid'
DEFAULT_NAME = 'ada iversen'

GIT_PATH = re.compile(r'^/([^/]+)/([^/]+?)\.git(?:/(.*))?$')

class ForgeServer(ThreadingHTTPServer):
    """A threaded server with a listen backlog deep enough for the benchmark's 50 concurrent
    clones: the default backlog of 5 drops their SYNs into a retransmission stall."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


FORGE = None
REST_API = None
MCP_API = None
TOKEN = ''
REQUEST_IDS = [0]
REQUEST_LOCK = threading.Lock()


def next_request_id():
    with REQUEST_LOCK:
        REQUEST_IDS[0] += 1
        return 'REQ-%08d' % REQUEST_IDS[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'forge/1.0'
    sys_version = ''

    # ------------------------------------------------------------------ plumbing

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        length = self.headers.get('Content-Length')
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                return b''
            if size <= 0:
                return b''
            chunks = []
            remaining = size
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b''.join(chunks)
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            out = []
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    break
                try:
                    size = int(line.strip().split(b';')[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(1024)
                    break
                out.append(self.rfile.read(size))
                self.rfile.readline(1024)
            return b''.join(out)
        return b''

    def _send(self, status, payload, headers=None, content_type='application/json; charset=utf-8'):
        if payload is None:
            body = b''
        elif isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload).encode('utf-8')
        try:
            self.send_response_only(status)
            if status != 204:
                if body:
                    self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self._headers_buffer.append(b'\r\n')
            payload = b''.join(self._headers_buffer)
            self._headers_buffer = []
            if body and self.command != 'HEAD' and status != 204:
                payload += body
            self._responded = True
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status, payload, headers=None):
        merged = {'X-GitHub-Request-Id': next_request_id()}
        merged.update(headers or {})
        self._send(status, payload, merged)

    # ------------------------------------------------------------------ verbs

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def do_OPTIONS(self):
        self._send(204, None, {'Allow': 'GET, POST, PUT, PATCH, DELETE'})

    # ------------------------------------------------------------------ routing

    def _dispatch(self, method):
        """Route one request; a fault answers a JSON 500 instead of dropping the connection."""
        try:
            self._route(method)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # never let a handler fault cost the client its answer
            if getattr(self, '_responded', False):
                self.close_connection = True
                return
            try:
                self._send_json(500, {'message': 'Internal Server Error: %s' % exc})
            except Exception:
                self.close_connection = True

    def _route(self, method):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {key: values[0] for key, values in
                 parse_qs(parsed.query, keep_blank_values=True).items()}
        body = self._read_body() if method in ('POST', 'PUT', 'PATCH', 'DELETE') else b''

        if path == '/healthz' or path == '/health':
            self._send(200, {'ok': True, 'status': 'ok', 'version': VERSION})
            return
        if path == '/api/v3' or path.startswith('/api/v3/'):
            self._rest(method, path, query, body)
            return
        if path == '/api/graphql':
            self._graphql(method, body)
            return
        if path == '/mcp' or path == '/mcp/':
            self._mcp(method, body)
            return
        match = GIT_PATH.match(path)
        if match:
            self._git(method, unquote(match.group(1)), unquote(match.group(2)),
                      match.group(3) or '', query, body)
            return
        self._send_json(404, {'message': 'Not Found'})

    # ------------------------------------------------------------------ surfaces

    def _authorized(self):
        header = self.headers.get('Authorization') or ''
        if not TOKEN:
            return True
        parts = header.split(' ', 1)
        if len(parts) != 2 or parts[0].lower() not in ('bearer', 'token'):
            return False
        return parts[1].strip() == TOKEN

    def _rest(self, method, path, query, body):
        if not self._authorized():
            self._send_json(401, {'message': 'Requires authentication'})
            return
        tail = path[len('/api/v3'):].strip('/')
        segments = [unquote(part) for part in tail.split('/')] if tail else []
        host = self.headers.get('Host') or 'localhost'
        base_url = 'http://' + host
        status, payload, headers = REST_API.handle(method, segments, query, body, base_url)
        if status == 204:
            self._send(204, None, dict(headers, **{'X-GitHub-Request-Id': next_request_id()}))
            return
        headers = dict(headers)
        headers.setdefault('X-OAuth-Scopes', 'repo, user, issues, pull_request')
        headers.setdefault('X-Accepted-OAuth-Scopes', 'repo')
        self._send_json(status, payload, headers)

    def _graphql(self, method, body):
        if method != 'POST':
            self._send_json(405, {'message': 'Method Not Allowed'})
            return
        if not self._authorized():
            self._send_json(401, {'message': 'Requires authentication'})
            return
        try:
            payload = json.loads(body.decode('utf-8')) if body else None
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {'message': 'Problems parsing JSON'})
            return
        forge = FORGE
        with forge.lock:
            if isinstance(payload, list):
                out = []
                for item in payload:
                    out.append(_graphql_one(item))
                self._send_json(200, out)
                return
            if not isinstance(payload, dict):
                self._send_json(400, {'message': 'Problems parsing JSON: an object is required'})
                return
            self._send_json(200, _graphql_one(payload))

    def _mcp(self, method, body):
        if method != 'POST':
            self._send_json(405, {'message': 'Method Not Allowed',
                                  'error': 'the MCP endpoint accepts POST'})
            return
        status, payload = MCP_API.handle_post(body, self._authorized())
        self._send(status, payload, {'MCP-Protocol-Version': mcp.PREFERRED_REVISION})

    def _git(self, method, owner, name, suffix, query, body=b''):
        repo = FORGE.get_repo(owner, name)
        suffix = suffix.strip('/')
        if suffix == 'info/refs':
            if method != 'GET':
                self._send(405, {'message': 'Method Not Allowed'})
                return
            service = query.get('service') or ''
            if service not in ('git-upload-pack', 'git-receive-pack'):
                self._send(400, {'message': 'dumb HTTP protocol is not supported'})
                return
            if repo is None:
                self._send_json(404, {'message': 'Repository not found'})
                return
            with FORGE.lock:
                if service == 'git-upload-pack':
                    protocol = self.headers.get('Git-Protocol') or ''
                    if 'version=2' in protocol:
                        body = gitwire.advertisement_v2(repo)
                    else:
                        body = gitwire.advertisement_v0(repo, service)
                else:
                    body = gitwire.advertisement_v0(repo, service)
            self._send(200, body,
                       {'Cache-Control': 'no-cache'},
                       content_type='application/x-%s-advertisement' % service)
            return
        if suffix in ('git-upload-pack', 'git-receive-pack'):
            if method != 'POST':
                self._send(405, {'message': 'Method Not Allowed'})
                return
            if repo is None:
                self._send_json(404, {'message': 'Repository not found'})
                return
            try:
                with FORGE.lock:
                    if suffix == 'git-upload-pack':
                        body = _upload_pack(repo, body)
                    else:
                        body = gitwire.receive_pack(repo, FORGE, body)
            except Exception as exc:
                self._send(400, {'message': 'the request could not be read: %s' % exc})
                return
            self._send(200, body, {'Cache-Control': 'no-cache'},
                       content_type='application/x-%s-result' % suffix)
            return
        self._send_json(404, {'message': 'Not Found'})


def _graphql_one(payload):
    if not isinstance(payload, dict):
        return {'errors': [{'message': 'Invalid Request: an operation object is required'}]}
    query = payload.get('query')
    variables = payload.get('variables') or {}
    operation = payload.get('operationName')
    if not isinstance(query, str) or not query.strip():
        return {'errors': [{'message': 'Invalid Request: query is required'}]}
    if not isinstance(variables, dict):
        variables = {}
    try:
        return gql.Executor(FORGE).run(query, variables, operation)
    except Exception as exc:
        return {'errors': [{'message': 'Internal error: %s' % exc}]}


def _upload_pack(repo, body):
    """Answer a git-upload-pack POST in whichever protocol the body asks for."""
    packets, _consumed = gitwire.parse_pkts(body)
    command = None
    args = []
    for kind, payload in packets:
        if kind != 'data':
            continue
        line = payload.decode('utf-8', 'replace').rstrip('\n')
        if not line:
            continue
        if command is None and line.startswith('command='):
            command = line.split('=', 1)[1].strip()
            continue
        if command is not None and '=' in line and not args and line.split(' ', 1)[0] in (
                'agent', 'object-format', 'server-option', 'session-id'):
            continue
        parts = line.split(' ')
        if len(parts) >= 2 and parts[0] in ('want', 'have', 'shallow', 'deepen-not'):
            args.append((parts[0], parts[1]))
        elif len(parts) >= 2 and parts[0] in ('deepen', 'deepen-since'):
            args.append((parts[0], parts[1]))
        elif len(parts) == 1 and parts[0] in ('peel', 'symrefs', 'unborn', 'thin-pack',
                                              'no-progress', 'ofs-delta', 'done',
                                              'wait-for-done'):
            args.append((parts[0], ''))
        elif len(parts) >= 2 and parts[0] in ('ref-prefix', 'filter'):
            args.append((parts[0], parts[1]))
    if command == 'ls-refs':
        return gitwire.ls_refs(repo, args)
    if command == 'fetch':
        return gitwire.fetch_v2(repo, args)
    return gitwire.fetch_v0(repo, args)


# --------------------------------------------------------------------------- lifecycle

def build_forge(login):
    now = os.environ.get('MAKEFUN_TRIAL_NOW') or '2026-01-01T00:00:00Z'
    data_dir = os.environ.get('FORGE_DATA_DIR')
    if not data_dir:
        try:
            import tempfile
            data_dir = tempfile.mkdtemp(prefix='forge-store-')
        except OSError:
            data_dir = None
    forge = store.Forge(login, DEFAULT_NAME, login + '@example.invalid', now, data_dir)
    forge.start_flusher()
    return forge


def register(env_url, port, attempts=120):
    """POST {"port": N} to the env until it accepts (the env is up before the entry starts)."""
    payload = json.dumps({'port': port}).encode()
    url = env_url.rstrip('/') + '/register'
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=payload, method='POST',
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return True
            if exc.code == 400:
                return False
        except Exception:
            pass
        time.sleep(min(2.0, 0.1 * (attempt + 1)))
    return False


def wait_for_done(env_url):
    """Poll GET /done with a growing delay; true once the suite has written its export."""
    url = env_url.rstrip('/') + '/done'
    delay = 0.25
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode('utf-8') or '{}')
            if payload.get('done') is True:
                return True
        except Exception:
            pass
        time.sleep(delay)
        delay = min(2.0, delay * 2)


def main(argv):
    global FORGE, REST_API, MCP_API, TOKEN

    port = 0
    env_url = os.environ.get('MAKEFUN_ENV_URL') or ''
    login = os.environ.get('FORGE_LOGIN') or DEFAULT_LOGIN
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg in ('--serve',):
            continue
        if arg in ('--port', '-p') and args:
            port = int(args.pop(0))
            continue
        if arg in ('--login',) and args:
            login = args.pop(0)
            continue
        if arg in ('--version', '-v'):
            sys.stdout.write(VERSION + '\n')
            return 0
        if arg in ('--help', '-h'):
            sys.stdout.write('usage: main.py [--serve] [--port N] [--login NAME]\n')
            return 0

    TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN') or ''
    FORGE = build_forge(login)
    REST_API = rest.RestApi(FORGE)
    MCP_API = mcp.McpApi(FORGE)

    host = '0.0.0.0'
    try:
        server = ForgeServer((host, port), Handler)
    except OSError:
        server = ForgeServer(('127.0.0.1', port), Handler)
    bound = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                              name='forge-http', daemon=True)
    thread.start()
    sys.stderr.write('%s listening on %s:%d\n' % (VERSION, host, bound))
    sys.stderr.flush()

    stopping = threading.Event()

    def on_signal(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if env_url:
        register(env_url, bound)
        while not stopping.is_set():
            if wait_for_done(env_url):
                break
    else:
        while not stopping.is_set():
            time.sleep(0.2)

    FORGE.stop()
    server.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
