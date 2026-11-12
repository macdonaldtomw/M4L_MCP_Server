"""Open devices/patch files, keep edits in memory, save safely.

A Document is opened from disk, edited in memory (JSON patchers are parsed on
demand and re-serialised only if marked dirty), and written back with
``save()``, which

* refuses if the file on disk changed since it was read (sha256 compare),
* re-parses the bytes it is about to write and checks every entry,
* backs the old file up, then writes a temp file and ``os.replace``s it.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import time
from typing import Optional

from . import container as C

BACKUP_DIR = os.environ.get('M4L_MCP_BACKUP_DIR') or os.path.join(
    os.path.expanduser('~'), '.m4l-mcp', 'backups')


def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------- JSON --
class JsonStyle:
    """How a JSON entry was laid out, so a rewritten entry looks like the
    original: newline style and the trailing bytes after the final brace
    (Max's top-level device JSON ends with ``\\r\\n\\0``)."""

    def __init__(self, data: bytes):
        end = data.rfind(b'}')
        self.suffix = data[end + 1:] if end >= 0 else b''
        self.crlf = b'\r\n' in data[:4096]

    def dump(self, obj) -> bytes:
        s = json.dumps(obj, indent='\t', ensure_ascii=False, separators=(',', ' : '))
        if self.crlf:
            s = s.replace('\n', '\r\n')
        return s.encode('utf-8') + self.suffix


def loads(data: bytes):
    end = data.rfind(b'}')
    return json.loads(data[:end + 1].decode('utf-8'))


# ----------------------------------------------------------------- Document --
class Document:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.reload()

    # -- loading
    def reload(self):
        raw = open(self.path, 'rb').read()
        self.disk_sha = sha(raw)
        self.disk_size = len(raw)
        self.loaded_at = time.time()
        self.c = C.parse(raw, self.path)
        self.roundtrip_ok = C.build(self.c) == raw
        self.orig = C.parse(raw, self.path)          # pristine copy for diffs
        self._json = {}       # entry name -> parsed object
        self._style = {}
        self.dirty = set()    # entry names whose JSON object must be re-serialised
        self.changed = set()  # entry names changed at all (incl. binary / added)
        self.removed = set()

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty or self.changed or self.removed)

    @property
    def kind(self) -> str:
        return self.c.kind

    def names(self):
        return [e.name for e in self.c.entries]

    @property
    def top_name(self) -> str:
        return self.c.entries[0].name

    def entry(self, name: Optional[str] = None) -> C.Entry:
        name = name or self.top_name
        try:
            return self.c.entry(name)
        except KeyError:
            raise KeyError('no embedded file %r in %s; files: %s' % (
                name, os.path.basename(self.path), ', '.join(self.names()))) from None

    def resolve_name(self, name: Optional[str]) -> str:
        """Accept exact names, case-insensitive names, or a unique substring."""
        if not name:
            return self.top_name
        ns = self.names()
        if name in ns:
            return name
        low = [n for n in ns if n.lower() == name.lower()]
        if len(low) == 1:
            return low[0]
        sub = [n for n in ns if name.lower() in n.lower()]
        if len(sub) == 1:
            return sub[0]
        if not sub:
            raise KeyError('no embedded file matching %r; files: %s' % (name, ', '.join(ns)))
        raise KeyError('%r is ambiguous: %s' % (name, ', '.join(sub)))

    def json(self, name: Optional[str] = None):
        name = self.resolve_name(name)
        if name not in self._json:
            e = self.entry(name)
            if not e.is_json():
                raise ValueError('%s is not a JSON patcher (type %r)' % (name, e.type))
            self._json[name] = loads(e.data)
            self._style[name] = JsonStyle(e.data)
        return self._json[name]

    def json_entries(self):
        return [e.name for e in self.c.entries if e.is_json()]

    def mark_dirty(self, name: Optional[str] = None):
        name = self.resolve_name(name)
        self.dirty.add(name)
        self.changed.add(name)

    def snapshot(self):
        return (copy.deepcopy(self._json), set(self.dirty), set(self.changed))

    def restore(self, snap):
        self._json, self.dirty, self.changed = copy.deepcopy(snap[0]), set(snap[1]), set(snap[2])

    # -- embedded files
    def put_file(self, name: str, data: bytes, type_code: Optional[bytes] = None) -> str:
        try:
            e = self.c.entry(name)
            e.data = data
            if type_code:
                e.fields = [(t, type_code if t == 'type' else v) for t, v in e.fields]
            status = 'replaced'
        except KeyError:
            self.c.entries.append(C.new_entry_like(self.c, name, data, type_code))
            status = 'added'
        self._json.pop(name, None)
        self._style.pop(name, None)
        self.dirty.discard(name)
        self.changed.add(name)
        self.removed.discard(name)
        return status

    def remove_file(self, name: str):
        name = self.resolve_name(name)
        if name == self.top_name:
            raise ValueError('cannot remove the top-level patcher')
        self.c.entries = [e for e in self.c.entries if e.name != name]
        self._json.pop(name, None)
        self.dirty.discard(name)
        self.changed.discard(name)
        self.removed.add(name)

    # -- serialisation
    def serialize(self) -> bytes:
        for name in self.dirty:
            self.entry(name).data = self._style[name].dump(self._json[name])
        self.dirty.clear()
        return C.build(self.c)

    def verify_bytes(self, data: bytes):
        """Re-parse what we are about to write and check it entry by entry."""
        c2 = C.parse(data, self.path)
        if c2.kind != self.c.kind:
            raise RuntimeError('verify: kind changed %s -> %s' % (self.c.kind, c2.kind))
        if [e.name for e in c2.entries] != self.names():
            raise RuntimeError('verify: directory mismatch')
        for a, b in zip(self.c.entries, c2.entries):
            if a.data != b.data:
                raise RuntimeError('verify: entry %s differs after rebuild' % a.name)
            if b.is_json():
                try:
                    loads(b.data)
                except Exception as ex:
                    raise RuntimeError('verify: %s is not valid JSON after rebuild: %s' % (b.name, ex))
        if self.kind != 'maxpat':
            top = c2.entries[0].data
            if not top.rstrip(b'\0').rstrip().endswith(b'}'):
                raise RuntimeError('verify: top-level JSON does not end with }')

    def save(self, dest: Optional[str] = None, force: bool = False, overwrite: bool = False) -> dict:
        dest = os.path.abspath(dest) if dest else self.path
        same = norm(dest) == norm(self.path)
        if not self.roundtrip_ok and not force:
            raise RuntimeError('this file did not rebuild byte-identically when opened, so the container '
                               'format is not fully understood; refusing to write (pass force=true to override)')
        notes = []
        if os.path.exists(dest):
            cur = open(dest, 'rb').read()
            if same and sha(cur) != self.disk_sha and not force:
                raise RuntimeError(
                    '%s changed on disk since it was read (someone saved it, e.g. from Max). '
                    'Refusing to overwrite. Use device_revert to reload it and redo the edits, '
                    'or save with force=true to discard the on-disk version (it is still backed up).' % dest)
            if not same and not overwrite:
                raise RuntimeError('%s exists; pass overwrite=true to replace it (it will be backed up)' % dest)
            backup = backup_file(dest, cur)
        else:
            backup = None
        data = self.serialize()
        self.verify_bytes(data)
        atomic_write(dest, data)
        changed = sorted(self.changed)
        removed = sorted(self.removed)
        if same:
            self.reload()
        else:
            notes.append('saved a copy; the open document still refers to %s' % self.path)
        return {'written': dest, 'bytes': len(data), 'backup': backup,
                'changed_files': changed, 'removed_files': removed, 'notes': notes}


def backup_file(path: str, data: bytes) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    base = os.path.basename(path)
    out = os.path.join(BACKUP_DIR, '%s.%s.bak' % (base, stamp))
    n = 1
    while os.path.exists(out):
        out = os.path.join(BACKUP_DIR, '%s.%s-%d.bak' % (base, stamp, n))
        n += 1
    with open(out, 'wb') as f:
        f.write(data)
    return out


def atomic_write(path: str, data: bytes):
    d = os.path.dirname(path) or '.'
    tmp = os.path.join(d, '.%s.m4l-mcp-%d.tmp' % (os.path.basename(path), os.getpid()))
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    last = None
    for _ in range(20):            # Live/Max may hold the file briefly on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError as ex:
            last = ex
            time.sleep(0.1)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise RuntimeError('could not replace %s (file locked?): %s' % (path, last))


# ----------------------------------------------------------------- registry --
class Registry:
    def __init__(self):
        self.docs = {}

    def get(self, path: str) -> Document:
        key = norm(path)
        doc = self.docs.get(key)
        if doc is None:
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            doc = self.docs[key] = Document(path)
            return doc
        # pick up outside changes automatically when we hold no edits
        try:
            st = os.stat(doc.path)
            if not doc.is_dirty and (st.st_size != doc.disk_size or st.st_mtime > doc.loaded_at):
                if sha(open(doc.path, 'rb').read()) != doc.disk_sha:
                    doc.reload()
        except FileNotFoundError:
            pass
        return doc

    def disk_changed(self, doc: Document) -> bool:
        try:
            return sha(open(doc.path, 'rb').read()) != doc.disk_sha
        except FileNotFoundError:
            return True

    def drop(self, path: str):
        self.docs.pop(norm(path), None)


REGISTRY = Registry()
