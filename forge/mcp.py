"""The Model Context Protocol endpoint: JSON-RPC 2.0 over the Streamable HTTP transport.

Speaks revision 2026-07-28 (`server/discover`, per-request `_meta` protocol versions) and answers
the legacy `initialize` handshake as well. The tools act on the same store REST serves.
"""

import json

SERVER_INFO = {'name': 'forge', 'version': '1.0.0'}
PREFERRED_REVISION = '2026-07-28'
SUPPORTED_VERSIONS = ['2026-07-28', '2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05']

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _schema(props, required):
    return {'type': 'object', 'properties': props, 'required': required,
            'additionalProperties': True}


_OWNER = {'type': 'string', 'description': 'The account login that owns the repository.'}
_REPO = {'type': 'string', 'description': 'The repository name.'}
_TITLE = {'type': 'string', 'description': 'The title.'}
_BODY = {'type': 'string', 'description': 'The body text.'}

TOOLS = [
    {
        'name': 'create_repo',
        'title': 'Create repository',
        'description': 'Create a repository on the forge, owned by the authenticated account.',
        'inputSchema': _schema({
            'name': {'type': 'string', 'description': 'The repository name.'},
            'description': {'type': 'string', 'description': 'A short description.'},
            'private': {'type': 'boolean', 'description': 'Whether the repository is private.'},
        }, ['name']),
    },
    {
        'name': 'get_repo',
        'title': 'Get repository',
        'description': 'Read one repository: its id, name, owner and default branch.',
        'inputSchema': _schema({'owner': _OWNER, 'repo': _REPO}, ['owner', 'repo']),
    },
    {
        'name': 'list_repos',
        'title': 'List repositories',
        'description': 'List the repositories the authenticated account owns.',
        'inputSchema': _schema({}, []),
    },
    {
        'name': 'create_issue',
        'title': 'Create issue',
        'description': 'Open an issue in a repository.',
        'inputSchema': _schema({'owner': _OWNER, 'repo': _REPO, 'title': _TITLE, 'body': _BODY},
                               ['owner', 'repo', 'title']),
    },
    {
        'name': 'get_issue',
        'title': 'Get issue',
        'description': 'Read one issue by its number.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'issue_number': {'type': 'integer', 'description': 'The issue number.'},
        }, ['owner', 'repo', 'issue_number']),
    },
    {
        'name': 'list_issues',
        'title': 'List issues',
        'description': 'List the issues of a repository, optionally filtered by state.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'state': {'type': 'string', 'description': 'open, closed or all.'},
        }, ['owner', 'repo']),
    },
    {
        'name': 'close_issue',
        'title': 'Close issue',
        'description': 'Close an issue by its number.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'issue_number': {'type': 'integer', 'description': 'The issue number.'},
        }, ['owner', 'repo', 'issue_number']),
    },
    {
        'name': 'create_pull_request',
        'title': 'Create pull request',
        'description': 'Open a pull request from a head branch into a base branch.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO, 'title': _TITLE, 'body': _BODY,
            'head': {'type': 'string', 'description': 'The head branch name.'},
            'base': {'type': 'string', 'description': 'The base branch name.'},
        }, ['owner', 'repo', 'title', 'head', 'base']),
    },
    {
        'name': 'get_pull_request',
        'title': 'Get pull request',
        'description': 'Read one pull request by its number.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'pull_number': {'type': 'integer', 'description': 'The pull request number.'},
        }, ['owner', 'repo', 'pull_number']),
    },
    {
        'name': 'list_pull_requests',
        'title': 'List pull requests',
        'description': 'List the pull requests of a repository.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'state': {'type': 'string', 'description': 'open, closed or all.'},
        }, ['owner', 'repo']),
    },
    {
        'name': 'merge_pull_request',
        'title': 'Merge pull request',
        'description': 'Squash-merge a pull request into its base branch.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'pull_number': {'type': 'integer', 'description': 'The pull request number.'},
        }, ['owner', 'repo', 'pull_number']),
    },
    {
        'name': 'create_label',
        'title': 'Create label',
        'description': 'Create an issue label in a repository.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'name': {'type': 'string', 'description': 'The label name.'},
            'color': {'type': 'string', 'description': 'A six-digit hex colour.'},
        }, ['owner', 'repo', 'name']),
    },
    {
        'name': 'list_branches',
        'title': 'List branches',
        'description': 'List the branches of a repository and the commit each points at.',
        'inputSchema': _schema({'owner': _OWNER, 'repo': _REPO}, ['owner', 'repo']),
    },
    {
        'name': 'get_file',
        'title': 'Get file',
        'description': 'Read a file of a repository at a branch, decoded from base64.',
        'inputSchema': _schema({
            'owner': _OWNER, 'repo': _REPO,
            'path': {'type': 'string', 'description': 'The repository-relative path.'},
            'ref': {'type': 'string', 'description': 'A branch name or commit id.'},
        }, ['owner', 'repo', 'path']),
    },
]

TOOLS_BY_NAME = {tool['name']: tool for tool in TOOLS}


class ToolError(Exception):
    """A tool ran and refused: the JSON-RPC answer carries isError: true."""


def _arg(args, *names):
    for name in names:
        if name in args and args[name] is not None:
            return args[name]
    return None


class McpApi:
    def __init__(self, forge):
        self.forge = forge

    # ------------------------------------------------------------------ transport

    def handle_post(self, body_bytes, authorized):
        if not authorized:
            return 401, {'error': 'unauthorized', 'message': 'Authorization: Bearer <token> is required'}
        text = body_bytes.decode('utf-8', 'replace') if body_bytes else ''
        try:
            payload = json.loads(text)
        except ValueError:
            return 400, {'jsonrpc': '2.0', 'id': None,
                         'error': {'code': PARSE_ERROR, 'message': 'Parse error: the body is not JSON'}}
        if isinstance(payload, list):
            out = []
            for item in payload:
                answer = self._one(item)
                if answer is not None:
                    out.append(answer)
            return 200, out
        answer = self._one(payload)
        if answer is None:
            return 202, None
        return 200, answer

    def _one(self, message):
        if not isinstance(message, dict):
            return {'jsonrpc': '2.0', 'id': None,
                    'error': {'code': INVALID_REQUEST, 'message': 'Invalid Request'}}
        method = message.get('method')
        msg_id = message.get('id')
        is_notification = 'id' not in message
        params = message.get('params') or {}
        if not isinstance(params, dict):
            params = {}
        if method is None:
            if is_notification:
                return None
            return self._error(msg_id, INVALID_REQUEST, 'Invalid Request: method is required')
        try:
            result = self._dispatch(method, params)
        except ToolError as exc:
            if is_notification:
                return None
            return self._error(msg_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # a tool fault is a tool error, never a 500
            if is_notification:
                return None
            return self._error(msg_id, INTERNAL_ERROR, 'Internal error: %s' % exc)
        if is_notification or result is _NO_RESULT:
            return None
        if isinstance(result, dict) and result.get('__rpc_error__'):
            return self._error(msg_id, result['code'], result['message'])
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}

    @staticmethod
    def _error(msg_id, code, message):
        return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}

    def _dispatch(self, method, params):
        if method == 'server/discover':
            return {
                'resultType': 'complete',
                'supportedVersions': list(SUPPORTED_VERSIONS),
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': dict(SERVER_INFO),
                'instructions': 'A GitHub-compatible forge: repositories, issues and pull requests.',
                '_meta': {'io.modelcontextprotocol/serverInfo': dict(SERVER_INFO)},
            }
        if method == 'initialize':
            revision = params.get('protocolVersion')
            if not isinstance(revision, str) or revision not in SUPPORTED_VERSIONS:
                revision = PREFERRED_REVISION
            return {'protocolVersion': revision,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': dict(SERVER_INFO)}
        if method == 'notifications/initialized' or method.startswith('notifications/'):
            return _NO_RESULT
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': TOOLS}
        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments')
            if not isinstance(args, dict):
                args = {}
            if not isinstance(name, str) or name not in TOOLS_BY_NAME:
                return {'__rpc_error__': True, 'code': INVALID_PARAMS,
                        'message': 'Unknown tool: %s' % name}
            handler = getattr(self, 'tool_' + name)
            try:
                result = handler(args)
            except ToolError as exc:
                return {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            except Exception as exc:
                return {'content': [{'type': 'text', 'text': 'tool failed: %s' % exc}], 'isError': True}
            text = json.dumps(result, sort_keys=True)
            return {'content': [{'type': 'text', 'text': text}],
                    'structuredContent': result,
                    'isError': False}
        return {'__rpc_error__': True, 'code': METHOD_NOT_FOUND,
                'message': 'Method not found: %s' % method}

    # ------------------------------------------------------------------ tools

    def _repo(self, args, required=True):
        owner = _arg(args, 'owner', 'owner_login', 'org', 'login')
        name = _arg(args, 'repo', 'repository', 'repo_name', 'repository_name')
        if isinstance(_arg(args, 'full_name'), str) and '/' in _arg(args, 'full_name'):
            owner, _, name = _arg(args, 'full_name').partition('/')
        if not owner or not name:
            if required:
                raise ToolError('owner and repo are required')
            return None
        repo = self.forge.owned(str(owner), str(name))
        if repo is None:
            raise ToolError('repository %s/%s was not found' % (owner, name))
        return repo

    def _number(self, args, *names):
        value = _arg(args, *names)
        if value is None:
            raise ToolError('%s is required' % names[0])
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolError('%s must be an integer' % names[0])

    def tool_create_repo(self, args):
        name = _arg(args, 'name', 'repo', 'repository_name')
        if not isinstance(name, str) or not name:
            raise ToolError('name is required')
        forge = self.forge
        with forge.lock:
            existing = forge.get_repo(forge.login, name)
            if existing is not None:
                return _repo_summary(existing)
            repo = forge.create_repo(forge.login, name, bool(_arg(args, 'private') or False),
                                     _arg(args, 'description') or '')
            return _repo_summary(repo)

    def tool_get_repo(self, args):
        with self.forge.lock:
            return _repo_summary(self._repo(args))

    def tool_list_repos(self, args):
        with self.forge.lock:
            return {'repositories': [_repo_summary(repo)
                                     for repo in sorted(self.forge.repos_of(self.forge.login),
                                                        key=lambda item: item.id)]}

    def tool_create_issue(self, args):
        title = _arg(args, 'title', 'issue_title', 'name')
        if not isinstance(title, str) or not title:
            raise ToolError('title is required')
        with self.forge.lock:
            repo = self._repo(args)
            issue = self.forge.new_issue(repo, title, _arg(args, 'body') or '')
            return _issue_summary(repo, issue)

    def tool_get_issue(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            issue = self.forge.find_issue(repo, self._number(args, 'issue_number', 'number'))
            if issue is None:
                raise ToolError('issue was not found')
            return _issue_summary(repo, issue)

    def tool_list_issues(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            state = (_arg(args, 'state') or 'open')
            issues = list(repo.issues)
            if isinstance(state, str) and state != 'all':
                issues = [issue for issue in issues if issue['state'] == state]
            return {'issues': [_issue_summary(repo, issue) for issue in issues],
                    'total_count': len(issues)}

    def tool_close_issue(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            issue = self.forge.find_issue(repo, self._number(args, 'issue_number', 'number'))
            if issue is None:
                raise ToolError('issue was not found')
            issue['state'] = 'closed'
            issue['closed_at'] = self.forge.now
            issue['updated_at'] = self.forge.now
            self.forge.touch()
            return _issue_summary(repo, issue)

    def tool_create_pull_request(self, args):
        title = _arg(args, 'title', 'pr_title', 'pull_request_title')
        head = _arg(args, 'head', 'head_ref', 'head_branch', 'source', 'from', 'branch')
        base = _arg(args, 'base', 'base_ref', 'base_branch', 'target', 'into')
        if not isinstance(head, str) or not head:
            raise ToolError('head is required')
        with self.forge.lock:
            repo = self._repo(args)
            base = base if isinstance(base, str) and base else repo.default_branch
            head_sha = repo.branch_tip(head)
            base_sha = repo.branch_tip(base)
            if head_sha is None:
                raise ToolError('head branch %s does not exist' % head)
            if base_sha is None:
                raise ToolError('base branch %s does not exist' % base)
            for existing in repo.pulls:
                if existing['state'] == 'open' and existing['head_ref'] == head and existing['base_ref'] == base:
                    return _pull_summary(repo, existing)
            pull = self.forge.new_pull(repo, title or head, _arg(args, 'body') or '',
                                       head, head_sha, base, base_sha)
            return _pull_summary(repo, pull)

    def tool_get_pull_request(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            pull = self.forge.find_pull(repo, self._number(args, 'pull_number', 'number'))
            if pull is None:
                raise ToolError('pull request was not found')
            return _pull_summary(repo, pull)

    def tool_list_pull_requests(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            state = _arg(args, 'state') or 'open'
            pulls = list(repo.pulls)
            if isinstance(state, str) and state != 'all':
                pulls = [pull for pull in pulls if pull['state'] == state]
            return {'pull_requests': [_pull_summary(repo, pull) for pull in pulls],
                    'total_count': len(pulls)}

    def tool_merge_pull_request(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            number = self._number(args, 'pull_number', 'pull_request_number', 'number')
            pull = self.forge.find_pull(repo, number)
            if pull is None:
                raise ToolError('pull request #%d was not found' % number)
            if pull['merged']:
                return _pull_summary(repo, pull)
            if repo.pull_conflicts(pull):
                raise ToolError('pull request #%d is not mergeable' % number)
            self.forge.merge_pull(repo, pull, 'squash')
            return _pull_summary(repo, pull)

    def tool_create_label(self, args):
        name = _arg(args, 'name', 'label')
        if not isinstance(name, str) or not name:
            raise ToolError('name is required')
        with self.forge.lock:
            repo = self._repo(args)
            if name not in repo.labels:
                repo.labels[name] = {'id': repo.next_label, 'name': name,
                                     'color': _arg(args, 'color') or 'ededed',
                                     'description': _arg(args, 'description') or ''}
                repo.next_label += 1
                self.forge.touch()
            return dict(repo.labels[name])

    def tool_list_branches(self, args):
        with self.forge.lock:
            repo = self._repo(args)
            return {'branches': [{'name': ref[len('refs/heads/'):], 'sha': repo.refs[ref]}
                                 for ref in sorted(repo.refs) if ref.startswith('refs/heads/')]}

    def tool_get_file(self, args):
        path = _arg(args, 'path', 'file_path')
        if not isinstance(path, str) or not path:
            raise ToolError('path is required')
        with self.forge.lock:
            repo = self._repo(args)
            ref = _arg(args, 'ref') or repo.default_branch
            tip = repo.branch_tip(ref) or (ref if repo.has(ref) else None)
            if tip is None:
                raise ToolError('ref %s does not exist' % ref)
            files = repo.files_at(tip)
            if path not in files:
                raise ToolError('file %s was not found' % path)
            oid = files[path][1]
            blob = repo.get(oid)
            import base64
            return {'path': path, 'sha': oid, 'size': len(blob[1]) if blob else 0,
                    'content': base64.b64encode(blob[1]).decode() if blob else ''}


class _NoResult:
    pass


_NO_RESULT = _NoResult()


def _repo_summary(repo):
    return {'id': repo.id, 'name': repo.name, 'full_name': '%s/%s' % (repo.owner, repo.name),
            'owner': {'login': repo.owner}, 'private': repo.private,
            'description': repo.description, 'default_branch': repo.default_branch,
            'html_url': 'http://localhost/%s/%s' % (repo.owner, repo.name)}


def _issue_summary(repo, issue):
    return {'id': issue['id'], 'number': issue['number'], 'title': issue['title'],
            'body': issue['body'], 'state': issue['state'],
            'labels': list(issue['labels']), 'comments': len(issue['comments']),
            'created_at': issue['created_at'],
            'html_url': 'http://localhost/%s/%s/issues/%d' % (repo.owner, repo.name, issue['number'])}


def _pull_summary(repo, pull):
    return {'id': pull['id'], 'number': pull['number'], 'title': pull['title'],
            'body': pull['body'], 'state': pull['state'], 'merged': pull['merged'],
            'head': {'ref': pull['head_ref']}, 'base': {'ref': pull['base_ref']},
            'merge_commit_sha': pull['merge_commit_sha'],
            'html_url': 'http://localhost/%s/%s/pull/%d' % (repo.owner, repo.name, pull['number'])}
