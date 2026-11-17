#!/usr/bin/env python3
"""End-to-end smoke test of every surface of the forge, over HTTP only."""
import http.client
import json
import subprocess
import sys
import tempfile
import os

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8110
HOST = '127.0.0.1'
TOK = 'tok123'
BASE = 'http://%s:%d' % (HOST, PORT)


def call(method, path, body=None, token=TOK, extra=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    headers.update(extra or {})
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data, dict(resp.getheaders())


def j(method, path, payload=None, **kw):
    status, data, headers = call(method, path, None if payload is None else json.dumps(payload), **kw)
    try:
        parsed = json.loads(data) if data else None
    except ValueError:
        parsed = None
    return status, parsed, headers


ok = fail = 0


def check(label, condition, detail=''):
    global ok, fail
    if condition:
        ok += 1
        print('  ok   %s' % label)
    else:
        fail += 1
        print('  FAIL %s %s' % (label, detail))


print('REST')
status, repo, _ = j('POST', '/api/v3/user/repos', {'name': 'demo', 'private': True, 'description': 'demo repo'})
check('POST /user/repos -> 201', status == 201 and repo['name'] == 'demo', status)
status, root, headers = j('GET', '/api/v3/')
check('GET /api/v3/ 200 + X-OAuth-Scopes', status == 200 and 'X-OAuth-Scopes' in headers)
check('X-GitHub-Request-Id on every reply', bool(headers.get('X-GitHub-Request-Id')))
status, meta, _ = j('GET', '/api/v3/meta')
check('GET /meta installed_version', status == 200 and meta['installed_version'])
status, put, _ = j('PUT', '/api/v3/repos/adaiversen/demo/contents/docs/a.md',
                   {'message': 'add a.md', 'content': 'aGVsbG8gZm9yZ2UK'})
check('PUT /contents -> 201 with commit sha', status == 201 and len(put['commit']['sha']) == 40, status)
status, got, _ = j('GET', '/api/v3/repos/adaiversen/demo/contents/docs/a.md')
check('GET /contents file (base64, sha, size)', status == 200 and got['encoding'] == 'base64' and got['size'] == 12, status)
status, listing, _ = j('GET', '/api/v3/repos/adaiversen/demo/contents/docs')
check('GET /contents dir lists entries', status == 200 and listing[0]['name'] == 'a.md', status)
status, _, _ = j('PUT', '/api/v3/repos/adaiversen/demo/contents/docs/a.md',
                 {'message': 'stale', 'content': 'eA==', 'sha': '0' * 40})
check('PUT /contents stale sha -> 409', status == 409, status)
status, label, _ = j('POST', '/api/v3/repos/adaiversen/demo/labels', {'name': 'bug', 'color': 'ededed'})
check('POST /labels -> 201', status == 201 and label['name'] == 'bug', status)
status, issue, _ = j('POST', '/api/v3/repos/adaiversen/demo/issues', {'title': 'first', 'body': 'b', 'labels': ['bug']})
check('POST /issues -> 201 open with label', status == 201 and issue['state'] == 'open' and issue['labels'][0]['name'] == 'bug', status)
status, bad, _ = j('POST', '/api/v3/repos/adaiversen/demo/issues', {'title': 'x', 'labels': ['nope']})
check('POST /issues unknown label -> 422', status == 422 and bad['message'], status)
status, lst, _ = j('GET', '/api/v3/repos/adaiversen/demo/issues?state=open&per_page=10')
check('GET /issues?state=open lists it', status == 200 and lst[0]['number'] == issue['number'], status)
status, cm, _ = j('POST', '/api/v3/repos/adaiversen/demo/issues/%d/comments' % issue['number'], {'body': 'a comment'})
check('POST comment -> 201', status == 201 and cm['body'] == 'a comment', status)
status, patched, _ = j('PATCH', '/api/v3/repos/adaiversen/demo/issues/%d' % issue['number'], {'state': 'closed'})
check('PATCH issue closed', status == 200 and patched['state'] == 'closed', status)
status, _, _ = j('GET', '/api/v3/repos/adaiversen/nope')
check('missing repo -> 404 JSON', status == 404, status)
status, _, _ = j('GET', '/api/v3/repos/adaiversen/demo', token=None)
check('no token -> 401', status == 401, status)
status, data, _ = call('POST', '/api/v3/user/repos', '{"name":', extra={'Content-Type': 'application/json'})
check('malformed body -> 400', status == 400 and json.loads(data)['message'], (status, data))
status, _, _ = j('GET', '/api/v3/no-such-route')
check('unknown route -> 404 JSON', status == 404, status)
status, _, _ = j('GET', '/api/v3/repos/olgabauer/demo')
check('same name under another owner -> 404', status == 404, status)

print('git (real git binary against the forge)')
status, _, _ = j('POST', '/api/v3/user/repos', {'name': 'gitdemo', 'private': True})
check('a repository for the git checks', status == 201, status)
work = tempfile.mkdtemp()
subprocess.run(['git', 'init', '-q', '-b', 'main', work], check=True)
subprocess.run(['git', '-C', work, 'config', 'user.email', 'a@b.c'], check=True)
subprocess.run(['git', '-C', work, 'config', 'user.name', 'A'], check=True)
with open(os.path.join(work, 'f.txt'), 'w') as handle:
    handle.write('line one\n')
subprocess.run(['git', '-C', work, 'add', '-A'], check=True)
subprocess.run(['git', '-C', work, 'commit', '-qm', 'first'], check=True)
subprocess.run(['git', '-C', work, 'tag', '-a', 'v1', '-m', 'v1'], check=True)
push = subprocess.run(['git', '-C', work, 'push', '-q', BASE + '/adaiversen/gitdemo.git', 'main', 'v1'],
                      capture_output=True, text=True)
check('git push main + tag', push.returncode == 0, push.stderr[-200:])
clone_dir = os.path.join(tempfile.mkdtemp(), 'clone')
clone = subprocess.run(['git', 'clone', '-q', BASE + '/adaiversen/gitdemo.git', clone_dir],
                       capture_output=True, text=True)
check('git clone', clone.returncode == 0, clone.stderr[-200:])
log = subprocess.run(['git', '-C', clone_dir, 'log', '--format=%s'], capture_output=True, text=True).stdout.split()
check('cloned history matches', 'first' in log, log)
shallow_dir = os.path.join(tempfile.mkdtemp(), 'shallow')
shallow = subprocess.run(['git', 'clone', '-q', '--depth', '1', BASE + '/adaiversen/gitdemo.git', shallow_dir],
                         capture_output=True, text=True)
count = subprocess.run(['git', '-C', shallow_dir, 'rev-list', '--count', 'HEAD'],
                       capture_output=True, text=True).stdout.strip()
check('git clone --depth 1 lands one commit', shallow.returncode == 0 and count == '1', count)
with open(os.path.join(work, 'f.txt'), 'a') as handle:
    handle.write('line two\n')
subprocess.run(['git', '-C', work, 'commit', '-qam', 'second'], check=True)
ff = subprocess.run(['git', '-C', work, 'push', '-q', BASE + '/adaiversen/gitdemo.git', 'main'],
                    capture_output=True, text=True)
check('fast-forward push accepted', ff.returncode == 0, ff.stderr[-200:])
subprocess.run(['git', '-C', work, 'reset', '-q', '--hard', 'HEAD~1'], check=True)
with open(os.path.join(work, 'f.txt'), 'a') as handle:
    handle.write('divergent\n')
subprocess.run(['git', '-C', work, 'commit', '-qam', 'divergent'], check=True)
nff = subprocess.run(['git', '-C', work, 'push', BASE + '/adaiversen/gitdemo.git', 'main'],
                     capture_output=True, text=True)
check('non-fast-forward push refused', nff.returncode != 0 and 'rejected' in (nff.stderr + nff.stdout).lower(),
      nff.stderr[-160:])
fetch = subprocess.run(['git', '-C', clone_dir, 'fetch', '-q', 'origin'], capture_output=True, text=True)
check('incremental fetch', fetch.returncode == 0, fetch.stderr[-200:])
status, _, _ = j('GET', '/api/v3/repos/adaiversen/demo')
check('repo still readable after the refused push', status == 200)

print('REST pull request + merge')
status, branch, _ = j('GET', '/api/v3/repos/adaiversen/demo/git/refs/heads/main')
head_sha = branch['object']['sha']
status, created, _ = j('POST', '/api/v3/repos/adaiversen/demo/git/refs', {'ref': 'refs/heads/feature', 'sha': head_sha})
check('POST /git/refs creates a branch', status == 201, status)
status, write, _ = j('PUT', '/api/v3/repos/adaiversen/demo/contents/f.txt',
                     {'message': 'feature change', 'content': 'bGluZSBvbmUKZmVhdHVyZQo=', 'branch': 'feature'})
check('contents write on a branch', status in (200, 201), status)
status, pull, _ = j('POST', '/api/v3/repos/adaiversen/demo/pulls',
                    {'title': 'Feature', 'body': 'body', 'head': 'feature', 'base': 'main'})
check('POST /pulls -> 201 open with head/base', status == 201 and pull['state'] == 'open' and pull['head']['ref'] == 'feature', status)
status, files, _ = j('GET', '/api/v3/repos/adaiversen/demo/pulls/%d/files' % pull['number'])
check('GET /pulls/{n}/files lists the change', status == 200 and files and files[0]['filename'] == 'f.txt', status)
status, merged, _ = j('PUT', '/api/v3/repos/adaiversen/demo/pulls/%d/merge' % pull['number'], {'merge_method': 'squash'})
check('PUT /pulls/{n}/merge -> merged:true + sha', status == 200 and merged['merged'] is True and len(merged['sha']) == 40, status)
status, after, _ = j('GET', '/api/v3/repos/adaiversen/demo/pulls/%d' % pull['number'])
check('merged pull is closed with merge_commit_sha', after['state'] == 'closed' and after['merge_commit_sha'] == merged['sha'])
status, commits, _ = j('GET', '/api/v3/repos/adaiversen/demo/commits?sha=main')
check('squash commit is on the default branch', any(c['sha'] == merged['sha'] for c in commits))
status, content, _ = j('GET', '/api/v3/repos/adaiversen/demo/contents/f.txt?ref=main')
import base64
check('merged content on the default branch', 'feature' in base64.b64decode(content['content']).decode())
status, _, _ = j('PUT', '/api/v3/repos/adaiversen/demo/pulls/999/merge', {'merge_method': 'squash'})
check('merge unknown pull -> 404', status == 404, status)

print('GraphQL')
status, gql, _ = j('POST', '/api/graphql', {'query': 'query { viewer { login } }'})
check('query viewer', status == 200 and gql['data']['viewer']['login'] == 'adaiversen', gql)
status, gql, _ = j('POST', '/api/graphql', {
    'query': 'query R($owner: String!, $name: String!) { repository(owner: $owner, name: $name) '
             '{ id databaseId name nameWithOwner owner { login } defaultBranchRef { name } viewerPermission hasIssuesEnabled } }',
    'variables': {'owner': 'adaiversen', 'name': 'demo'}})
repo_node = gql['data']['repository']
check('repository fields', status == 200 and repo_node['nameWithOwner'] == 'adaiversen/demo' and repo_node['viewerPermission'] == 'ADMIN', gql)
status, gql, _ = j('POST', '/api/graphql', {'query': 'query { Issue: __type(name: "Issue") { fields { name } } '
                                                     'SearchType: __type(name: "SearchType") { enumValues { name } } }'})
fields = [f['name'] for f in gql['data']['Issue']['fields']]
enums = [e['name'] for e in gql['data']['SearchType']['enumValues']]
check('introspection lists Issue fields and SearchType ISSUE',
      all(name in fields for name in ('id', 'number', 'title', 'state', 'body')) and 'ISSUE' in enums)
status, gql, _ = j('POST', '/api/graphql', {
    'query': 'mutation C($input: CreateIssueInput!) { createIssue(input: $input) { issue { number id } } }',
    'variables': {'input': {'repositoryId': repo_node['id'], 'title': 'from graphql', 'body': 'b'}}})
number = gql['data']['createIssue']['issue']['number']
check('mutation createIssue', status == 200 and isinstance(number, int), gql)
status, rest_issue, _ = j('GET', '/api/v3/repos/adaiversen/demo/issues/%d' % number)
check('the GraphQL issue is the REST object', rest_issue['title'] == 'from graphql')
status, gql, _ = j('POST', '/api/graphql', {'query': 'query { viewer { login noSuchField } }'})
check('unknown field -> 200 with errors naming it',
      status == 200 and 'noSuchField' in json.dumps(gql['errors']))
status, gql, _ = j('POST', '/api/graphql', {
    'query': 'query R { repository(owner: "adaiversen", name: "ghost") { id } }'})
check('unknown repository -> data.repository null, no errors',
      gql['data']['repository'] is None and 'errors' not in gql)
status, gql, _ = j('POST', '/api/graphql', [
    {'query': 'query { viewer { login } }'},
    {'query': 'query { SearchType: __type(name: "SearchType") { name } }'}])
check('two operations in one body answer in order',
      status == 200 and gql[0]['data']['viewer']['login'] == 'adaiversen' and gql[1]['data']['SearchType']['name'] == 'SearchType')
status, _, _ = j('POST', '/api/graphql', {'query': 'query { viewer { login } }'}, token=None)
check('unauthenticated graphql -> 401', status == 401, status)

print('MCP')
status, disc, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 'discover-1', 'method': 'server/discover',
                                    'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}},
                    extra={'Mcp-Protocol-Version': '2026-07-28', 'Accept': 'application/json, text/event-stream'})
check('server/discover lists supportedVersions',
      status == 200 and disc['result']['supportedVersions'][0] == '2026-07-28'
      and disc['result']['serverInfo']['name'] and disc['result']['capabilities']['tools'] is not None, disc)
status, tools, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
names = [tool['name'] for tool in tools['result']['tools']]
check('tools/list offers the six required tools',
      all(name in names for name in ('create_repo', 'get_repo', 'create_issue', 'list_issues',
                                     'create_pull_request', 'merge_pull_request')), names)
check('every tool has a description and an object inputSchema',
      all(tool['description'] and tool['inputSchema']['type'] == 'object' for tool in tools['result']['tools']))
status, called, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                      'params': {'name': 'create_issue',
                                                 'arguments': {'owner': 'adaiversen', 'repo': 'demo',
                                                               'title': 'from mcp', 'body': 'b'}}})
check('tools/call create_issue', status == 200 and called['result']['isError'] is False
      and 'from mcp' in called['result']['content'][0]['text'], called)
status, called, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                      'params': {'name': 'nope', 'arguments': {}}})
check('unknown tool -> JSON-RPC -32602', called['error']['code'] == -32602, called)
status, called, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                      'params': {'name': 'create_issue', 'arguments': {}}})
check('missing arguments -> isError', called['result']['isError'] is True, called)
status, _, _ = j('POST', '/mcp', {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/list', 'params': {}}, token=None)
check('unauthenticated mcp -> 401', status == 401, status)
conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
conn.request('POST', '/mcp', body='{"jsonrpc": "2.0", "id": 9,', headers={'Authorization': 'Bearer ' + TOK,
                                                                          'Content-Type': 'application/json'})
resp = conn.getresponse()
broken = json.loads(resp.read())
conn.close()
check('body that is not JSON -> 400 with -32700', resp.status == 400 and broken['error']['code'] == -32700,
      (resp.status, broken))

print('\n%d passed, %d failed' % (ok, fail))
sys.exit(1 if fail else 0)
