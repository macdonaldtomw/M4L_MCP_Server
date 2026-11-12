"""Binary container formats for Max for Live devices and Max patch files.

Supported layouts
-----------------
* ``.amxd`` (plain):  RIFF-like little-endian chunks ``ampf`` (device type),
  ``meta``, ``ptch``; the ``ptch`` payload is the patcher JSON, ending ``\\r\\n\\0``.
* ``.amxd`` (frozen): same chunks, but the ``ptch`` payload is an ``mx@c``
  collective: a 16-byte big-endian header (magic, header size, 0, offset of the
  directory), the concatenated file bodies, then a ``dlst`` directory of
  ``dire`` records (``type fnam sz32 of32 vers flag mdat``; filenames are
  NUL-terminated and padded to 4 bytes). The first entry is the device's own
  top-level patcher JSON.
* ``.maxpat`` / ``.maxhelp`` / ``.json``: a single JSON patcher on disk.

Everything not understood is preserved byte for byte; ``build()`` of an
unmodified parse reproduces the original file exactly (checked on open).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

DEVICE_TYPES = {b'aaaa': 'audio_effect', b'mmmm': 'midi_effect', b'iiii': 'instrument',
                b'nnnn': 'note_effect'}

# 4-char directory type codes used in collectives, by extension.
TYPE_BY_EXT = {
    '.maxpat': b'JSON', '.json': b'JSON', '.maxhelp': b'JSON', '.amxd': b'JSON',
    '.js': b'TEXT', '.txt': b'TEXT', '.jsx': b'TEXT', '.xml': b'TEXT', '.coll': b'TEXT',
    '.png': b'PNG ', '.jpg': b'JPEG', '.jpeg': b'JPEG', '.svg': b'svg ', '.gif': b'GIFf',
    '.wav': b'WAVE', '.aif': b'AIFF', '.aiff': b'AIFF',
}


class FormatError(ValueError):
    pass


@dataclass
class Entry:
    """One embedded file. ``fields`` keeps every directory record field in
    order (raw bytes); ``sz32``/``of32`` are recomputed on build."""
    name: str
    data: bytes
    fields: list = field(default_factory=list)   # [(tag:str, value:bytes)]

    @property
    def type(self) -> str:
        for t, v in self.fields:
            if t == 'type':
                return v.decode('latin-1')
        return ''

    def is_json(self) -> bool:
        if self.type == 'JSON':
            return True
        return self.name.lower().endswith(('.maxpat', '.json', '.maxhelp', '.amxd'))


@dataclass
class Container:
    kind: str                       # 'amxd-plain' | 'amxd-frozen' | 'maxpat'
    entries: list                   # [Entry]
    chunks: list = field(default_factory=list)   # amxd: [(tag, payload)] before ptch
    trailer: list = field(default_factory=list)  # amxd: chunks after ptch
    mx_header: bytes = b''          # frozen: first 12 bytes of the mx@c header

    @property
    def device_type(self) -> Optional[str]:
        for tag, payload in self.chunks:
            if tag == b'ampf':
                return DEVICE_TYPES.get(payload[:4], payload[:4].decode('latin-1'))
        return None

    def entry(self, name: str) -> Entry:
        for e in self.entries:
            if e.name == name:
                return e
        raise KeyError(name)


def _chunks(d: bytes):
    p, out = 0, []
    while p < len(d):
        if p + 8 > len(d):
            raise FormatError('truncated chunk header at %d' % p)
        tag = d[p:p + 4]
        n = struct.unpack('<I', d[p + 4:p + 8])[0]
        if p + 8 + n > len(d):
            raise FormatError('chunk %r at %d overruns file (%d > %d)' % (tag, p, p + 8 + n, len(d)))
        out.append((tag, d[p + 8:p + 8 + n]))
        p += 8 + n
    return out


def parse(d: bytes, filename: str = '') -> Container:
    if d[:4] != b'ampf':
        # plain JSON patcher file
        s = d.lstrip()
        if not s.startswith(b'{'):
            raise FormatError('%s: not an .amxd (no ampf header) and not a JSON patcher' % (filename or 'file'))
        name = filename.replace('\\', '/').rsplit('/', 1)[-1] or 'patch.maxpat'
        return Container('maxpat', [Entry(name, d, [('type', b'JSON')])])

    chunks = _chunks(d)
    idx = [i for i, (t, _) in enumerate(chunks) if t == b'ptch']
    if len(idx) != 1:
        raise FormatError('expected exactly one ptch chunk, found %d' % len(idx))
    i = idx[0]
    head, ptch, trailer = chunks[:i], chunks[i][1], chunks[i + 1:]
    name = filename.replace('\\', '/').rsplit('/', 1)[-1] or 'device.amxd'

    if ptch[:4] != b'mx@c':
        return Container('amxd-plain', [Entry(name, ptch, [('type', b'JSON')])], head, trailer)

    if len(ptch) < 16:
        raise FormatError('mx@c header truncated')
    dir_off = struct.unpack('>I', ptch[12:16])[0]
    dl = ptch[dir_off:]
    if dl[:4] != b'dlst':
        raise FormatError('mx@c directory not found at offset %d' % dir_off)
    n = struct.unpack('>I', dl[4:8])[0]
    p, entries = 8, []
    while p < n:
        tag, sz = dl[p:p + 4], struct.unpack('>I', dl[p + 4:p + 8])[0]
        if tag != b'dire':
            raise FormatError('unexpected directory record %r' % tag)
        q, fields = p + 8, []
        while q < p + sz:
            t = dl[q:q + 4].decode('latin-1')
            s = struct.unpack('>I', dl[q + 4:q + 8])[0]
            fields.append((t, dl[q + 8:q + s]))
            q += s
        f = dict(fields)
        ename = f['fnam'].split(b'\0', 1)[0].decode('utf-8')
        size = struct.unpack('>I', f['sz32'])[0]
        off = struct.unpack('>I', f['of32'])[0]
        entries.append(Entry(ename, ptch[off:off + size], fields))
        p += sz
    return Container('amxd-frozen', entries, head, trailer, ptch[:12])


def _pad_name(name: str) -> bytes:
    b = name.encode('utf-8') + b'\0'
    return b + b'\0' * (-len(b) % 4)


def build(c: Container) -> bytes:
    if c.kind == 'maxpat':
        return c.entries[0].data
    if c.kind == 'amxd-plain':
        ptch = c.entries[0].data
    else:
        body, pos, offs = [], 16, []
        for e in c.entries:
            offs.append(pos)
            body.append(e.data)
            pos += len(e.data)
        dl = []
        for e, off in zip(c.entries, offs):
            rec = []
            for t, v in e.fields:
                if t == 'sz32':
                    v = struct.pack('>I', len(e.data))
                elif t == 'of32':
                    v = struct.pack('>I', off)
                rec.append(t.encode('latin-1') + struct.pack('>I', len(v) + 8) + v)
            rec = b''.join(rec)
            dl.append(b'dire' + struct.pack('>I', len(rec) + 8) + rec)
        dl = b''.join(dl)
        dl = b'dlst' + struct.pack('>I', len(dl) + 8) + dl
        ptch = c.mx_header + struct.pack('>I', pos) + b''.join(body) + dl
    out = [t + struct.pack('<I', len(p)) + p for t, p in c.chunks]
    out.append(b'ptch' + struct.pack('<I', len(ptch)) + ptch)
    out += [t + struct.pack('<I', len(p)) + p for t, p in c.trailer]
    return b''.join(out)


def new_entry_like(c: Container, name: str, data: bytes, type_code: Optional[bytes] = None) -> Entry:
    """A directory record for a new embedded file, modelled on an existing
    non-top-level entry (so vers/flag/mdat fields look like Max's own)."""
    if c.kind != 'amxd-frozen':
        raise FormatError('only frozen devices (mx@c collectives) can hold extra files; '
                          'freeze the device in Max first')
    ext = '.' + name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    tc = type_code or TYPE_BY_EXT.get(ext, b'TEXT')
    if len(tc) != 4:
        raise FormatError('type code must be 4 bytes, got %r' % tc)
    tmpl = c.entries[1] if len(c.entries) > 1 else c.entries[0]
    fields = []
    for t, v in tmpl.fields:
        if t == 'type':
            v = tc
        elif t == 'fnam':
            v = _pad_name(name)
        elif t == 'flag':
            v = b'\0' * len(v)
        fields.append((t, v))
    return Entry(name, data, fields)


def rename_entry(e: Entry, name: str) -> None:
    e.name = name
    e.fields = [(t, _pad_name(name) if t == 'fnam' else v) for t, v in e.fields]
