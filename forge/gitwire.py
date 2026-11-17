"""git smart HTTP: pkt-line framing, protocol v0/v2 advertisements, ls-refs, fetch and
receive-pack, spoken to the wire git itself speaks — no git binary anywhere.
"""

import zlib

from . import gitobj

ZERO_OID = '0' * 40
FLUSH = b'0000'
DELIM = b'0001'
MAX_PAYLOAD = 65515

AGENT = 'forge/1.0'

UPLOAD_CAPS = ('multi_ack_detailed side-band-64k thin-pack ofs-delta shallow '
               'no-progress allow-tip-sha1-in-want agent=' + AGENT)
RECEIVE_CAPS = 'report-status delete-refs side-band-64k ofs-delta agent=' + AGENT


def pkt(payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


def pkt_lines(lines):
    out = []
    for line in lines:
        if line == 'flush':
            out.append(FLUSH)
        elif line == 'delim':
            out.append(DELIM)
        else:
            out.append(pkt(line))
    return b''.join(out)


def parse_pkts(body):
    """[(kind, payload)] plus the offset just past the last complete packet."""
    out = []
    i = 0
    n = len(body)
    while i + 4 <= n:
        header = body[i:i + 4]
        try:
            length = int(header, 16)
        except ValueError:
            break
        if length == 0:
            out.append(('flush', b''))
            i += 4
            continue
        if length == 1:
            out.append(('delim', b''))
            i += 4
            continue
        if length == 2:
            out.append(('response-end', b''))
            i += 4
            continue
        if length < 4 or i + length > n:
            break
        out.append(('data', body[i + 4:i + length]))
        i += length
    return out, i


def sideband(chunks, band=1):
    """Frame bytes as band-`band` sideband packets (64k payloads)."""
    out = []
    for start in range(0, len(chunks), MAX_PAYLOAD):
        out.append(pkt(bytes([band]) + chunks[start:start + MAX_PAYLOAD]))
    return b''.join(out)


# --------------------------------------------------------------------------- advertisements

def _peeled(repo, oid):
    obj = repo.get(oid)
    if obj is not None and obj[0] == 'tag':
        target = gitobj.parse_tag(obj[1])['object']
        if target and repo.has(target):
            return target
    return None


def advertisement_v0(repo, service):
    """v0 ref advertisement with the smart-HTTP `# service=` preamble."""
    caps = RECEIVE_CAPS if service == 'git-receive-pack' else UPLOAD_CAPS
    if service == 'git-upload-pack':
        caps = caps + ' symref=HEAD:refs/heads/' + repo.default_branch
    head = repo.head_oid()
    parts = [pkt('# service=%s\n' % service), FLUSH]
    refs = [('HEAD', head)]
    for name in sorted(repo.refs):
        if name.startswith('refs/heads/'):
            refs.append((name, repo.refs[name]))
    for name in sorted(repo.refs):
        if not name.startswith('refs/heads/'):
            refs.append((name, repo.refs[name]))
    emitted = 0
    for name, oid in refs:
        if oid is None:
            continue
        if emitted == 0:
            parts.append(pkt('%s %s\0%s\n' % (oid, name, caps)))
        else:
            parts.append(pkt('%s %s\n' % (oid, name)))
        emitted += 1
        if name.startswith('refs/tags/'):
            peeled = _peeled(repo, oid)
            if peeled:
                parts.append(pkt('%s %s^{}\n' % (peeled, name)))
    if emitted == 0:
        parts.append(pkt('%s capabilities^{}\0%s\n' % (ZERO_OID, caps)))
    parts.append(FLUSH)
    return b''.join(parts)


def advertisement_v2(repo):
    """Protocol v2 capability advertisement (`version 2` first, no service preamble)."""
    return pkt_lines([
        'version 2\n',
        'agent=%s\n' % AGENT,
        'object-format=sha1\n',
        'ls-refs=unborn\n',
        'fetch=shallow\n',
        'server-option\n',
        'flush',
    ])


# --------------------------------------------------------------------------- ls-refs

def ls_refs(repo, args):
    prefixes = [value for key, value in args if key == 'ref-prefix']
    peel = any(key == 'peel' for key, _ in args)
    symrefs = any(key == 'symrefs' for key, _ in args)
    unborn = any(key == 'unborn' for key, _ in args)

    def wanted(name):
        return not prefixes or any(name.startswith(prefix) for prefix in prefixes)

    out = []
    head = repo.head_oid()
    if wanted('HEAD'):
        if head:
            line = '%s HEAD' % head
            if symrefs:
                line += ' symref-target:refs/heads/' + repo.default_branch
            out.append(pkt(line + '\n'))
        elif unborn:
            out.append(pkt('unborn HEAD symref-target:refs/heads/%s\n' % repo.default_branch))
    for name in sorted(repo.refs):
        if not wanted(name):
            continue
        oid = repo.refs[name]
        line = '%s %s' % (oid, name)
        if peel and name.startswith('refs/tags/'):
            peeled = _peeled(repo, oid)
            if peeled:
                line += ' peeled:' + peeled
        out.append(pkt(line + '\n'))
    out.append(FLUSH)
    return b''.join(out)


# --------------------------------------------------------------------------- fetch

def _depth_commits(repo, wants, depth):
    """(commits within `depth` of the wants, the commits at the boundary)."""
    included = []
    boundary = []
    seen = set()
    queue = [(want, 1) for want in wants]
    while queue:
        oid, level = queue.pop(0)
        if oid in seen:
            continue
        obj = repo.get(oid)
        if obj is None:
            continue
        if obj[0] == 'tag':
            queue.append((gitobj.parse_tag(obj[1])['object'], level))
            included.append(oid)
            continue
        if obj[0] != 'commit':
            continue
        seen.add(oid)
        included.append(oid)
        parents = gitobj.parse_commit(obj[1])['parents']
        if level < depth:
            for parent in parents:
                queue.append((parent, level + 1))
        elif parents:
            boundary.append(oid)
    return included, boundary


def select_objects(repo, wants, haves, shallow, depth):
    """(objects to send, new shallow boundary, unshallowed commits)."""
    if depth is not None:
        included, boundary = _depth_commits(repo, wants, depth)
        wanted_objects = set(included)
        for oid in included:
            obj = repo.get(oid)
            if obj is None:
                continue
            if obj[0] == 'commit':
                tree = gitobj.parse_commit(obj[1])['tree']
                if tree:
                    wanted_objects |= repo.closure([tree])
            elif obj[0] == 'tag':
                wanted_objects |= repo.closure([oid])
        new_shallow = [oid for oid in boundary if oid not in shallow]
        unshallow = [oid for oid in shallow if oid in set(included)]
    else:
        wanted_objects = repo.closure(wants)
        new_shallow = []
        unshallow = []
    held = repo.shallow_closure(haves, set(shallow)) if haves else set()
    send = [oid for oid in wanted_objects if oid not in held]
    return send, new_shallow, unshallow


def _pack_for(repo, oids):
    items = []
    for oid in oids:
        obj = repo.get(oid)
        if obj is not None:
            items.append(obj)
    return gitobj.write_pack(items)


def fetch_v2(repo, args):
    wants = [value for key, value in args if key == 'want']
    haves = [value for key, value in args if key == 'have']
    shallow = [value for key, value in args if key == 'shallow']
    depth = None
    for key, value in args:
        if key == 'deepen':
            try:
                depth = int(value)
            except ValueError:
                depth = 1
    if depth is not None and depth <= 0:
        depth = None
    done = any(key == 'done' for key, _ in args)
    if haves and not done:
        # The negotiation round: acknowledge what we hold, and let the client send `done`.
        parts = [pkt('acknowledgments\n')]
        for oid in haves:
            if repo.has(oid):
                parts.append(pkt('ACK %s\n' % oid))
        parts.append(FLUSH)
        return b''.join(parts)
    key = (repo.version, tuple(wants), tuple(haves), tuple(shallow), depth)
    cached = repo.fetch_cache.get(key)
    if cached is not None:
        return cached
    oids, new_shallow, unshallow = select_objects(repo, wants, haves, shallow, depth)
    parts = []
    if new_shallow or unshallow:
        parts.append(pkt('shallow-info\n'))
        for oid in new_shallow:
            parts.append(pkt('shallow %s\n' % oid))
        for oid in unshallow:
            parts.append(pkt('unshallow %s\n' % oid))
        parts.append(FLUSH)
    pack = _pack_for(repo, oids)
    parts.append(pkt('packfile\n'))
    parts.append(sideband(pack, 1))
    parts.append(FLUSH)
    answer = b''.join(parts)
    if len(repo.fetch_cache) > 64:
        repo.fetch_cache.clear()
    repo.fetch_cache[key] = answer
    return answer


def fetch_v0(repo, args):
    wants = [value for key, value in args if key == 'want']
    haves = [value for key, value in args if key == 'have']
    shallow = [value for key, value in args if key == 'shallow']
    depth = None
    for key, value in args:
        if key == 'deepen':
            try:
                depth = int(value)
            except ValueError:
                depth = 1
    if depth is not None and depth <= 0:
        depth = None
    oids, new_shallow, unshallow = select_objects(repo, wants, haves, shallow, depth)
    parts = []
    for oid in new_shallow:
        parts.append(pkt('shallow %s\n' % oid))
    for oid in unshallow:
        parts.append(pkt('unshallow %s\n' % oid))
    parts.append(pkt('NAK\n'))
    parts.append(sideband(_pack_for(repo, oids), 1))
    parts.append(FLUSH)
    return b''.join(parts)


# --------------------------------------------------------------------------- receive-pack

def parse_receive(body):
    """(commands, capabilities, pack bytes) from a receive-pack request body."""
    commands = []
    caps = ''
    i = 0
    n = len(body)
    while i + 4 <= n:
        try:
            length = int(body[i:i + 4], 16)
        except ValueError:
            return commands, caps, b''
        if length == 0:
            i += 4
            break
        if length < 4 or i + length > n:
            return commands, caps, b''
        payload = body[i + 4:i + length]
        i += length
        line = payload.rstrip(b'\n')
        if b'\0' in line:
            line, caps = line.split(b'\0', 1)
            caps = caps.decode('utf-8', 'replace')
        text = line.decode('utf-8', 'replace')
        parts = text.split(' ')
        if len(parts) >= 3:
            commands.append((parts[0], parts[1], parts[2]))
    return commands, caps, body[i:]


def receive_pack(repo, forge, body):
    """Apply a push: unpack the pack, then report each ref's verdict."""
    commands, caps, pack = parse_receive(body)
    side = 'side-band-64k' in caps.split()
    report = []
    unpack_ok = True
    unpack_detail = 'ok'
    if pack:
        try:
            objects = gitobj.read_pack(pack, resolve_base=repo.get)
        except Exception:
            objects = None
        if objects is None:
            unpack_ok = False
            unpack_detail = 'unpacker error'
        else:
            for oid, otype, data in objects:
                if oid not in repo.objects:
                    repo.objects[oid] = (otype, data)
                    repo.version += 1
                forge.defer_object(otype, data)
    for old, new, ref in commands:
        current = repo.refs.get(ref)
        if new == ZERO_OID:
            if current is None:
                report.append((ref, True, ''))
            elif old != ZERO_OID and current != old:
                report.append((ref, False, 'failed to update ref'))
            else:
                repo.refs.pop(ref, None)
                report.append((ref, True, ''))
            continue
        if not repo.has(new):
            report.append((ref, False, 'missing necessary objects'))
            continue
        if current is None:
            repo.refs[ref] = new
            report.append((ref, True, ''))
            continue
        if old != ZERO_OID and current != old:
            report.append((ref, False, 'failed to update ref'))
            continue
        if ref.startswith('refs/heads/') and current != new and not repo.is_ancestor(current, new):
            report.append((ref, False, 'non-fast-forward'))
            continue
        repo.refs[ref] = new
        report.append((ref, True, ''))
    repo.pushed_at = forge.now
    repo.updated_at = forge.now
    repo.dirty = True
    repo.version += 1
    forge.touch()

    inner = [pkt('unpack %s\n' % unpack_detail)]
    for ref, ok, reason in report:
        if ok:
            inner.append(pkt('ok %s\n' % ref))
        else:
            inner.append(pkt('ng %s %s\n' % (ref, reason)))
    inner.append(FLUSH)
    framed = b''.join(inner)
    if not side:
        return framed
    return sideband(framed, 1) + FLUSH
