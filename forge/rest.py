"""The GitHub REST v3 surface: repositories, contents, branches, commits, labels, issues and pulls.

Every reply is JSON with a `message` string on an error, and the caller adds the request-id
header. Ownership follows the bearer token: only the token's login can see or touch a repository.
"""

import base64
import json

from . import gitobj

PER_PAGE_DEFAULT = 30


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def b64(data):
    return base64.b64encode(data).decode('ascii')


def identity(forge):
    return '%s <%s> %s' % (forge.login, forge.email, forge.now)


# --------------------------------------------------------------------------- serialisers

def repo_json(repo):
    return {
        'id': repo.id,
        'node_id': 'R_kgDO%06d' % repo.id,
        'name': repo.name,
        'full_name': '%s/%s' % (repo.owner, repo.name),
        'owner': {'login': repo.owner, 'id': 1000, 'type': 'User',
                  'avatar_url': 'http://localhost/avatar/%s' % repo.owner},
        'private': repo.private,
        'fork': False,
        'description': repo.description,
        'default_branch': repo.default_branch,
        'has_issues': True,
        'has_projects': False,
        'has_wiki': False,
        'open_issues_count': len([i for i in repo.issues if i['state'] == 'open']),
        'visibility': 'private' if repo.private else 'public',
        'html_url': 'http://localhost/%s/%s' % (repo.owner, repo.name),
        'url': 'http://localhost/api/v3/repos/%s/%s' % (repo.owner, repo.name),
        'created_at': repo.created_at,
        'updated_at': repo.updated_at,
        'pushed_at': repo.pushed_at,
        'permissions': {'admin': True, 'maintain': True, 'push': True, 'triage': True, 'pull': True},
    }


def label_json(repo, name):
    record = repo.labels.get(name) or {'name': name, 'color': 'ededed', 'id': 0}
    return {'id': record.get('id', 0), 'name': record['name'],
            'color': record.get('color', 'ededed'),
            'description': record.get('description', ''),
            'default': False,
            'url': 'http://localhost/api/v3/repos/%s/%s/labels/%s' % (repo.owner, repo.name, record['name'])}


def issue_json(repo, issue):
    base = 'http://localhost/api/v3/repos/%s/%s/issues/%d' % (repo.owner, repo.name, issue['number'])
    return {
        'id': issue['id'],
        'node_id': 'I_kwDO%06d' % issue['id'],
        'number': issue['number'],
        'title': issue['title'],
        'body': issue['body'],
        'state': issue['state'],
        'state_reason': None,
        'locked': False,
        'labels': [label_json(repo, name) for name in issue['labels']],
        'user': {'login': repo.owner, 'id': 1000, 'type': 'User'},
        'assignee': None,
        'assignees': [],
        'milestone': None,
        'comments': len(issue['comments']),
        'created_at': issue['created_at'],
        'updated_at': issue['updated_at'],
        'closed_at': issue['closed_at'],
        'author_association': 'OWNER',
        'html_url': base.replace('/api/v3/repos', ''),
        'url': base,
        'repository_url': 'http://localhost/api/v3/repos/%s/%s' % (repo.owner, repo.name),
    }


def comment_json(repo, issue, comment):
    return {
        'id': comment['id'],
        'node_id': 'IC_kwDO%06d' % comment['id'],
        'body': comment['body'],
        'user': {'login': repo.owner, 'id': 1000, 'type': 'User'},
        'created_at': comment['created_at'],
        'updated_at': comment['created_at'],
        'url': 'http://localhost/api/v3/repos/%s/%s/issues/%d/comments/%d' % (
            repo.owner, repo.name, issue['number'], comment['id']),
        'issue_url': 'http://localhost/api/v3/repos/%s/%s/issues/%d' % (repo.owner, repo.name, issue['number']),
    }


def pull_json(repo, pull):
    head_sha = repo.branch_tip(pull['head_ref']) or pull['head_sha']
    base_sha = repo.branch_tip(pull['base_ref']) or pull['base_sha']
    merge_base = repo.merge_base(base_sha, head_sha) if base_sha and head_sha else None
    files = repo.diff_files(merge_base, head_sha) if head_sha else []
    additions = sum(entry['additions'] for entry in files)
    deletions = sum(entry['deletions'] for entry in files)
    url = 'http://localhost/api/v3/repos/%s/%s/pulls/%d' % (repo.owner, repo.name, pull['number'])
    return {
        'id': pull['id'],
        'node_id': 'PR_kwDO%06d' % pull['id'],
        'number': pull['number'],
        'title': pull['title'],
        'body': pull['body'],
        'state': pull['state'],
        'locked': False,
        'draft': False,
        'merged': pull['merged'],
        'mergeable': not repo.pull_conflicts(pull) if pull['state'] == 'open' else None,
        'mergeable_state': 'clean' if not repo.pull_conflicts(pull) else 'dirty',
        'merged_at': pull['merged_at'],
        'merge_commit_sha': pull['merge_commit_sha'],
        'created_at': pull['created_at'],
        'updated_at': pull['updated_at'],
        'closed_at': pull['merged_at'],
        'user': {'login': repo.owner, 'id': 1000, 'type': 'User'},
        'labels': [],
        'head': {'ref': pull['head_ref'], 'sha': head_sha,
                 'label': '%s:%s' % (repo.owner, pull['head_ref']),
                 'user': {'login': repo.owner}, 'repo': repo_json(repo)},
        'base': {'ref': pull['base_ref'], 'sha': base_sha,
                 'label': '%s:%s' % (repo.owner, pull['base_ref']),
                 'user': {'login': repo.owner}, 'repo': repo_json(repo)},
        'additions': additions,
        'deletions': deletions,
        'changed_files': len(files),
        'commits': len(repo.walk_commits(head_sha)) if head_sha else 0,
        'html_url': url.replace('/api/v3/repos', ''),
        'url': url,
        'diff_url': url + '.diff',
        'patch_url': url + '.patch',
    }


# --------------------------------------------------------------------------- the API

class RestApi:
    def __init__(self, forge):
        self.forge = forge

    # ------------------------------------------------------------------ entry

    def handle(self, method, segments, query, body_bytes, base_url):
        forge = self.forge
        with forge.lock:
            try:
                return self._route(method, segments, query, body_bytes, base_url)
            except HttpError as exc:
                return exc.status, {'message': exc.message}, {}

    def _route(self, method, seg, query, body, base_url):
        if not seg:
            return 200, {
                'current_user_url': base_url + '/api/v3/user',
                'current_user_authorizations_html_url': base_url + '/settings/connections/applications',
                'repository_url': base_url + '/api/v3/repos/{owner}/{repo}',
                'user_repositories_url': base_url + '/api/v3/user/repos',
                'rate_limit_url': base_url + '/api/v3/rate_limit',
                'emojis_url': base_url + '/api/v3/emojis',
            }, {}
        head = seg[0]
        if head == 'meta' and len(seg) == 1:
            return 200, {'installed_version': 'forge-1.0.0', 'verifiable_password_authentication': False}, {}
        if head == 'user' and len(seg) == 1:
            return 200, self._user(), {}
        if head == 'user' and len(seg) == 2 and seg[1] == 'repos':
            if method == 'GET':
                return self._list_repos(query, base_url)
            if method == 'POST':
                return self._create_repo(body)
            raise HttpError(405, 'Method Not Allowed')
        if head == 'repos' and len(seg) >= 3:
            return self._repo_route(method, seg[1], seg[2], seg[3:], query, body)
        raise HttpError(404, 'Not Found')

    # ------------------------------------------------------------------ helpers

    def _user(self):
        forge = self.forge
        return {'login': forge.login, 'id': 1000, 'node_id': 'U_kgDO000001',
                'name': forge.name, 'email': forge.email, 'type': 'User',
                'site_admin': False, 'public_repos': len(forge.repos_of(forge.login)),
                'total_private_repos': len(forge.repos_of(forge.login)),
                'html_url': 'http://localhost/' + forge.login}

    def _repo(self, owner, name):
        repo = self.forge.owned(owner, name)
        if repo is None:
            raise HttpError(404, 'Not Found')
        return repo

    @staticmethod
    def _paged(items, query):
        per_page = _int_param(query, 'per_page', PER_PAGE_DEFAULT, 1, 100)
        page = _int_param(query, 'page', 1, 1, 10 ** 6)
        start = (page - 1) * per_page
        return items[start:start + per_page], per_page, page, len(items)

    def _list_repos(self, query, base_url):
        repos = sorted(self.forge.repos_of(self.forge.login), key=lambda repo: repo.id)
        page_items, per_page, page, total = self._paged(repos, query)
        headers = {}
        last = max(1, (total + per_page - 1) // per_page)
        links = []
        if page < last:
            links.append('<%s/api/v3/user/repos?per_page=%d&page=%d>; rel="next"' % (base_url, per_page, page + 1))
            links.append('<%s/api/v3/user/repos?per_page=%d&page=%d>; rel="last"' % (base_url, per_page, last))
        if page > 1:
            links.append('<%s/api/v3/user/repos?per_page=%d&page=%d>; rel="prev"' % (base_url, per_page, page - 1))
            links.append('<%s/api/v3/user/repos?per_page=%d&page=1>; rel="first"' % (base_url, per_page))
        if links:
            headers['Link'] = ', '.join(links)
        return 200, [repo_json(repo) for repo in page_items], headers

    def _create_repo(self, body):
        data = _json_body(body)
        name = data.get('name')
        if not isinstance(name, str) or not name:
            raise HttpError(422, 'Repository creation failed: name is required')
        forge = self.forge
        if forge.get_repo(forge.login, name) is not None:
            raise HttpError(422, 'Repository creation failed: name already exists on this account')
        repo = forge.create_repo(forge.login, name, bool(data.get('private', False)),
                                 data.get('description') or '')
        if data.get('auto_init'):
            tree = gitobj.build_tree_from_files(repo, {'README.md': ('100644', repo.put('blob', b'# ' + name.encode() + b'\n'))})
            ident = identity(forge)
            commit = repo.put('commit', gitobj.build_commit(tree, [], 'Initial commit\n', ident, ident))
            repo.refs['refs/heads/' + repo.default_branch] = commit
        return 201, repo_json(repo), {}

    # ------------------------------------------------------------------ repository routes

    def _repo_route(self, method, owner, name, rest, query, body):
        repo = self._repo(owner, name)
        if not rest:
            if method == 'GET':
                return 200, repo_json(repo), {}
            if method == 'PATCH':
                data = _json_body(body)
                if 'description' in data:
                    repo.description = data.get('description') or ''
                if 'private' in data:
                    repo.private = bool(data['private'])
                if 'name' in data and isinstance(data['name'], str) and data['name'] != repo.name:
                    self.forge.repos.pop(self.forge.key(repo.owner, repo.name), None)
                    repo.name = data['name']
                    self.forge.repos[self.forge.key(repo.owner, repo.name)] = repo
                if 'default_branch' in data and isinstance(data['default_branch'], str):
                    repo.default_branch = data['default_branch']
                repo.updated_at = self.forge.now
                self.forge.touch()
                return 200, repo_json(repo), {}
            if method == 'DELETE':
                self.forge.delete_repo(repo)
                return 204, None, {}
            raise HttpError(405, 'Method Not Allowed')

        section = rest[0]
        if section == 'branches':
            return self._branches(repo, rest[1:], method)
        if section == 'commits':
            return self._commits(repo, query)
        if section == 'git':
            return self._git_data(repo, rest[1:], method, body)
        if section == 'contents':
            return self._contents(repo, rest[1:], method, query, body)
        if section == 'labels':
            return self._labels(repo, rest[1:], method, body)
        if section == 'issues':
            return self._issues(repo, rest[1:], method, query, body)
        if section == 'pulls':
            return self._pulls(repo, rest[1:], method, query, body)
        raise HttpError(404, 'Not Found')

    def _branches(self, repo, rest, method):
        if method != 'GET':
            raise HttpError(405, 'Method Not Allowed')
        if not rest:
            out = []
            for ref in sorted(repo.refs):
                if not ref.startswith('refs/heads/'):
                    continue
                out.append({'name': ref[len('refs/heads/'):],
                            'commit': {'sha': repo.refs[ref],
                                       'url': 'http://localhost/api/v3/repos/%s/%s/commits/%s' % (
                                           repo.owner, repo.name, repo.refs[ref])},
                            'protected': False})
            return 200, out, {}
        branch = rest[0]
        oid = repo.branch_tip(branch)
        if oid is None:
            raise HttpError(404, 'Branch not found')
        return 200, {'name': branch, 'commit': {'sha': oid}, 'protected': False}, {}

    def _commits(self, repo, query):
        ref = query.get('sha') or query.get('ref') or repo.default_branch
        tip = _resolve_ref(repo, ref)
        if tip is None:
            raise HttpError(404, 'No commit found for the ref')
        out = []
        for oid in repo.walk_commits(tip, limit=500):
            obj = repo.get(oid)
            if obj is None:
                continue
            commit = gitobj.parse_commit(obj[1])
            out.append({
                'sha': oid,
                'node_id': 'C_%s' % oid,
                'commit': {
                    'message': commit['message'].decode('utf-8', 'replace').rstrip('\n'),
                    'author': _person(commit['author']),
                    'committer': _person(commit['committer']),
                    'tree': {'sha': commit['tree']},
                },
                'parents': [{'sha': parent} for parent in commit['parents']],
                'url': 'http://localhost/api/v3/repos/%s/%s/commits/%s' % (repo.owner, repo.name, oid),
            })
        return 200, out, {}

    def _git_data(self, repo, rest, method, body):
        if rest[:1] == ['refs']:
            if len(rest) == 1 and method == 'POST':
                data = _json_body(body)
                ref = data.get('ref')
                sha = data.get('sha')
                if not isinstance(ref, str) or not ref.startswith('refs/'):
                    raise HttpError(422, 'Reference update failed: ref is required')
                if not isinstance(sha, str) or not repo.has(sha):
                    raise HttpError(422, 'Reference update failed: no object %s' % sha)
                if ref in repo.refs:
                    raise HttpError(422, 'Reference already exists')
                repo.refs[ref] = sha
                repo.version += 1
                self.forge.touch()
                return 201, {'ref': ref, 'node_id': 'REF_%s' % ref,
                             'object': {'sha': sha, 'type': _obj_type(repo, sha)},
                             'url': 'http://localhost/api/v3/repos/%s/%s/git/%s' % (repo.owner, repo.name, ref)}, {}
            if len(rest) == 3 and rest[1] == 'heads' and method == 'GET':
                oid = repo.branch_tip(rest[2])
                if oid is None:
                    raise HttpError(404, 'Not Found')
                return 200, {'ref': 'refs/heads/' + rest[2], 'node_id': 'REF_%s' % rest[2],
                             'object': {'sha': oid, 'type': _obj_type(repo, oid)}}, {}
        raise HttpError(404, 'Not Found')

    # ------------------------------------------------------------------ contents

    def _contents(self, repo, rest, method, query, body):
        path = '/'.join(rest)
        ref = query.get('ref') or repo.default_branch
        tip = _resolve_ref(repo, ref)
        if method == 'GET':
            if tip is None:
                raise HttpError(404, 'Not Found')
            files = repo.files_at(tip)
            if path in files:
                mode, oid = files[path]
                blob = repo.get(oid)
                data = blob[1] if blob else b''
                return 200, {'type': 'file', 'encoding': 'base64', 'size': len(data),
                             'name': path.split('/')[-1], 'path': path, 'sha': oid,
                             'content': b64(data),
                             'url': 'http://localhost/api/v3/repos/%s/%s/contents/%s' % (repo.owner, repo.name, path),
                             'git_url': 'http://localhost/api/v3/repos/%s/%s/git/blobs/%s' % (repo.owner, repo.name, oid),
                             'html_url': None}, {}
            tree = _tree_at_path(repo, tip, path)
            if tree is not None:
                out = []
                for mode, name, oid in gitobj.parse_tree(repo.get(tree)[1]):
                    child_path = (path + '/' + name) if path else name
                    if mode == '40000':
                        out.append({'type': 'dir', 'name': name, 'path': child_path, 'sha': oid, 'size': 0})
                    else:
                        blob = repo.get(oid)
                        out.append({'type': 'file', 'name': name, 'path': child_path, 'sha': oid,
                                    'size': len(blob[1]) if blob else 0})
                return 200, out, {}
            raise HttpError(404, 'Not Found')

        if method == 'PUT':
            data = _json_body(body)
            if 'content' not in data:
                raise HttpError(422, 'Invalid request: content is required')
            branch = data.get('branch') or repo.default_branch
            tip = repo.branch_tip(branch)
            files = repo.files_at(tip) if tip else {}
            existing = files.get(path)
            sha = data.get('sha')
            if sha is not None and (existing is None or existing[1] != sha):
                raise HttpError(409, 'sha does not match the current file contents')
            try:
                content = base64.b64decode(''.join(str(data['content']).split()))
            except Exception:
                raise HttpError(422, 'Invalid request: content is not valid base64')
            blob = repo.put('blob', content)
            files[path] = ('100644', blob)
            tree = gitobj.build_tree_from_files(repo, files)
            ident = identity(self.forge)
            message = data.get('message') or 'Update %s' % path
            commit = repo.put('commit', gitobj.build_commit(tree, [tip] if tip else [],
                                                           message.encode('utf-8'), ident, ident))
            repo.refs['refs/heads/' + branch] = commit
            repo.updated_at = self.forge.now
            self.forge.touch()
            status = 200 if existing is not None else 201
            return status, {
                'content': {'name': path.split('/')[-1], 'path': path, 'sha': blob,
                            'size': len(content), 'encoding': 'base64', 'content': b64(content),
                            'type': 'file'},
                'commit': {'sha': commit, 'message': message,
                           'author': {'name': self.forge.login, 'email': self.forge.email},
                           'committer': {'name': self.forge.login, 'email': self.forge.email}},
            }, {}

        if method == 'DELETE':
            data = _json_body(body) if body else {}
            branch = data.get('branch') or repo.default_branch
            tip = repo.branch_tip(branch)
            files = repo.files_at(tip) if tip else {}
            if path not in files:
                raise HttpError(404, 'Not Found')
            files.pop(path)
            tree = gitobj.build_tree_from_files(repo, files)
            ident = identity(self.forge)
            message = data.get('message') or 'Delete %s' % path
            commit = repo.put('commit', gitobj.build_commit(tree, [tip] if tip else [],
                                                           message.encode('utf-8'), ident, ident))
            repo.refs['refs/heads/' + branch] = commit
            repo.updated_at = self.forge.now
            self.forge.touch()
            return 200, {'commit': {'sha': commit, 'message': message}, 'content': None}, {}
        raise HttpError(405, 'Method Not Allowed')

    # ------------------------------------------------------------------ labels / issues

    def _labels(self, repo, rest, method, body):
        if rest:
            raise HttpError(404, 'Not Found')
        if method == 'GET':
            return 200, [label_json(repo, name) for name in sorted(repo.labels)], {}
        if method == 'POST':
            data = _json_body(body)
            name = data.get('name')
            if not isinstance(name, str) or not name:
                raise HttpError(422, 'Invalid request: name is required')
            if name in repo.labels:
                raise HttpError(422, 'Validation Failed: label already exists')
            repo.labels[name] = {'id': repo.next_label, 'name': name,
                                 'color': data.get('color') or 'ededed',
                                 'description': data.get('description') or ''}
            repo.next_label += 1
            self.forge.touch()
            return 201, label_json(repo, name), {}
        raise HttpError(405, 'Method Not Allowed')

    def _issues(self, repo, rest, method, query, body):
        if not rest:
            if method == 'GET':
                return self._list_issues(repo, query)
            if method == 'POST':
                return self._create_issue(repo, body)
            raise HttpError(405, 'Method Not Allowed')
        try:
            number = int(rest[0])
        except ValueError:
            raise HttpError(404, 'Not Found')
        issue = self.forge.find_issue(repo, number)
        if len(rest) == 1:
            if issue is None:
                raise HttpError(404, 'Not Found')
            if method == 'GET':
                return 200, issue_json(repo, issue), {}
            if method == 'PATCH':
                data = _json_body(body)
                if 'state' in data and data['state'] in ('open', 'closed'):
                    if data['state'] != issue['state']:
                        issue['state'] = data['state']
                        issue['closed_at'] = self.forge.now if data['state'] == 'closed' else None
                if 'title' in data and isinstance(data['title'], str):
                    issue['title'] = data['title']
                if 'body' in data and isinstance(data['body'], str):
                    issue['body'] = data['body']
                if 'labels' in data and isinstance(data['labels'], list):
                    names = [name for name in data['labels'] if isinstance(name, str)]
                    missing = [name for name in names if name not in repo.labels]
                    if missing:
                        raise HttpError(422, 'Validation Failed: label %s does not exist' % missing[0])
                    issue['labels'] = names
                issue['updated_at'] = self.forge.now
                self.forge.touch()
                return 200, issue_json(repo, issue), {}
            raise HttpError(405, 'Method Not Allowed')
        if rest[1] == 'comments' and len(rest) == 2:
            if issue is None:
                raise HttpError(404, 'Not Found')
            if method == 'GET':
                return 200, [comment_json(repo, issue, comment) for comment in issue['comments']], {}
            if method == 'POST':
                data = _json_body(body)
                comment = self.forge.new_comment(repo, issue, data.get('body') or '')
                return 201, comment_json(repo, issue, comment), {}
            raise HttpError(405, 'Method Not Allowed')
        raise HttpError(404, 'Not Found')

    def _list_issues(self, repo, query):
        state = query.get('state') or 'open'
        sort = query.get('sort') or 'created'
        direction = (query.get('direction') or 'desc').lower()
        issues = list(repo.issues)
        if state != 'all':
            issues = [issue for issue in issues if issue['state'] == state]
        if sort == 'created':
            issues.sort(key=lambda issue: issue['number'], reverse=(direction == 'desc'))
        elif sort == 'updated':
            issues.sort(key=lambda issue: (issue['updated_at'], issue['number']),
                        reverse=(direction == 'desc'))
        elif sort == 'comments':
            issues.sort(key=lambda issue: (len(issue['comments']), issue['number']),
                        reverse=(direction == 'desc'))
        else:
            issues.sort(key=lambda issue: issue['number'], reverse=(direction == 'desc'))
        page_items, _per_page, _page, _total = self._paged(issues, query)
        return 200, [issue_json(repo, issue) for issue in page_items], {}

    def _create_issue(self, repo, body):
        data = _json_body(body)
        title = data.get('title')
        if not isinstance(title, str) or not title:
            raise HttpError(422, 'Validation Failed: title is required')
        names = data.get('labels') or []
        if not isinstance(names, list):
            names = []
        names = [name for name in names if isinstance(name, str)]
        missing = [name for name in names if name not in repo.labels]
        if missing:
            raise HttpError(422, 'Validation Failed: label %s does not exist' % missing[0])
        issue = self.forge.new_issue(repo, title, data.get('body') or '', names)
        return 201, issue_json(repo, issue), {}

    # ------------------------------------------------------------------ pulls

    def _pulls(self, repo, rest, method, query, body):
        if not rest:
            if method == 'GET':
                state = query.get('state') or 'open'
                pulls = list(repo.pulls)
                if state != 'all':
                    pulls = [pull for pull in pulls if pull['state'] == state]
                pulls.sort(key=lambda pull: pull['number'])
                page_items, _per_page, _page, _total = self._paged(pulls, query)
                return 200, [pull_json(repo, pull) for pull in page_items], {}
            if method == 'POST':
                return self._create_pull(repo, body)
            raise HttpError(405, 'Method Not Allowed')
        try:
            number = int(rest[0])
        except ValueError:
            raise HttpError(404, 'Not Found')
        pull = self.forge.find_pull(repo, number)
        if len(rest) == 1:
            if pull is None:
                raise HttpError(404, 'Not Found')
            if method == 'GET':
                return 200, pull_json(repo, pull), {}
            if method == 'PATCH':
                data = _json_body(body)
                if 'state' in data and data['state'] in ('open', 'closed'):
                    pull['state'] = data['state']
                pull['updated_at'] = self.forge.now
                self.forge.touch()
                return 200, pull_json(repo, pull), {}
            raise HttpError(405, 'Method Not Allowed')
        if pull is None:
            raise HttpError(404, 'Not Found')
        if rest[1] == 'files' and len(rest) == 2 and method == 'GET':
            head_sha = repo.branch_tip(pull['head_ref']) or pull['head_sha']
            base_sha = repo.branch_tip(pull['base_ref']) or pull['base_sha']
            merge_base = repo.merge_base(base_sha, head_sha) if base_sha and head_sha else None
            files = repo.diff_files(merge_base, head_sha) if head_sha else []
            return 200, files, {}
        if rest[1] == 'merge' and len(rest) == 2 and method == 'PUT':
            data = _json_body(body) if body else {}
            if pull['merged'] or pull['state'] == 'closed':
                raise HttpError(405, 'Pull Request is not mergeable')
            if repo.pull_conflicts(pull):
                raise HttpError(405, 'Pull Request is not mergeable')
            method_name = data.get('merge_method') or 'merge'
            sha = self.forge.merge_pull(repo, pull, method_name)
            return 200, {'sha': sha, 'merged': True,
                         'message': 'Pull Request successfully merged'}, {}
        raise HttpError(404, 'Not Found')

    def _create_pull(self, repo, body):
        data = _json_body(body)
        title = data.get('title')
        head_ref = data.get('head')
        if isinstance(head_ref, str) and ':' in head_ref:
            head_ref = head_ref.split(':', 1)[1]
        base_ref = data.get('base') or repo.default_branch
        if not isinstance(head_ref, str) or not head_ref:
            raise HttpError(422, 'Validation Failed: head is required')
        if not isinstance(base_ref, str) or not base_ref:
            raise HttpError(422, 'Validation Failed: base is required')
        head_sha = repo.branch_tip(head_ref)
        if head_sha is None:
            raise HttpError(422, 'Validation Failed: head ref %s does not exist' % head_ref)
        base_sha = repo.branch_tip(base_ref)
        if base_sha is None:
            raise HttpError(422, 'Validation Failed: base ref %s does not exist' % base_ref)
        for existing in repo.pulls:
            if existing['state'] == 'open' and existing['head_ref'] == head_ref and existing['base_ref'] == base_ref:
                raise HttpError(422, 'A pull request already exists for %s.' % head_ref)
        pull = self.forge.new_pull(repo, title or head_ref, data.get('body') or '',
                                   head_ref, head_sha, base_ref, base_sha)
        return 201, pull_json(repo, pull), {}


# --------------------------------------------------------------------------- helpers

def _json_body(body):
    if not body:
        return {}
    try:
        data = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise HttpError(400, 'Problems parsing JSON')
    if not isinstance(data, dict):
        raise HttpError(400, 'Problems parsing JSON: a JSON object is required')
    return data


def _int_param(query, name, default, low, high):
    raw = query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _resolve_ref(repo, ref):
    if not ref:
        return repo.head_oid()
    tip = repo.branch_tip(ref)
    if tip:
        return tip
    if ref in repo.refs:
        return repo.refs[ref]
    if len(ref) == 40 and repo.has(ref):
        return ref
    return None


def _obj_type(repo, oid):
    obj = repo.get(oid)
    return obj[0] if obj else 'commit'


def _tree_at_path(repo, commit_oid, path):
    tree = repo.tree_at(commit_oid)
    if tree is None:
        return None
    if not path:
        return tree
    parts = path.split('/')
    current = tree
    for part in parts:
        obj = repo.get(current)
        if obj is None or obj[0] != 'tree':
            return None
        found = None
        for mode, name, oid in gitobj.parse_tree(obj[1]):
            if name == part:
                found = (mode, oid)
                break
        if found is None:
            return None
        if found[0] != '40000':
            return None
        current = found[1]
    return current


def _person(line):
    text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
    name = text
    email = ''
    date = ''
    if '<' in text and '>' in text:
        name = text[:text.index('<')].strip()
        email = text[text.index('<') + 1:text.index('>')]
        tail = text[text.index('>') + 1:].strip().split(' ')
        if tail:
            date = tail[0]
    return {'name': name, 'email': email, 'date': date}
