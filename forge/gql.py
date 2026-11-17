"""The GraphQL v4 surface: a small document parser and an executor for the schema gh and
github-mcp-server read (viewer, repository, issues, pull requests, mutations, introspection).
"""

import base64

from . import gitobj

# --------------------------------------------------------------------------- schema description

OBJECT_FIELDS = {
    'Query': ['viewer', 'repository', 'node', '__type', '__schema'],
    'Mutation': ['createIssue', 'closeIssue', 'createPullRequest', 'mergePullRequest', 'addComment'],
    'User': ['id', 'login', 'name', 'email', 'databaseId', 'avatarUrl', 'url'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner', 'defaultBranchRef',
                   'viewerPermission', 'hasIssuesEnabled', 'issue', 'issues', 'pullRequests',
                   'pullRequest', 'description', 'isPrivate', 'createdAt', 'updatedAt', 'url'],
    'Issue': ['id', 'number', 'title', 'state', 'body', 'createdAt', 'closedAt', 'updatedAt',
              'author', 'labels', 'url'],
    'PullRequest': ['id', 'number', 'title', 'body', 'state', 'merged', 'mergedAt', 'mergeable',
                    'headRefName', 'baseRefName', 'headRefOid', 'baseRefOid', 'files', 'additions',
                    'deletions', 'changedFiles', 'createdAt', 'url'],
    'IssueConnection': ['nodes', 'pageInfo', 'totalCount'],
    'PullRequestConnection': ['nodes', 'pageInfo', 'totalCount'],
    'PullRequestChangedFile': ['path', 'additions', 'deletions', 'changes'],
    'PageInfo': ['hasNextPage', 'hasPreviousPage', 'endCursor', 'startCursor'],
    'Label': ['id', 'name', 'color', 'description'],
    'Ref': ['name', 'target'],
    'Commit': ['oid', 'message'],
    'IssueComment': ['id', 'body', 'createdAt'],
    'CommentEdge': ['node', 'cursor'],
    'AddCommentPayload': ['commentEdge', 'clientMutationId'],
    'CreateIssuePayload': ['issue', 'clientMutationId'],
    'CloseIssuePayload': ['issue', 'clientMutationId'],
    'CreatePullRequestPayload': ['pullRequest', 'clientMutationId'],
    'MergePullRequestPayload': ['pullRequest', 'clientMutationId'],
    'CreateRepositoryPayload': ['repository', 'clientMutationId'],
    '__Type': ['name', 'kind', 'description', 'fields', 'enumValues', 'inputFields'],
    '__Field': ['name', 'description', 'isDeprecated', 'deprecationReason', 'type', 'args'],
    '__EnumValue': ['name', 'description', 'isDeprecated', 'deprecationReason'],
}

ENUM_VALUES = {
    'IssueState': ['OPEN', 'CLOSED'],
    'IssueStateReason': ['COMPLETED', 'NOT_PLANNED', 'REOPENED'],
    'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
    'MergeableState': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
    'PullRequestMergeMethod': ['MERGE', 'SQUASH', 'REBASE'],
    'RepositoryPermission': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ'],
    'SearchType': ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION'],
    'IssueOrderField': ['CREATED_AT', 'UPDATED_AT', 'COMMENTS'],
    'OrderDirection': ['ASC', 'DESC'],
    'RepositoryVisibility': ['PUBLIC', 'PRIVATE', 'INTERNAL'],
}

INPUT_TYPES = {
    'CreateIssueInput': ['repositoryId', 'title', 'body', 'assigneeIds', 'labelIds', 'milestoneId'],
    'CloseIssueInput': ['issueId', 'stateReason'],
    'CreatePullRequestInput': ['repositoryId', 'baseRefName', 'headRefName', 'title', 'body',
                               'draft', 'maintainerCanModify'],
    'MergePullRequestInput': ['pullRequestId', 'mergeMethod', 'commitHeadline', 'commitBody',
                              'expectedHeadOid'],
    'AddCommentInput': ['subjectId', 'body', 'clientMutationId'],
}

GLOBAL_PREFIX = {'Repository': '04', 'Issue': '05', 'PullRequest': '06', 'IssueComment': '07',
                 'User': '08'}


def encode_id(kind, number):
    return base64.b64encode(('%s:%s' % (GLOBAL_PREFIX.get(kind, '00'), number)).encode()).decode()


def decode_id(text):
    try:
        raw = base64.b64decode(text.encode()).decode()
    except Exception:
        return None, None
    if ':' not in raw:
        return None, None
    prefix, _, value = raw.partition(':')
    for kind, code in GLOBAL_PREFIX.items():
        if code == prefix:
            try:
                return kind, int(value)
            except ValueError:
                return None, None
    return None, None


# --------------------------------------------------------------------------- lexer / parser

def lex(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ' \t\r\n,':
            i += 1
            continue
        if ch == '#':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if text.startswith('...', i):
            tokens.append(('punct', '...'))
            i += 3
            continue
        if ch == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                if end < 0:
                    end = n
                tokens.append(('str', text[i + 3:end]))
                i = end + 3
                continue
            j = i + 1
            buf = []
            while j < n:
                cur = text[j]
                if cur == '\\' and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\',
                                '/': '/'}.get(nxt, nxt))
                    j += 2
                    continue
                if cur == '"':
                    break
                buf.append(cur)
                j += 1
            tokens.append(('str', ''.join(buf)))
            i = j + 1
            continue
        if ch == '$':
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            tokens.append(('var', text[i + 1:j]))
            i = j
            continue
        if ch.isdigit() or (ch == '-' and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] in '.eE+-'):
                j += 1
            raw = text[i:j]
            tokens.append(('float' if ('.' in raw or 'e' in raw or 'E' in raw) else 'int', raw))
            i = j
            continue
        if ch.isalpha() or ch == '_':
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            tokens.append(('name', text[i:j]))
            i = j
            continue
        tokens.append(('punct', ch))
        i += 1
    return tokens


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, text):
        self.tokens = lex(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def expect(self, value):
        kind, tok = self.take()
        if tok != value:
            raise ParseError('expected %s, got %s' % (value, tok))

    def parse_document(self):
        operations = []
        while self.pos < len(self.tokens):
            operations.append(self.parse_operation())
        if not operations:
            raise ParseError('empty document')
        return operations

    def parse_operation(self):
        kind, tok = self.peek()
        op_type = 'query'
        name = None
        if kind == 'name' and tok in ('query', 'mutation', 'subscription'):
            op_type = tok
            self.take()
            kind2, tok2 = self.peek()
            if kind2 == 'name':
                name = tok2
                self.take()
        variables = {}
        if self.peek() == ('punct', '('):
            self.take()
            while self.peek() != ('punct', ')'):
                var_kind, var_name = self.take()
                self.expect(':')
                self.parse_type()
                default = None
                if self.peek() == ('punct', '='):
                    self.take()
                    default = self.parse_value()
                variables[var_name] = default
            self.expect(')')
        selection = self.parse_selection_set()
        return {'type': op_type, 'name': name, 'variables': variables, 'selection': selection}

    def parse_type(self):
        kind, tok = self.take()
        if tok == '[':
            self.parse_type()
            self.expect(']')
        if self.peek() == ('punct', '!'):
            self.take()

    def parse_selection_set(self):
        self.expect('{')
        selections = []
        while self.peek() != ('punct', '}'):
            kind, tok = self.take()
            if kind == 'punct' and tok == '...':
                kind2, tok2 = self.peek()
                if kind2 == 'name' and tok2 == 'on':
                    self.take()
                    _k, type_name = self.take()
                    selections.append({'kind': 'inline', 'type': type_name,
                                       'selection': self.parse_selection_set()})
                else:
                    if kind2 == 'name':
                        self.take()
                    selections.append({'kind': 'spread', 'name': tok2})
                continue
            alias = None
            name = tok
            if self.peek() == ('punct', ':'):
                self.take()
                alias = name
                _k, name = self.take()
            args = {}
            if self.peek() == ('punct', '('):
                args = self.parse_arguments()
            while self.peek() == ('punct', '@'):
                self.take()
                self.take()
                if self.peek() == ('punct', '('):
                    self.parse_arguments()
            selection = None
            if self.peek() == ('punct', '{'):
                selection = self.parse_selection_set()
            selections.append({'kind': 'field', 'alias': alias, 'name': name,
                               'args': args, 'selection': selection})
        self.expect('}')
        return selections

    def parse_arguments(self):
        self.expect('(')
        args = {}
        while self.peek() != ('punct', ')'):
            _k, name = self.take()
            self.expect(':')
            args[name] = self.parse_value()
        self.expect(')')
        return args

    def parse_value(self):
        kind, tok = self.take()
        if kind == 'var':
            return ('var', tok)
        if kind == 'int':
            return int(tok)
        if kind == 'float':
            return float(tok)
        if kind == 'str':
            return tok
        if kind == 'punct' and tok == '[':
            items = []
            while self.peek() != ('punct', ']'):
                items.append(self.parse_value())
            self.expect(']')
            return items
        if kind == 'punct' and tok == '{':
            obj = {}
            while self.peek() != ('punct', '}'):
                _k, key = self.take()
                self.expect(':')
                obj[key] = self.parse_value()
            self.expect('}')
            return obj
        if kind == 'name':
            if tok == 'true':
                return True
            if tok == 'false':
                return False
            if tok == 'null':
                return None
            return tok
        raise ParseError('bad value %r' % (tok,))


def resolve_value(value, variables):
    if isinstance(value, tuple) and value and value[0] == 'var':
        return variables.get(value[1])
    if isinstance(value, list):
        return [resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, variables) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- executor

class Executor:
    def __init__(self, forge):
        self.forge = forge
        self.errors = []

    def fail(self, message, path=None):
        entry = {'message': message}
        if path:
            entry['path'] = path
        self.errors.append(entry)
        return None

    # ------------------------------------------------------------------ document

    def run(self, query, variables=None, operation_name=None):
        variables = variables or {}
        try:
            operations = Parser(query).parse_document()
        except ParseError as exc:
            return {'errors': [{'message': 'Syntax Error: %s' % exc}]}
        chosen = None
        if operation_name:
            for op in operations:
                if op['name'] == operation_name:
                    chosen = op
                    break
            if chosen is None:
                return {'errors': [{'message': 'Unknown operation named "%s"' % operation_name}]}
        else:
            chosen = operations[0]
        scope = dict(chosen['variables'])
        scope.update(variables)
        root = 'Mutation' if chosen['type'] == 'mutation' else 'Query'
        data = self.resolve(root, self.forge, chosen['selection'], scope)
        out = {'data': data}
        if self.errors:
            out['errors'] = self.errors
        return out

    # ------------------------------------------------------------------ selection

    def resolve(self, type_name, obj, selection, scope):
        out = {}
        for sel in selection:
            if sel['kind'] == 'spread':
                continue
            if sel['kind'] == 'inline':
                if self.matches(sel['type'], type_name):
                    out.update(self.resolve(type_name, obj, sel['selection'], scope))
                continue
            key = sel['alias'] or sel['name']
            args = {name: resolve_value(value, scope) for name, value in sel['args'].items()}
            try:
                value, child_type = self.field(type_name, obj, sel['name'], args)
            except FieldError as exc:
                value, child_type = self.fail(str(exc), [sel['name']]), None
            if sel['selection'] and value is not None:
                if isinstance(value, list):
                    out[key] = [self.resolve(child_type, item, sel['selection'], scope)
                                if item is not None else None for item in value]
                else:
                    out[key] = self.resolve(child_type, value, sel['selection'], scope)
            else:
                out[key] = value
        return out

    @staticmethod
    def matches(condition, type_name):
        if condition == type_name:
            return True
        return False

    # ------------------------------------------------------------------ fields

    def field(self, type_name, obj, name, args):
        if type_name == 'Query':
            return self.query_field(obj, name, args)
        if type_name == 'Mutation':
            return self.mutation_field(obj, name, args)
        if type_name == '__Type':
            return self.type_field(obj, name, args)
        if type_name in ('__Field', '__EnumValue'):
            if name == 'name':
                return obj.get('name'), None
            if name == 'description':
                return obj.get('description'), None
            if name == 'isDeprecated':
                return False, None
            if name == 'deprecationReason':
                return None, None
            raise FieldError('Field "%s" doesn\'t exist on type "%s"' % (name, type_name))
        handler = RESOLVERS.get(type_name)
        if handler is None:
            raise FieldError('Field "%s" doesn\'t exist on type "%s"' % (name, type_name))
        if name not in OBJECT_FIELDS.get(type_name, []):
            raise FieldError('Field "%s" doesn\'t exist on type "%s"' % (name, type_name))
        return handler(self, obj, name, args)

    # ------------------------------------------------------------------ Query

    def query_field(self, forge, name, args):
        if name == 'viewer':
            return {'login': forge.login, 'name': forge.name, 'email': forge.email,
                    'id': encode_id('User', 1000), 'databaseId': 1000}, 'User'
        if name == 'repository':
            owner = args.get('owner')
            repo_name = args.get('name')
            repo = forge.owned(owner or '', repo_name or '')
            return repo, 'Repository'
        if name == 'node':
            kind, number = decode_id(args.get('id') or '')
            if kind == 'Repository':
                for repo in forge.repos.values():
                    if repo.id == number:
                        return repo, 'Repository'
            if kind == 'Issue':
                for repo in forge.repos.values():
                    for issue in repo.issues:
                        if issue['id'] == number:
                            return (repo, issue), 'Issue'
            if kind == 'PullRequest':
                for repo in forge.repos.values():
                    for pull in repo.pulls:
                        if pull['id'] == number:
                            return (repo, pull), 'PullRequest'
            return None, 'Repository'
        if name == '__type':
            type_name = args.get('name')
            if type_name in OBJECT_FIELDS:
                return {'name': type_name, 'kind': 'OBJECT',
                        'fields': [{'name': field} for field in OBJECT_FIELDS[type_name]],
                        'enumValues': []}, '__Type'
            if type_name in ENUM_VALUES:
                return {'name': type_name, 'kind': 'ENUM', 'fields': [],
                        'enumValues': [{'name': value} for value in ENUM_VALUES[type_name]]}, '__Type'
            if type_name in INPUT_TYPES:
                return {'name': type_name, 'kind': 'INPUT_OBJECT', 'fields': [],
                        'enumValues': []}, '__Type'
            return None, '__Type'
        if name == '__schema':
            return {'name': 'schema'}, '__Type'
        raise FieldError('Field "%s" doesn\'t exist on type "Query"' % name)

    def type_field(self, obj, name, args):
        if name == 'name':
            return obj.get('name'), None
        if name == 'kind':
            return obj.get('kind'), None
        if name == 'description':
            return None, None
        if name == 'fields':
            return obj.get('fields', []), '__Field'
        if name == 'enumValues':
            return obj.get('enumValues', []), '__EnumValue'
        if name == 'inputFields':
            return [], '__Field'
        raise FieldError('Field "%s" doesn\'t exist on type "__Type"' % name)

    # ------------------------------------------------------------------ Mutation

    def mutation_field(self, forge, name, args):
        payload = args.get('input') or {}
        if name == 'createIssue':
            repo = self.repo_by_id(payload.get('repositoryId'))
            if repo is None:
                raise FieldError('Could not resolve to a Repository with the global id')
            issue = forge.new_issue(repo, payload.get('title') or '', payload.get('body') or '')
            return {'issue': (repo, issue), 'clientMutationId': payload.get('clientMutationId')}, 'CreateIssuePayload'
        if name == 'closeIssue':
            found = self.issue_by_id(payload.get('issueId'))
            if found is None:
                raise FieldError('Could not resolve to an Issue with the global id')
            repo, issue = found
            issue['state'] = 'closed'
            issue['closed_at'] = forge.now
            issue['updated_at'] = forge.now
            forge.touch()
            return {'issue': (repo, issue), 'clientMutationId': payload.get('clientMutationId')}, 'CloseIssuePayload'
        if name == 'createPullRequest':
            repo = self.repo_by_id(payload.get('repositoryId'))
            if repo is None:
                raise FieldError('Could not resolve to a Repository with the global id')
            head_ref = payload.get('headRefName')
            base_ref = payload.get('baseRefName')
            head_sha = repo.branch_tip(head_ref or '')
            base_sha = repo.branch_tip(base_ref or '')
            if head_sha is None:
                raise FieldError('Could not resolve to a Ref for headRefName %s' % head_ref)
            if base_sha is None:
                raise FieldError('Could not resolve to a Ref for baseRefName %s' % base_ref)
            pull = forge.new_pull(repo, payload.get('title') or head_ref,
                                  payload.get('body') or '', head_ref, head_sha, base_ref, base_sha)
            return {'pullRequest': (repo, pull),
                    'clientMutationId': payload.get('clientMutationId')}, 'CreatePullRequestPayload'
        if name == 'mergePullRequest':
            found = self.pull_by_id(payload.get('pullRequestId'))
            if found is None:
                raise FieldError('Could not resolve to a PullRequest with the global id')
            repo, pull = found
            if repo.pull_conflicts(pull):
                raise FieldError('Pull request is not mergeable')
            forge.merge_pull(repo, pull, payload.get('mergeMethod') or 'SQUASH')
            return {'pullRequest': (repo, pull),
                    'clientMutationId': payload.get('clientMutationId')}, 'MergePullRequestPayload'
        if name == 'addComment':
            found = self.issue_by_id(payload.get('subjectId'))
            if found is None:
                raise FieldError('Could not resolve to a node with the global id')
            repo, issue = found
            comment = forge.new_comment(repo, issue, payload.get('body') or '')
            return {'commentEdge': {'node': (repo, issue, comment), 'cursor': 'c%d' % comment['id']},
                    'clientMutationId': payload.get('clientMutationId')}, 'AddCommentPayload'
        raise FieldError('Field "%s" doesn\'t exist on type "Mutation"' % name)

    def repo_by_id(self, global_id):
        kind, number = decode_id(global_id or '')
        if kind != 'Repository':
            return None
        for repo in self.forge.repos.values():
            if repo.id == number:
                return repo
        return None

    def issue_by_id(self, global_id):
        kind, number = decode_id(global_id or '')
        if kind != 'Issue':
            return None
        for repo in self.forge.repos.values():
            for issue in repo.issues:
                if issue['id'] == number:
                    return repo, issue
        return None

    def pull_by_id(self, global_id):
        kind, number = decode_id(global_id or '')
        if kind != 'PullRequest':
            return None
        for repo in self.forge.repos.values():
            for pull in repo.pulls:
                if pull['id'] == number:
                    return repo, pull
        return None


class FieldError(Exception):
    pass


# --------------------------------------------------------------------------- resolvers

def _repo_of(obj):
    return obj


def resolve_repository(executor, repo, name, args):
    forge = executor.forge
    if name == 'id':
        return encode_id('Repository', repo.id), None
    if name == 'databaseId':
        return repo.id, None
    if name == 'name':
        return repo.name, None
    if name == 'nameWithOwner':
        return '%s/%s' % (repo.owner, repo.name), None
    if name == 'owner':
        return {'login': repo.owner, 'id': encode_id('User', 1000)}, 'User'
    if name == 'defaultBranchRef':
        return {'name': repo.default_branch,
                'target': {'oid': repo.head_oid() or ''}}, 'Ref'
    if name == 'viewerPermission':
        return 'ADMIN', None
    if name == 'hasIssuesEnabled':
        return True, None
    if name == 'description':
        return repo.description, None
    if name == 'isPrivate':
        return repo.private, None
    if name == 'createdAt':
        return repo.created_at, None
    if name == 'updatedAt':
        return repo.updated_at, None
    if name == 'url':
        return 'http://localhost/%s/%s' % (repo.owner, repo.name), None
    if name == 'issue':
        issue = forge.find_issue(repo, _as_int(args.get('number')))
        return ((repo, issue) if issue else None), 'Issue'
    if name == 'issues':
        issues = list(repo.issues)
        states = args.get('states')
        if states:
            wanted = set()
            for state in states:
                if isinstance(state, str):
                    wanted.add('open' if state.upper() == 'OPEN' else 'closed')
            issues = [issue for issue in issues if issue['state'] in wanted]
        order = args.get('orderBy') or {}
        field = (order.get('field') or 'CREATED_AT') if isinstance(order, dict) else 'CREATED_AT'
        direction = (order.get('direction') or 'DESC') if isinstance(order, dict) else 'DESC'
        if field == 'COMMENTS':
            issues.sort(key=lambda issue: (len(issue['comments']), issue['number']))
        else:
            issues.sort(key=lambda issue: issue['number'])
        if str(direction).upper() == 'DESC':
            issues.reverse()
        return _connection(issues, args, lambda issue: (repo, issue), 'Issue'), 'IssueConnection'
    if name == 'pullRequests':
        pulls = sorted(repo.pulls, key=lambda pull: pull['number'])
        return _connection(pulls, args, lambda pull: (repo, pull), 'PullRequest'), 'PullRequestConnection'
    if name == 'pullRequest':
        pull = forge.find_pull(repo, _as_int(args.get('number')))
        return ((repo, pull) if pull else None), 'PullRequest'
    raise FieldError('Field "%s" doesn\'t exist on type "Repository"' % name)


def _connection(items, args, wrap, child):
    first = args.get('first')
    if not isinstance(first, int) or first < 0:
        first = len(items)
    after = args.get('after')
    start = 0
    if isinstance(after, str) and after.startswith('cursor:'):
        try:
            start = int(after.split(':', 1)[1])
        except ValueError:
            start = 0
    window = items[start:start + first]
    end = start + len(window)
    page_info = {'hasNextPage': end < len(items), 'hasPreviousPage': start > 0,
                 'startCursor': 'cursor:%d' % start if window else None,
                 'endCursor': 'cursor:%d' % end if window else None}
    return {'nodes': [wrap(item) for item in window], 'pageInfo': page_info,
            'totalCount': len(items), '_child': child}


def resolve_issue(executor, obj, name, args):
    repo, issue = obj
    if name == 'id':
        return encode_id('Issue', issue['id']), None
    if name == 'number':
        return issue['number'], None
    if name == 'title':
        return issue['title'], None
    if name == 'state':
        return 'OPEN' if issue['state'] == 'open' else 'CLOSED', None
    if name == 'body':
        return issue['body'], None
    if name == 'createdAt':
        return issue['created_at'], None
    if name == 'closedAt':
        return issue['closed_at'], None
    if name == 'updatedAt':
        return issue['updated_at'], None
    if name == 'url':
        return 'http://localhost/%s/%s/issues/%d' % (repo.owner, repo.name, issue['number']), None
    if name == 'author':
        return {'login': repo.owner}, 'User'
    if name == 'labels':
        return [{'name': label, 'color': 'ededed', 'id': encode_id('Label', 1)}
                for label in issue['labels']], 'Label'
    raise FieldError('Field "%s" doesn\'t exist on type "Issue"' % name)


def resolve_pull(executor, obj, name, args):
    repo, pull = obj
    head_sha = repo.branch_tip(pull['head_ref']) or pull['head_sha']
    base_sha = repo.branch_tip(pull['base_ref']) or pull['base_sha']
    if name == 'id':
        return encode_id('PullRequest', pull['id']), None
    if name == 'number':
        return pull['number'], None
    if name == 'title':
        return pull['title'], None
    if name == 'body':
        return pull['body'], None
    if name == 'state':
        if pull['merged']:
            return 'MERGED', None
        return 'OPEN' if pull['state'] == 'open' else 'CLOSED', None
    if name == 'merged':
        return bool(pull['merged']), None
    if name == 'mergedAt':
        return pull['merged_at'], None
    if name == 'mergeable':
        return 'CONFLICTING' if repo.pull_conflicts(pull) else 'MERGEABLE', None
    if name == 'headRefName':
        return pull['head_ref'], None
    if name == 'baseRefName':
        return pull['base_ref'], None
    if name == 'headRefOid':
        return head_sha, None
    if name == 'baseRefOid':
        return base_sha, None
    if name == 'files':
        merge_base = repo.merge_base(base_sha, head_sha) if base_sha and head_sha else None
        files = repo.diff_files(merge_base, head_sha) if head_sha else []
        return _connection(files, args, lambda entry: entry, 'PullRequestChangedFile'), 'PullRequestConnection'
    if name == 'additions':
        return sum(entry['additions'] for entry in repo.diff_files(
            repo.merge_base(base_sha, head_sha), head_sha)), None
    if name == 'deletions':
        return sum(entry['deletions'] for entry in repo.diff_files(
            repo.merge_base(base_sha, head_sha), head_sha)), None
    if name == 'changedFiles':
        return len(repo.diff_files(repo.merge_base(base_sha, head_sha), head_sha)), None
    if name == 'createdAt':
        return pull['created_at'], None
    if name == 'url':
        return 'http://localhost/%s/%s/pull/%d' % (repo.owner, repo.name, pull['number']), None
    raise FieldError('Field "%s" doesn\'t exist on type "PullRequest"' % name)


def resolve_user(executor, obj, name, args):
    if name == 'id':
        return encode_id('User', 1000), None
    if name == 'login':
        return obj.get('login'), None
    if name == 'name':
        return obj.get('name'), None
    if name == 'email':
        return obj.get('email'), None
    if name == 'databaseId':
        return 1000, None
    if name == 'avatarUrl':
        return 'http://localhost/avatar', None
    if name == 'url':
        return 'http://localhost/%s' % obj.get('login'), None
    raise FieldError('Field "%s" doesn\'t exist on type "User"' % name)


def resolve_ref(executor, obj, name, args):
    if name == 'name':
        return obj.get('name'), None
    if name == 'target':
        return {'oid': obj.get('oid'), 'message': ''}, 'Commit'
    raise FieldError('Field "%s" doesn\'t exist on type "Ref"' % name)


def resolve_commit(executor, obj, name, args):
    if name == 'oid':
        return obj.get('oid'), None
    if name == 'message':
        return obj.get('message') or '', None
    raise FieldError('Field "%s" doesn\'t exist on type "Commit"' % name)


def resolve_connection(executor, obj, name, args):
    if name == 'nodes':
        return obj.get('nodes', []), obj.get('_child')
    if name == 'pageInfo':
        return obj.get('pageInfo', {}), 'PageInfo'
    if name == 'totalCount':
        return obj.get('totalCount', 0), None
    raise FieldError('Field "%s" doesn\'t exist on type "Connection"' % name)


def resolve_page_info(executor, obj, name, args):
    if name in ('hasNextPage', 'hasPreviousPage'):
        return bool(obj.get(name)), None
    if name in ('endCursor', 'startCursor'):
        return obj.get(name), None
    raise FieldError('Field "%s" doesn\'t exist on type "PageInfo"' % name)


def resolve_changed_file(executor, obj, name, args):
    if name == 'path':
        return obj.get('filename'), None
    if name in ('additions', 'deletions', 'changes'):
        return obj.get(name, 0), None
    raise FieldError('Field "%s" doesn\'t exist on type "PullRequestChangedFile"' % name)


def resolve_label(executor, obj, name, args):
    if name == 'id':
        return encode_id('Label', 1), None
    if name in ('name', 'color', 'description'):
        return obj.get(name), None
    raise FieldError('Field "%s" doesn\'t exist on type "Label"' % name)


def resolve_comment_edge(executor, obj, name, args):
    if name == 'node':
        return obj.get('node'), 'IssueComment'
    if name == 'cursor':
        return obj.get('cursor'), None
    raise FieldError('Field "%s" doesn\'t exist on type "CommentEdge"' % name)


def resolve_comment(executor, obj, name, args):
    repo, issue, comment = obj
    if name == 'id':
        return encode_id('IssueComment', comment['id']), None
    if name == 'body':
        return comment['body'], None
    if name == 'createdAt':
        return comment['created_at'], None
    raise FieldError('Field "%s" doesn\'t exist on type "IssueComment"' % name)


def _payload_resolver(kind):
    def resolve(executor, obj, name, args):
        if name == 'clientMutationId':
            return obj.get('clientMutationId'), None
        for key, value in obj.items():
            if key == 'clientMutationId':
                continue
            if key == name:
                if key == 'commentEdge':
                    return value, 'CommentEdge'
                if key == 'issue':
                    return value, 'Issue'
                if key == 'pullRequest':
                    return value, 'PullRequest'
                if key == 'repository':
                    return value, 'Repository'
        raise FieldError('Field "%s" doesn\'t exist on type "%s"' % (name, kind))
    return resolve


def _as_int(value):
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


RESOLVERS = {
    'Repository': resolve_repository,
    'Issue': resolve_issue,
    'PullRequest': resolve_pull,
    'User': resolve_user,
    'Ref': resolve_ref,
    'Commit': resolve_commit,
    'IssueConnection': resolve_connection,
    'PullRequestConnection': resolve_connection,
    'PageInfo': resolve_page_info,
    'PullRequestChangedFile': resolve_changed_file,
    'Label': resolve_label,
    'CommentEdge': resolve_comment_edge,
    'IssueComment': resolve_comment,
    'CreateIssuePayload': _payload_resolver('CreateIssuePayload'),
    'CloseIssuePayload': _payload_resolver('CloseIssuePayload'),
    'CreatePullRequestPayload': _payload_resolver('CreatePullRequestPayload'),
    'MergePullRequestPayload': _payload_resolver('MergePullRequestPayload'),
    'AddCommentPayload': _payload_resolver('AddCommentPayload'),
    'CreateRepositoryPayload': _payload_resolver('CreateRepositoryPayload'),
}
