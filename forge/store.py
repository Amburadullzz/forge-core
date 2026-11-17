"""The forge's store: repositories, git objects and refs, issues, labels, comments and pulls.

One process, one store, its own state. Everything is held in memory and mirrored to a data
directory (a JSON snapshot plus loose object files) by a background flusher, so the forge serves
from its own store and survives a restart of the same process tree.
"""

import json
import os
import threading
import time
import zlib

from . import gitobj


class Repo:
    """One repository: its metadata, its git object store and its refs."""

    __slots__ = ('id', 'owner', 'name', 'private', 'description', 'default_branch',
                 'created_at', 'updated_at', 'pushed_at', 'objects', 'refs',
                 'labels', 'issues', 'pulls', 'next_issue', 'next_pull', 'next_label',
                 'dirty', 'version', 'commit_cache', 'fetch_cache', 'on_new_object')

    def __init__(self, repo_id, owner, name, private=False, description='', default_branch='main', created_at=''):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ''
        self.default_branch = default_branch or 'main'
        self.created_at = created_at
        self.updated_at = created_at
        self.pushed_at = created_at
        self.objects = {}
        self.refs = {}
        self.labels = {}
        self.issues = []
        self.pulls = []
        self.next_issue = 1
        self.next_pull = 1
        self.next_label = 1
        self.dirty = False
        # A monotonic counter over every change to the refs or the object store: a cached
        # packfile answer is only valid for the version it was built at.
        self.version = 0
        self.commit_cache = {}
        self.fetch_cache = {}
        self.on_new_object = None

    # ---------------------------------------------------------------- git object store

    def put(self, otype, data):
        oid = gitobj.obj_oid(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
            self.dirty = True
            self.version += 1
            if self.on_new_object is not None:
                self.on_new_object(otype, data)
        return oid

    def get(self, oid):
        return self.objects.get(oid)

    def has(self, oid):
        return oid in self.objects

    def head_oid(self):
        """The tip of the default branch, or None for an unborn repository."""
        return self.refs.get('refs/heads/' + self.default_branch)

    def branch_tip(self, branch):
        return self.refs.get('refs/heads/' + branch)

    def files_at(self, commit_oid):
        """{path: (mode, oid)} of a commit's tree."""
        tree = gitobj.commit_tree(self, commit_oid)
        if tree is None:
            return {}
        return gitobj.flatten_tree(self, tree)

    def tree_at(self, commit_oid):
        return gitobj.commit_tree(self, commit_oid)

    def commit_view(self, commit_oid):
        cached = self.commit_cache.get(commit_oid)
        if cached is None:
            obj = self.objects.get(commit_oid)
            if obj is None or obj[0] != 'commit':
                return None
            cached = gitobj.parse_commit(obj[1])
            self.commit_cache[commit_oid] = cached
        return cached

    def commit_parents(self, commit_oid):
        view = self.commit_view(commit_oid)
        return view['parents'] if view else []

    def is_ancestor(self, ancestor, descendant):
        """True when `ancestor` is reachable from `descendant` by first-parent-free walking."""
        if ancestor == descendant:
            return True
        if ancestor is None:
            return True
        seen = set()
        stack = [descendant]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if oid == ancestor:
                return True
            for parent in self.commit_parents(oid):
                if parent not in seen:
                    stack.append(parent)
        return False

    def closure(self, tips, stop_at=None):
        """Every object reachable from `tips` (commit -> tree + parents, tree -> entries, tag -> target)."""
        seen = set()
        stack = [oid for oid in tips if oid]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            obj = self.objects.get(oid)
            if obj is None:
                continue
            seen.add(oid)
            otype, data = obj
            if otype == 'commit':
                commit = gitobj.parse_commit(data)
                if commit['tree']:
                    stack.append(commit['tree'])
                for parent in commit['parents']:
                    if stop_at is not None and oid in stop_at:
                        continue
                    stack.append(parent)
            elif otype == 'tree':
                for mode, _name, entry_oid in gitobj.parse_tree(data):
                    if mode == '160000':
                        continue
                    stack.append(entry_oid)
            elif otype == 'tag':
                target = gitobj.parse_tag(data)['object']
                if target:
                    stack.append(target)
        return seen

    def shallow_closure(self, tips, shallow):
        """Closure that does not walk past the client's shallow boundary commits."""
        seen = set()
        stack = [oid for oid in tips if oid]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            obj = self.objects.get(oid)
            if obj is None:
                continue
            seen.add(oid)
            otype, data = obj
            if otype == 'commit':
                commit = gitobj.parse_commit(data)
                if commit['tree']:
                    stack.append(commit['tree'])
                if oid not in shallow:
                    for parent in commit['parents']:
                        stack.append(parent)
            elif otype == 'tree':
                for mode, _name, entry_oid in gitobj.parse_tree(data):
                    if mode == '160000':
                        continue
                    stack.append(entry_oid)
            elif otype == 'tag':
                target = gitobj.parse_tag(data)['object']
                if target:
                    stack.append(target)
        return seen

    def walk_commits(self, tip, limit=500):
        """Commits reachable from `tip`, newest first (BFS order from the tip)."""
        if not tip:
            return []
        out = []
        seen = set()
        queue = [tip]
        while queue and len(out) < limit:
            oid = queue.pop(0)
            if oid in seen:
                continue
            seen.add(oid)
            obj = self.objects.get(oid)
            if obj is None or obj[0] != 'commit':
                continue
            out.append(oid)
            for parent in gitobj.parse_commit(obj[1])['parents']:
                if parent not in seen:
                    queue.append(parent)
        return out

    def merge_base(self, a, b):
        """A best-effort merge base: the first commit of `a`'s history that is in `b`'s."""
        if not a or not b:
            return None
        ancestors_b = set(self.walk_commits(b, limit=5000))
        queue = [a]
        seen = set()
        while queue:
            oid = queue.pop(0)
            if oid in seen:
                continue
            seen.add(oid)
            if oid in ancestors_b:
                return oid
            for parent in self.commit_parents(oid):
                queue.append(parent)
        return None

    def diff_files(self, base_commit, head_commit):
        """[{'filename','status','additions','deletions','changes','sha'}] between two commits."""
        before = self.files_at(base_commit) if base_commit else {}
        after = self.files_at(head_commit) if head_commit else {}
        out = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path)
            new = after.get(path)
            if old is not None and new is not None and old[1] == new[1]:
                continue
            additions = 0
            deletions = 0
            if new is not None:
                blob = self.get(new[1])
                new_lines = blob[1].split(b'\n') if blob else []
                if old is None:
                    additions = max(1, len([line for line in new_lines if line]))
                else:
                    old_blob = self.get(old[1])
                    old_lines = old_blob[1].split(b'\n') if old_blob else []
                    from collections import Counter
                    counts = Counter(old_lines)
                    for line in new_lines:
                        if counts.get(line):
                            counts[line] -= 1
                        else:
                            additions += 1
                    counts = Counter(new_lines)
                    for line in old_lines:
                        if counts.get(line):
                            counts[line] -= 1
                        else:
                            deletions += 1
            else:
                old_blob = self.get(old[1])
                deletions = max(1, len([line for line in old_blob[1].split(b'\n') if line])) if old_blob else 1
            if additions == 0 and deletions == 0:
                additions = 1
            status = 'added' if old is None else 'removed' if new is None else 'modified'
            out.append({'filename': path, 'status': status,
                        'additions': additions, 'deletions': deletions,
                        'changes': additions + deletions,
                        'sha': new[1] if new is not None else old[1]})
        return out

    def changed_paths(self, base_commit, head_commit):
        return [entry['filename'] for entry in self.diff_files(base_commit, head_commit)]

    def pull_conflicts(self, pull):
        """True when the base moved under the pull and both sides touched the same paths."""
        head_sha = self.branch_tip(pull['head_ref']) or pull['head_sha']
        base_sha = self.branch_tip(pull['base_ref']) or pull['base_sha']
        if not head_sha or not base_sha:
            return True
        if self.is_ancestor(head_sha, base_sha):
            return False
        merge_base = self.merge_base(base_sha, head_sha)
        if merge_base is None:
            return False
        head_paths = set(self.changed_paths(merge_base, head_sha))
        base_paths = set(self.changed_paths(merge_base, base_sha))
        return bool(head_paths & base_paths)


class Forge:
    """The whole forge: one authenticated login, its repositories, and the shared counters."""

    def __init__(self, login, name, email, now='2026-01-01T00:00:00Z', data_dir=None):
        self.login = login
        self.name = name
        self.email = email
        self.now = now
        self.repos = {}
        # Objects are content addressed, so one pool serves every repository: a push's objects
        # are stored once, and the snapshot never has to list them.
        self.objects = {}
        self.next_repo_id = 1
        self.next_issue_id = 1
        self.next_pull_id = 1
        self.next_comment_id = 1
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._pending = []
        self.last_touch = 0.0
        self._flusher = None
        if data_dir:
            try:
                os.makedirs(os.path.join(data_dir, 'objects'), exist_ok=True)
            except OSError:
                self.data_dir = None
        if self.data_dir:
            self._load()

    # ---------------------------------------------------------------- repositories

    def key(self, owner, name):
        return (owner.lower(), name.lower())

    def get_repo(self, owner, name):
        return self.repos.get(self.key(owner, name))

    def owned(self, owner, name):
        """A repository, only when the token's own login owns it (else None, which is a 404)."""
        if owner.lower() != self.login.lower():
            return None
        return self.repos.get(self.key(owner, name))

    def create_repo(self, owner, name, private=False, description=''):
        repo = Repo(self.next_repo_id, owner, name, private, description,
                    'main', self.now)
        repo.objects = self.objects
        repo.on_new_object = self.defer_object
        self.next_repo_id += 1
        self.repos[self.key(owner, name)] = repo
        self.touch()
        return repo

    def delete_repo(self, repo):
        self.repos.pop(self.key(repo.owner, repo.name), None)
        self.touch()

    def repos_of(self, login):
        return [repo for repo in self.repos.values() if repo.owner.lower() == login.lower()]

    # ---------------------------------------------------------------- issues

    def new_issue(self, repo, title, body, labels=None):
        issue = {
            'id': self.next_issue_id,
            'number': repo.next_issue,
            'title': title or '',
            'body': body or '',
            'state': 'open',
            'labels': list(labels or []),
            'created_at': self.now,
            'updated_at': self.now,
            'closed_at': None,
            'comments': [],
        }
        self.next_issue_id += 1
        repo.next_issue += 1
        repo.issues.append(issue)
        self.touch()
        return issue

    def find_issue(self, repo, number):
        for issue in repo.issues:
            if issue['number'] == number:
                return issue
        return None

    def new_comment(self, repo, issue, body):
        comment = {
            'id': self.next_comment_id,
            'body': body or '',
            'created_at': self.now,
            'user': self.login,
        }
        self.next_comment_id += 1
        issue['comments'].append(comment)
        issue['updated_at'] = self.now
        self.touch()
        return comment

    # ---------------------------------------------------------------- pulls

    def new_pull(self, repo, title, body, head_ref, head_sha, base_ref, base_sha):
        pull = {
            'id': self.next_pull_id,
            'number': repo.next_pull,
            'title': title or '',
            'body': body or '',
            'state': 'open',
            'head_ref': head_ref,
            'head_sha': head_sha,
            'base_ref': base_ref,
            'base_sha': base_sha,
            'merged': False,
            'merged_at': None,
            'merge_commit_sha': None,
            'created_at': self.now,
            'updated_at': self.now,
        }
        self.next_pull_id += 1
        repo.next_pull += 1
        repo.pulls.append(pull)
        self.touch()
        return pull

    def find_pull(self, repo, number):
        for pull in repo.pulls:
            if pull['number'] == number:
                return pull
        return None

    def pull_conflicts(self, repo, pull):
        return repo.pull_conflicts(pull)

    def merge_pull(self, repo, pull, method='squash'):
        """Squash-merge a pull request onto its base branch; answers the merge commit sha."""
        head_sha = repo.branch_tip(pull['head_ref']) or pull['head_sha']
        base_sha = repo.branch_tip(pull['base_ref'])
        files = repo.files_at(base_sha) if base_sha else {}
        for path in repo.changed_paths(repo.merge_base(base_sha, head_sha), head_sha):
            head_files = repo.files_at(head_sha)
            if path in head_files:
                files[path] = head_files[path]
            else:
                files.pop(path, None)
        tree = gitobj.build_tree_from_files(repo, files)
        message = '%s (#%d)' % (pull['title'], pull['number'])
        if pull['body']:
            message += '\n\n' + pull['body']
        identity = '%s <%s> %s' % (self.login, self.email, self.now)
        data = gitobj.build_commit(tree, [base_sha] if base_sha else [], message,
                                   identity, identity)
        commit_oid = repo.put('commit', data)
        repo.refs['refs/heads/' + pull['base_ref']] = commit_oid
        repo.pushed_at = self.now
        repo.updated_at = self.now
        pull['merged'] = True
        pull['state'] = 'closed'
        pull['merged_at'] = self.now
        pull['merge_commit_sha'] = commit_oid
        pull['updated_at'] = self.now
        pull['base_sha'] = commit_oid
        self.touch()
        return commit_oid

    # ---------------------------------------------------------------- persistence

    def touch(self):
        self.last_touch = time.monotonic()
        self._dirty.set()

    def start_flusher(self):
        if not self.data_dir or self._flusher is not None:
            return
        self._flusher = threading.Thread(target=self._flush_loop, name='forge-flush', daemon=True)
        self._flusher.start()

    def _flush_loop(self):
        """Drain queued objects and snapshot the metadata once the requests go quiet, so a
        burst of workload traffic never waits behind the store's own bookkeeping."""
        while not self._stop.is_set():
            if self._pending:
                batch = self._pending[:400]
                self._pending = self._pending[400:]
                self._append_objects(batch)
                continue
            if self._dirty.wait(0.3):
                if time.monotonic() - self.last_touch < 1.0:
                    time.sleep(0.15)
                    continue
                self._dirty.clear()
                try:
                    self.flush()
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        if self._pending:
            batch, self._pending = self._pending, []
            self._append_objects(batch)
        if self.data_dir:
            try:
                self.flush()
            except Exception:
                pass

    def flush(self):
        if not self.data_dir:
            return
        with self.lock:
            snapshot = {'login': self.login, 'now': self.now,
                        'next_repo_id': self.next_repo_id,
                        'next_issue_id': self.next_issue_id,
                        'next_pull_id': self.next_pull_id,
                        'next_comment_id': self.next_comment_id,
                        'repos': []}
            for repo in self.repos.values():
                snapshot['repos'].append({
                    'id': repo.id, 'owner': repo.owner, 'name': repo.name,
                    'private': repo.private, 'description': repo.description,
                    'default_branch': repo.default_branch,
                    'created_at': repo.created_at, 'updated_at': repo.updated_at,
                    'pushed_at': repo.pushed_at, 'refs': dict(repo.refs),
                    'labels': repo.labels, 'issues': repo.issues, 'pulls': repo.pulls,
                    'next_issue': repo.next_issue, 'next_pull': repo.next_pull,
                    'next_label': repo.next_label,
                })
        payload = json.dumps(snapshot)
        tmp = os.path.join(self.data_dir, 'db.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as handle:
                handle.write(payload)
            os.replace(tmp, os.path.join(self.data_dir, 'db.json'))
        except OSError:
            pass

    def defer_object(self, otype, data):
        """Queue an object for the log; the flusher writes it off the request path."""
        if self.data_dir:
            self._pending.append((otype, data))

    def _append_objects(self, batch):
        """Append objects to one log file: a record is oid hex, type, length, zlib bytes."""
        if not self.data_dir or not batch:
            return
        parts = []
        for otype, data in batch:
            oid = gitobj.obj_oid(otype, data)
            packed = zlib.compress(data, 1)
            parts.append(oid.encode('ascii'))
            parts.append(bytes([gitobj.TYPE_CODES[otype]]))
            parts.append(len(packed).to_bytes(4, 'big'))
            parts.append(packed)
        try:
            with open(os.path.join(self.data_dir, 'objects.log'), 'ab') as handle:
                handle.write(b''.join(parts))
        except OSError:
            pass

    def _load(self):
        path = os.path.join(self.data_dir, 'db.json')
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError):
            return
        self.next_repo_id = snapshot.get('next_repo_id', 1)
        self.next_issue_id = snapshot.get('next_issue_id', 1)
        self.next_pull_id = snapshot.get('next_pull_id', 1)
        self.next_comment_id = snapshot.get('next_comment_id', 1)
        for record in snapshot.get('repos', []):
            repo = Repo(record['id'], record['owner'], record['name'], record['private'],
                        record['description'], record['default_branch'], record['created_at'])
            repo.updated_at = record.get('updated_at', repo.created_at)
            repo.pushed_at = record.get('pushed_at', repo.created_at)
            repo.refs = dict(record.get('refs', {}))
            repo.labels = record.get('labels', {})
            repo.issues = record.get('issues', [])
            repo.pulls = record.get('pulls', [])
            repo.next_issue = record.get('next_issue', 1)
            repo.next_pull = record.get('next_pull', 1)
            repo.next_label = record.get('next_label', 1)
            repo.objects = self.objects
            repo.on_new_object = self.defer_object
            self.repos[self.key(repo.owner, repo.name)] = repo
        self._load_objects()

    def _load_objects(self):
        """Read the object log back into the shared pool."""
        path = os.path.join(self.data_dir, 'objects.log')
        try:
            with open(path, 'rb') as handle:
                blob = handle.read()
        except OSError:
            return
        offset = 0
        total = len(blob)
        while offset + 45 <= total:
            oid = blob[offset:offset + 40].decode('ascii', 'replace')
            code = blob[offset + 40]
            length = int.from_bytes(blob[offset + 41:offset + 45], 'big')
            start = offset + 45
            end = start + length
            if end > total:
                break
            otype = gitobj.CODE_TYPES.get(code)
            if otype is not None:
                try:
                    self.objects[oid] = (otype, zlib.decompress(blob[start:end]))
                except zlib.error:
                    pass
            offset = end

