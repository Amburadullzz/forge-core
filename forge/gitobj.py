"""git object model, tree/commit codec and the packfile format (read and write).

Pure standard library: SHA-1 object ids, the loose-object header, git's tree sort order,
zlib-compressed pack entries, OFS_DELTA/REF_DELTA resolution and the pack trailer checksum.
"""

import hashlib
import struct
import zlib

TYPE_CODES = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
CODE_TYPES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}

OBJ_TYPES = ('commit', 'tree', 'blob', 'tag')


def obj_oid(otype, data):
    """git's object id: sha1 over `<type> <size>\\0` + the object's bytes."""
    h = hashlib.sha1()
    h.update(('%s %d\0' % (otype, len(data))).encode('ascii'))
    h.update(data)
    return h.hexdigest()


def loose_bytes(otype, data):
    return ('%s %d\0' % (otype, len(data))).encode('ascii') + data


# --------------------------------------------------------------------------- trees

def parse_tree(data):
    """[(mode, name, oid)] in the object's stored order."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b' ', i)
        nul = data.index(b'\0', sp + 1)
        mode = data[i:sp].decode('ascii')
        name = data[sp + 1:nul].decode('utf-8', 'surrogateescape')
        oid = data[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        i = nul + 21
    return out


def _tree_sort_key(entry):
    mode, name, _oid = entry
    return (name + '/' if mode == '40000' else name).encode('utf-8', 'surrogateescape')


def build_tree(entries):
    """Serialize tree entries in git's order (directories sort as if they ended in '/')."""
    parts = []
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        parts.append(('%s %s\0' % (mode, name)).encode('utf-8', 'surrogateescape'))
        parts.append(bytes.fromhex(oid))
    return b''.join(parts)


# --------------------------------------------------------------------------- commits / tags

def parse_commit(data):
    blank = data.find(b'\n\n')
    head = data if blank < 0 else data[:blank]
    message = b'' if blank < 0 else data[blank + 2:]
    tree = None
    parents = []
    author = b''
    committer = b''
    for line in head.split(b'\n'):
        if not line:
            continue
        sp = line.find(b' ')
        key = line if sp < 0 else line[:sp]
        val = b'' if sp < 0 else line[sp + 1:]
        if key == b'tree':
            tree = val.decode('ascii')
        elif key == b'parent':
            parents.append(val.decode('ascii'))
        elif key == b'author':
            author = val
        elif key == b'committer':
            committer = val
    return {'tree': tree, 'parents': parents, 'author': author,
            'committer': committer, 'message': message}


def build_commit(tree, parents, message, author, committer):
    if isinstance(message, str):
        message = message.encode('utf-8')
    lines = ['tree ' + tree]
    for parent in parents:
        lines.append('parent ' + parent)
    lines.append('author ' + author)
    lines.append('committer ' + committer)
    body = ('\n'.join(lines) + '\n\n').encode('utf-8')
    if not message.endswith(b'\n'):
        message += b'\n'
    return body + message


def parse_tag(data):
    blank = data.find(b'\n\n')
    head = data if blank < 0 else data[:blank]
    message = b'' if blank < 0 else data[blank + 2:]
    headers = {}
    for line in head.split(b'\n'):
        if not line:
            continue
        sp = line.find(b' ')
        if sp < 0:
            continue
        headers[line[:sp].decode('ascii')] = line[sp + 1:].decode('utf-8', 'surrogateescape')
    return {'object': headers.get('object', ''), 'type': headers.get('type', ''),
            'tag': headers.get('tag', ''), 'tagger': headers.get('tagger', ''),
            'message': message}


# --------------------------------------------------------------------------- pack write

def _pack_object_header(type_code, size):
    out = bytearray()
    first = (type_code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects, level=1):
    """A packfile (version 2) of plain objects: `objects` is [(otype, data)]. No deltas."""
    items = list(objects)
    parts = [b'PACK', struct.pack('>II', 2, len(items))]
    for otype, data in items:
        parts.append(_pack_object_header(TYPE_CODES[otype], len(data)))
        parts.append(zlib.compress(data, level))
    body = b''.join(parts)
    return body + hashlib.sha1(body).digest()


# --------------------------------------------------------------------------- pack read

class PackError(Exception):
    pass


def _inflate_at(buf, offset, end):
    d = zlib.decompressobj()
    try:
        out = d.decompress(buf[offset:end])
    except zlib.error as exc:
        raise PackError('zlib stream at %d did not inflate: %s' % (offset, exc))
    consumed = (end - offset) - len(d.unused_data)
    if consumed <= 0:
        raise PackError('zlib stream at %d consumed no bytes' % offset)
    return out, offset + consumed


def _apply_delta(base, delta):
    pos = 0
    size = 0
    shift = 0
    while True:
        b = delta[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    target_size = 0
    shift = 0
    while True:
        b = delta[pos]
        pos += 1
        target_size |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    if size != len(base):
        raise PackError('delta expects a %d-byte base, got %d' % (size, len(base)))
    out = bytearray()
    n = len(delta)
    while pos < n:
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    copy_off |= delta[pos] << shift
                    pos += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    copy_size |= delta[pos] << shift
                    pos += 1
                shift += 8
            if copy_size == 0:
                copy_size = 0x10000
            out += base[copy_off:copy_off + copy_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise PackError('delta opcode 0 is invalid')
    if len(out) != target_size:
        raise PackError('delta produced %d of %d bytes' % (len(out), target_size))
    return bytes(out)


def read_pack(buf, resolve_base=None):
    """Resolve a packfile into [(oid, otype, data)]. `resolve_base(oid)` supplies thin-pack bases."""
    if len(buf) < 32 or buf[:4] != b'PACK':
        raise PackError('not a packfile')
    version, count = struct.unpack('>II', buf[4:12])
    if version not in (2, 3):
        raise PackError('unsupported pack version %d' % version)
    end = len(buf) - 20
    raw = []
    by_offset = {}
    offset = 12
    for _index in range(count):
        start = offset
        b = buf[offset]
        offset += 1
        type_code = (b >> 4) & 0x07
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = buf[offset]
            offset += 1
            size |= (b & 0x7F) << shift
            shift += 7
        base_offset = None
        base_oid = None
        if type_code == 6:
            b = buf[offset]
            offset += 1
            off = b & 0x7F
            while b & 0x80:
                b = buf[offset]
                offset += 1
                off = ((off + 1) << 7) | (b & 0x7F)
            base_offset = start - off
        elif type_code == 7:
            base_oid = buf[offset:offset + 20].hex()
            offset += 20
        elif type_code not in CODE_TYPES:
            raise PackError('pack entry has unknown type code %d' % type_code)
        data, offset = _inflate_at(buf, offset, end)
        if len(data) != size:
            raise PackError('pack entry at %d declares %d bytes, inflated %d' % (start, size, len(data)))
        entry = {'offset': start, 'type_code': type_code, 'data': data,
                 'base_offset': base_offset, 'base_oid': base_oid,
                 'resolved': None, 'oid': None, 'otype': None}
        raw.append(entry)
        by_offset[start] = entry
    if offset != end:
        raise PackError('pack has %d stray bytes before its trailer' % (end - offset))

    by_oid = {}
    resolving = set()

    def resolve(entry):
        if entry['resolved'] is not None:
            return entry['otype'], entry['resolved'], entry['oid']
        if entry['offset'] in resolving:
            raise PackError('delta cycle at %d' % entry['offset'])
        resolving.add(entry['offset'])
        code = entry['type_code']
        if code == 6:
            base = by_offset.get(entry['base_offset'])
            if base is None:
                raise PackError('ofs-delta base offset %s is not in the pack' % entry['base_offset'])
            otype, bdata, _ = resolve(base)
            data = _apply_delta(bdata, entry['data'])
        elif code == 7:
            base = by_oid.get(entry['base_oid'])
            if base is not None:
                otype, bdata, _ = resolve(base)
                data = _apply_delta(bdata, entry['data'])
            else:
                external = resolve_base(entry['base_oid']) if resolve_base else None
                if external is None:
                    raise PackError('ref-delta base %s is not in the pack' % entry['base_oid'])
                otype, bdata = external
                data = _apply_delta(bdata, entry['data'])
        else:
            otype = CODE_TYPES[code]
            data = entry['data']
        oid = obj_oid(otype, data)
        entry['resolved'] = data
        entry['otype'] = otype
        entry['oid'] = oid
        by_oid[oid] = entry
        resolving.discard(entry['offset'])
        return otype, data, oid

    out = []
    for entry in raw:
        otype, data, oid = resolve(entry)
        out.append((oid, otype, data))
    return out


# --------------------------------------------------------------------------- tree flatten / build

def flatten_tree(store, tree_oid, prefix='', out=None):
    """{path: (mode, oid)} for every blob (and gitlink) under a tree."""
    if out is None:
        out = {}
    obj = store.get(tree_oid)
    if obj is None or obj[0] != 'tree':
        return out
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == '40000':
            flatten_tree(store, oid, path + '/', out)
        else:
            out[path] = (mode, oid)
    return out


def build_tree_from_files(store, files):
    """Create the trees for {path: (mode, oid)} and answer the root tree oid."""
    root = {}

    def node_for(parts, node):
        if len(parts) == 1:
            node.setdefault('_files', {})[parts[0]] = True
            return node
        node.setdefault('_dirs', {}).setdefault(parts[0], {'_files': {}})
        return node_for(parts[1:], node['_dirs'][parts[0]])

    root = {'_files': {}, '_dirs': {}}
    for path, value in files.items():
        parts = path.split('/')
        node = root
        for part in parts[:-1]:
            node = node['_dirs'].setdefault(part, {'_files': {}, '_dirs': {}})
        node['_files'][parts[-1]] = value

    def emit(node):
        entries = []
        for name, value in node['_files'].items():
            mode, oid = value
            entries.append((mode, name, oid))
        for name, child in node['_dirs'].items():
            entries.append(('40000', name, emit(child)))
        data = build_tree(entries)
        return store.put('tree', data)

    return emit(root)


def commit_tree(store, commit_oid):
    obj = store.get(commit_oid)
    if obj is None or obj[0] != 'commit':
        return None
    return parse_commit(obj[1])['tree']
