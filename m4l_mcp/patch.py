"""Patcher model: addressing, references, instance tree, queries, trace,
validation, edits and Live parameter registration.

Addressing
----------
* ``file``: an embedded file name of a frozen device (unique substring ok);
  omitted = the device's top-level patcher (or the only patcher of a .maxpat).
* box paths: ``obj-12`` or ``obj-1137::obj-12`` -- ids of boxes with an
  embedded ``patcher`` (``p``/``patcher``/embedded bpatcher) followed by the
  box id. ``/`` works as separator too. Paths stay inside one file: a bpatcher
  or abstraction that refers to another embedded file is a *reference*; address
  that file directly (``patch_tree`` shows where every file is used).
* instance paths (Live parameter registry keys) chain box ids through
  references as well, e.g. ``obj-1137::obj-50::obj-107::obj-107``.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import objdb
from .document import REGISTRY, Document

SEP = '::'

DECOR = {'panel', 'comment', 'fpic', 'live.line', 'live.comment', 'live.meter~', 'live.scope~'}


# ------------------------------------------------------------ addressing ----
def split_path(path) -> list:
    if path is None:
        return []
    if isinstance(path, (list, tuple)):
        return [str(x) for x in path]
    return [x for x in re.split(r'::|/', str(path).strip()) if x]


def join(*parts) -> str:
    out = []
    for p in parts:
        out += split_path(p)
    return SEP.join(out)


def boxes(p):
    return [b['box'] for b in p.get('boxes', [])]


def box_map(p):
    return {b['id']: b for b in boxes(p)}


def get_patcher(doc: Document, file: Optional[str], sub=None):
    j = doc.json(file)
    p = j['patcher']
    walked = []
    for sid in split_path(sub):
        m = box_map(p)
        if sid not in m:
            raise KeyError('no box %s in %s' % (sid, where(doc, file, walked)))
        if not isinstance(m[sid].get('patcher'), dict):
            raise KeyError('%s in %s has no embedded patcher (it may reference another file: %s)'
                           % (sid, where(doc, file, walked), ref_summary(doc, m[sid]) or 'none'))
        p = m[sid]['patcher']
        walked.append(sid)
    return p


def get_box(doc: Document, file: Optional[str], path):
    ids = split_path(path)
    if not ids:
        raise ValueError('empty box path')
    p = get_patcher(doc, file, ids[:-1])
    m = box_map(p)
    if ids[-1] not in m:
        raise KeyError('no box %s in %s' % (ids[-1], where(doc, file, ids[:-1])))
    return p, m[ids[-1]], ids[:-1]


def where(doc: Document, file, sub) -> str:
    name = doc.resolve_name(file)
    s = join(sub)
    return name + (' :: ' + s if s else '')


def label(b, n=60) -> str:
    t = b.get('text')
    if t is None and b.get('maxclass') == 'bpatcher':
        t = b.get('name', '(embedded)')
    if t is None:
        vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
        t = vo.get('parameter_longname') or b.get('varname') or ''
    t = str(t).replace('\n', ' ')
    if len(t) > n:
        t = t[:n - 1] + '…'
    mc = b.get('maxclass', '?')
    return '%s [%s]' % (b.get('id'), t if mc == 'newobj' else (mc + (' ' + t if t else '')))


def is_param(b) -> bool:
    """UI objects use the box attribute; non-UI objects (dict, coll, pattr...)
    use saved_object_attributes / '@parameter_enable 1'."""
    return bool(b.get('parameter_enable') or (b.get('saved_object_attributes') or {}).get('parameter_enable'))


def param_info(b):
    if not is_param(b):
        return None
    vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
    return {k.replace('parameter_', ''): v for k, v in vo.items()}


def summary(b, doc=None) -> dict:
    s = {'id': b.get('id'), 'maxclass': b.get('maxclass')}
    for k in ('text', 'varname', 'name', 'numinlets', 'numoutlets', 'outlettype', 'patching_rect'):
        if k in b:
            s[k] = b[k]
    if b.get('presentation'):
        s['presentation_rect'] = b.get('presentation_rect')
    if b.get('hidden'):
        s['hidden'] = 1
    pi = param_info(b)
    if pi:
        s['parameter'] = {k: pi[k] for k in ('longname', 'shortname', 'type') if k in pi}
    if isinstance(b.get('patcher'), dict):
        s['embedded_patcher'] = '%d boxes' % len(b['patcher'].get('boxes', []))
    if doc is not None:
        r = ref_summary(doc, b)
        if r:
            s['refers_to'] = r
    return s


# ------------------------------------------------------------ references ----
_INDEX = None


def search_roots():
    env = os.environ.get('M4L_MCP_SEARCH')
    if env:
        return [p for p in env.split(os.pathsep) if p]
    home = os.path.expanduser('~')
    roots = []
    for v in ('Max 9', 'Max 8'):
        roots += [os.path.join(home, 'Documents', v, 'Library'), os.path.join(home, 'Documents', v, 'Packages')]
    ul = os.environ.get('ABLETON_USER_LIBRARY')
    if ul:
        roots.append(ul)
    for r in objdb.default_roots():
        for sub in ('packages', 'patchers', 'externals'):
            roots.append(os.path.join(r, sub))
    return [r for r in roots if os.path.isdir(r)]


def disk_index():
    """lower-case file name -> path, first root wins (Max search-path order)."""
    global _INDEX
    if _INDEX is None:
        idx = {}
        for root in search_roots():
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if not d.startswith('.')]
                for f in fn:
                    idx.setdefault(f.lower(), os.path.join(dp, f))
        _INDEX = idx
    return _INDEX


def resolve_file(doc: Document, name: str, local_dir: Optional[str] = None):
    """('embedded', name) | ('disk', path) | None, in Max's lookup order:
    the collective first, then next to the file, then the search path."""
    if not name:
        return None
    if os.path.isabs(name) and os.path.isfile(name):
        return ('disk', name)
    # Max falls back to the bare file name for absolute paths saved on another machine
    base = re.split(r'[\\/]', name)[-1].lower()
    for n in doc.names():
        if n.lower() == base:
            return ('embedded', n)
    d = local_dir or os.path.dirname(doc.path)
    p = _dir_files(d).get(base) or disk_index().get(base)
    return ('disk', p) if p else None


_DIRS = {}


def _dir_files(d):
    if d not in _DIRS:
        try:
            _DIRS[d] = {f.lower(): os.path.join(d, f) for f in os.listdir(d)}
        except OSError:
            _DIRS[d] = {}
    return _DIRS[d]


def reset_caches():
    global _INDEX
    _INDEX = None
    _DIRS.clear()


def file_refs(b) -> list:
    """[(kind, filename)] this box loads by name. 'abstraction?' entries are
    only real if they resolve (an object name might be an external)."""
    out = []
    mc = b.get('maxclass')
    if mc == 'bpatcher' and not isinstance(b.get('patcher'), dict) and b.get('name'):
        out.append(('bpatcher', b['name']))
    if mc == 'newobj':
        toks = str(b.get('text', '')).split()
        if toks:
            c = toks[0]
            if c in ('poly~', 'pfft~', 'bpatcher') and len(toks) > 1:
                n = toks[1]
                out.append((c, n if n.lower().endswith(('.maxpat', '.json', '.maxhelp')) else n + '.maxpat'))
            elif c in ('js', 'node.script', 'v8', 'jsui') and len(toks) > 1:
                n = toks[1]
                out.append(('script', n if n.lower().endswith(('.js', '.mjs', '.ts')) else n + '.js'))
            elif c not in ('p', 'patcher') and not isinstance(b.get('patcher'), dict):
                out.append(('abstraction?', c + '.maxpat'))
    if mc in ('jsui', 'v8ui') and b.get('filename'):
        n = b['filename']
        out.append(('script', n if '.' in n else n + '.js'))
    for k in ('pictures',):
        for n in b.get(k) or []:
            if n:
                out.append(('picture', n))
    for k in ('pic', 'picture', 'bgfile', 'imagefile'):
        if isinstance(b.get(k), str) and b.get(k):
            out.append(('picture', b[k]))
    return list(dict.fromkeys(out))


def patch_ref(doc: Document, b, local_dir=None):
    """If this box instantiates a patcher stored in a file: (kind, where, target)."""
    for kind, name in file_refs(b):
        if kind in ('bpatcher', 'poly~', 'pfft~', 'abstraction?'):
            r = resolve_file(doc, name, local_dir)
            if r:
                return (kind.rstrip('?'), r[0], r[1])
    return None


def ref_summary(doc: Document, b):
    r = patch_ref(doc, b)
    if r:
        return '%s -> %s %s' % r
    return None


def open_target(doc: Document, where_, target):
    """(Document, file) for a resolved reference."""
    if where_ == 'embedded':
        return doc, target
    d = REGISTRY.get(target)
    return d, None


# --------------------------------------------------------- instance tree ----
def subpatchers(doc, file, p, sub, local_dir=None):
    """Yield (box, child) for each patcher-holding box in p; child is
    (doc, file, sub) or None if unresolved."""
    for b in boxes(p):
        if isinstance(b.get('patcher'), dict):
            yield b, 'embedded patcher', (doc, file, sub + [b['id']])
            continue
        r = patch_ref(doc, b, local_dir)
        if r:
            kind, w, t = r
            try:
                d2, f2 = open_target(doc, w, t)
                d2.json(f2)
                yield b, '%s (%s)' % (kind, w), (d2, f2, [])
            except Exception:
                yield b, '%s (%s, unreadable)' % (kind, w), None


def instance_tree(doc: Document, max_depth=12, include_poly=False):
    """Flat list of patcher instances: {'instance', 'doc', 'file', 'sub', 'via', 'depth'}."""
    out = [{'instance': '', 'doc': doc, 'file': doc.top_name, 'sub': [], 'via': 'top', 'depth': 0}]

    def rec(d, f, sub, prefix, depth, stack):
        if depth >= max_depth:
            return
        p = get_patcher(d, f, sub)
        for b, via, child in subpatchers(d, f, p, sub, os.path.dirname(d.path)):
            if child is None:
                continue
            if not include_poly and via.startswith(('poly~', 'pfft~')):
                continue
            inst = join(prefix, b['id'])
            d2, f2, s2 = child
            key = (d2.path, d2.resolve_name(f2), tuple(s2))
            if key in stack:
                continue
            out.append({'instance': inst, 'doc': d2, 'file': d2.resolve_name(f2), 'sub': s2, 'via': via,
                        'depth': depth + 1, 'box': label(b)})
            rec(d2, f2, s2, inst, depth + 1, stack | {key})

    rec(doc, doc.top_name, [], '', 0, frozenset())
    return out


def tree_report(doc: Document, max_depth=12):
    lines = []
    usage = {}
    for n in instance_tree(doc, max_depth, include_poly=True):
        loc = n['file'] if n['doc'] is doc else n['doc'].path
        if n['sub']:
            loc += ' :: ' + join(n['sub'])
        if n['depth']:
            lines.append('%s%s  %s  via %s  -> %s' % ('  ' * n['depth'], n['instance'], n.get('box', ''),
                                                    n['via'], loc))
            if not n['sub']:
                usage.setdefault(n['file'] if n['doc'] is doc else n['doc'].path, []).append(n['instance'])
        else:
            lines.append('(top) ' + loc)
    return lines, usage


def instance_prefixes(doc: Document, file, sub):
    f = doc.resolve_name(file)
    s = split_path(sub)
    return [n['instance'] for n in instance_tree(doc)
            if n['doc'] is doc and n['file'] == f and n['sub'] == s]


# ---------------------------------------------------------------- search ----
def all_patchers(doc: Document):
    """Every patcher in every JSON entry of this document: (file, sub, p)."""
    for name in doc.json_entries():
        try:
            j = doc.json(name)
        except Exception:
            continue
        if not isinstance(j, dict) or not isinstance(j.get('patcher'), dict):
            continue
        stack = [([], j['patcher'])]
        while stack:
            sub, p = stack.pop()
            yield name, sub, p
            for b in boxes(p):
                if isinstance(b.get('patcher'), dict):
                    stack.append((sub + [b['id']], b['patcher']))


def find(doc: Document, query=None, maxclass=None, id=None, file=None, attr=None, limit=50):
    rx = None
    if query and query.startswith('re:'):
        rx = re.compile(query[3:], re.I)
    q = (query or '').lower()
    fname = doc.resolve_name(file) if file else None
    hits = []
    for name, sub, p in all_patchers(doc):
        if fname and name != fname:
            continue
        for b in boxes(p):
            if maxclass and b.get('maxclass') != maxclass:
                continue
            if id and b.get('id') != id:
                continue
            if attr:
                k, _, v = attr.partition('=')
                if k not in b or (v and str(b[k]) != v):
                    continue
            if query:
                vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
                hay = ' | '.join(str(x) for x in (b.get('text', ''), b.get('varname', ''), b.get('name', ''),
                                                  vo.get('parameter_longname', ''), b.get('hint', ''),
                                                  b.get('annotation', ''), b.get('comment', '')))
                if rx is not None:
                    if not rx.search(hay):
                        continue
                elif q not in hay.lower():
                    continue
            s = summary(b, doc)
            s['file'] = name
            s['box'] = join(sub, b['id'])
            hits.append(s)
            if len(hits) >= limit:
                return hits, True
    return hits, False


# ----------------------------------------------------------- connections ----
def connections(doc: Document, file, box_path):
    p, b, sub = get_box(doc, file, box_path)
    m = box_map(p)
    bid = b['id']
    ins, outs = [], []
    for l in p.get('lines', []):
        pl = l['patchline']
        s, d = pl['source'], pl['destination']
        if d[0] == bid:
            ins.append({'inlet': d[1], 'from': label(m[s[0]]) if s[0] in m else s[0] + ' (MISSING)',
                        'outlet': s[1], 'box': join(sub, s[0])})
        if s[0] == bid:
            outs.append({'outlet': s[1], 'to': label(m[d[0]]) if d[0] in m else d[0] + ' (MISSING)',
                         'inlet': d[1], 'box': join(sub, d[0])})
    ins.sort(key=lambda x: (x['inlet'], x['box']))
    outs.sort(key=lambda x: (x['outlet'], x['box']))
    res = {'box': summary(b, doc), 'in': ins, 'out': outs}
    links = named_links(doc, b)
    if links:
        res['named_links'] = links
    return res


def all_lines(doc: Document, file, sub, limit=300):
    p = get_patcher(doc, file, sub)
    m = box_map(p)
    out = []
    for l in p.get('lines', [])[:limit]:
        s, d = l['patchline']['source'], l['patchline']['destination']
        out.append('%s:%d -> %s:%d' % (label(m[s[0]], 40) if s[0] in m else s[0] + '?', s[1],
                                       label(m[d[0]], 40) if d[0] in m else d[0] + '?', d[1]))
    return out, len(p.get('lines', []))


# --------------------------------------------------------- named links -----
SENDERS = {'s': 'send', 'send': 'send', 'send~': 'send~', 'forward': 'forward', 'v': 'value', 'value': 'value',
           'pv': 'pv'}
RECEIVERS = {'r': 'send', 'receive': 'send', 'receive~': 'send~', 'v': 'value', 'value': 'value', 'pv': 'pv'}


def _sends(b):
    """[(channel, name)] this box sends to by name."""
    out = []
    if b.get('maxclass') == 'newobj':
        toks = str(b.get('text', '')).split()
        if len(toks) > 1 and toks[0] in SENDERS and SENDERS[toks[0]] != 'forward':
            out.append((SENDERS[toks[0]], toks[1]))
    elif b.get('maxclass') == 'message':
        segs = str(b.get('text', '')).split(';')[1:]
        for s in segs:
            t = s.split()
            if t:
                out.append(('send', t[0]))
    return out


def _receives(b):
    if b.get('maxclass') == 'newobj':
        toks = str(b.get('text', '')).split()
        if len(toks) > 1 and toks[0] in RECEIVERS:
            return [(RECEIVERS[toks[0]], toks[1])]
    return []


def _name_index(doc: Document):
    snd, rcv = {}, {}
    for name, sub, p in all_patchers(doc):
        for b in boxes(p):
            for ch, n in _sends(b):
                snd.setdefault((ch, n), []).append((name, sub, b))
            for ch, n in _receives(b):
                rcv.setdefault((ch, n), []).append((name, sub, b))
    return snd, rcv


def named_links(doc: Document, b):
    snd, rcv = _name_index(doc)
    out = []
    for ch, n in _sends(b):
        out.append({'sends_to': '%s %s' % (ch, n),
                    'receivers': ['%s :: %s' % (f, join(s, x['id'])) for f, s, x in rcv.get((ch, n), [])
                                  if x is not b]})
    for ch, n in _receives(b):
        out.append({'receives': '%s %s' % (ch, n),
                    'senders': ['%s :: %s' % (f, join(s, x['id'])) for f, s, x in snd.get((ch, n), [])
                                if x is not b]})
    return out


# ----------------------------------------------------------------- trace ----
def _io_box(p, cls, index1):
    """inlet/outlet box with 1-based index (falls back to x-order)."""
    cand = [b for b in boxes(p) if b.get('maxclass') == cls]
    for b in cand:
        if b.get('index') == index1:
            return b
    cand.sort(key=lambda b: (b.get('patching_rect') or [0])[0])
    return cand[index1 - 1] if 0 < index1 <= len(cand) else None


def _child_loc(doc, file, sub, b):
    if isinstance(b.get('patcher'), dict):
        return (doc, doc.resolve_name(file), sub + [b['id']])
    r = patch_ref(doc, b, os.path.dirname(doc.path))
    if r:
        try:
            d2, f2 = open_target(doc, r[1], r[2])
            return (d2, d2.resolve_name(f2), [])
        except Exception:
            return None
    return None


def _parents_of_file(root: Document, d: Document, f: str):
    """Boxes that instantiate file f of doc d (anywhere in root's tree)."""
    out = []
    for n in instance_tree(root):
        if n['doc'] is d and n['file'] == f and not n['sub'] and n['depth']:
            parent = n['instance'].split(SEP)
            # find the parent node
            for m in instance_tree(root):
                if m['instance'] == SEP.join(parent[:-1]):
                    out.append((m['doc'], m['file'], m['sub'], parent[-1], n['instance']))
                    break
    return out


def trace(doc: Document, file, box_path, direction='out', depth=4, port=None, follow_names=True, max_hops=150):
    """Follow patch cords (and named send/receive) from a box."""
    fname = doc.resolve_name(file)
    p0, b0, sub0 = get_box(doc, file, box_path)
    snd, rcv = _name_index(doc) if follow_names else ({}, {})
    hops, seen = [], set()
    queue = [(doc, fname, sub0, b0['id'], port, 0)]
    fwd = direction != 'in'
    truncated = False

    def loc(d, f, s):
        base = f if d is doc else os.path.basename(d.path)
        return base + (' :: ' + join(s) if s else '')

    def push(d, f, s, bid, prt, dep):
        queue.append((d, f, s, bid, prt, dep))

    while queue:
        d, f, s, bid, prt, dep = queue.pop(0)
        key = (d.path, f, tuple(s), bid, prt)
        if key in seen or dep >= depth:
            continue
        seen.add(key)
        p = get_patcher(d, f, s)
        m = box_map(p)
        b = m.get(bid)
        if b is None:
            continue
        here = loc(d, f, s)

        def hop(kind, frm, to, where_=None):
            hops.append({'depth': dep + 1, 'edge': kind, 'from': frm, 'to': to, 'where': where_ or here})

        # cords
        for l in p.get('lines', []):
            src, dst = l['patchline']['source'], l['patchline']['destination']
            mine, other = (src, dst) if fwd else (dst, src)
            if mine[0] != bid or (prt is not None and mine[1] != prt):
                continue
            ob = m.get(other[0])
            if ob is None:
                hop('BROKEN line', label(b), other[0] + ' (missing)')
                continue
            if fwd:
                hop('out %d -> in %d' % (mine[1], other[1]), label(b), label(ob))
            else:
                hop('in %d <- out %d' % (mine[1], other[1]), label(b), label(ob))
            child = _child_loc(d, f, s, ob)
            if child:
                cd, cf, cs = child
                cp = get_patcher(cd, cf, cs)
                io = _io_box(cp, 'inlet' if fwd else 'outlet', other[1] + 1)
                if io is not None:
                    hop('enter ' + ('inlet %d' % (other[1] + 1) if fwd else 'outlet %d' % (other[1] + 1)),
                        label(ob), label(io), loc(cd, cf, cs))
                    push(cd, cf, cs, io['id'], None, dep + 1)
                continue
            if ob.get('maxclass') == ('outlet' if fwd else 'inlet'):
                idx = ob.get('index', 1) - 1
                if s:      # embedded subpatcher: parent box known
                    pd, pf, ps, pb = d, f, s[:-1], s[-1]
                    pm = box_map(get_patcher(pd, pf, ps))
                    hop('leave via %s %d' % ('outlet' if fwd else 'inlet', idx + 1), label(ob),
                        label(pm[pb]), loc(pd, pf, ps))
                    push(pd, pf, ps, pb, idx, dep + 1)
                else:
                    parents = _parents_of_file(doc, d, f)
                    for pd, pf, ps, pb, inst in parents:
                        pm = box_map(get_patcher(pd, pf, ps))
                        hop('leave file via %s %d (instance %s)' % ('outlet' if fwd else 'inlet', idx + 1, inst),
                            label(ob), label(pm[pb]) if pb in pm else pb, loc(pd, pf, ps))
                        push(pd, pf, ps, pb, idx, dep + 1)
                    if not parents:
                        hop('leave file (no instances found)', label(ob), '?')
                continue
            push(d, f, s, ob['id'], None, dep + 1)
        # named send/receive
        if follow_names and d is doc:
            pairs = _sends(b) if fwd else _receives(b)
            for ch, n in pairs:
                peers = rcv.get((ch, n), []) if fwd else snd.get((ch, n), [])
                for pf, ps, pb in peers:
                    if pb is b:
                        continue
                    hop('%s %s' % ('send' if fwd else 'receive from', n), label(b), label(pb), loc(doc, pf, ps))
                    push(doc, pf, ps, pb['id'], None, dep + 1)
        if len(hops) >= max_hops:
            truncated = True
            break
    return {'start': label(b0), 'where': loc(doc, fname, sub0), 'direction': direction,
            'hops': hops[:max_hops], 'truncated': truncated}


# ---------------------------------------------------------- presentation ----
def presentation(doc: Document, file, sub, depth=1):
    p = get_patcher(doc, file, sub)
    items = []
    for b in boxes(p):
        if not b.get('presentation'):
            continue
        r = b.get('presentation_rect') or b.get('patching_rect')
        it = {'id': b['id'], 'class': b.get('maxclass'), 'what': label(b, 40), 'rect': [round(x, 1) for x in r]}
        if b.get('hidden'):
            it['hidden'] = 1
        if b.get('background'):
            it['background'] = 1
        if b.get('maxclass') == 'bpatcher':
            child = _child_loc(doc, file, split_path(sub), b)
            if child:
                cd, cf, cs = child
                it['shows'] = cf + (' :: ' + join(cs) if cs else '')
                if b.get('offset'):
                    it['offset'] = b['offset']
                if depth > 1:
                    it['contents'] = presentation(cd, cf, cs, depth - 1)['items']
        items.append(it)
    items.sort(key=lambda x: (x['rect'][1], x['rect'][0]))
    res = {'where': where(doc, file, split_path(sub)), 'items': items}
    ctl = [x for x in items if not x.get('background') and not x.get('hidden') and x['class'] not in DECOR]
    ov = []
    for i, a in enumerate(ctl):
        for c in ctl[i + 1:]:
            if _overlap(a['rect'], c['rect']):
                ov.append('%s overlaps %s' % (a['what'], c['what']))
    if ov:
        res['overlaps'] = ov
    if not split_path(sub) and doc.kind != 'maxpat' and doc.resolve_name(file) == doc.top_name:
        w = p.get('devicewidth') or 0
        res['device_size'] = [w, 169]
        if w:
            out = [x['what'] for x in items if x['rect'][0] + x['rect'][2] > w + 0.5 or x['rect'][1] + x['rect'][3] > 169.5]
            if out:
                res['outside_device'] = out
    return res


def _overlap(a, b):
    return a[0] < b[0] + b[2] - 0.5 and b[0] < a[0] + a[2] - 0.5 and a[1] < b[1] + b[3] - 0.5 and b[1] < a[1] + a[3] - 0.5


# ------------------------------------------------------------ parameters ----
def registry(doc: Document):
    """The device's Live parameter registry (top-level ``parameters`` dict)."""
    if doc.kind == 'maxpat':
        return None
    p = doc.json(doc.top_name)['patcher']
    return p.setdefault('parameters', {})


def parameter_report(doc: Document):
    reg = registry(doc) or {}
    found = {}
    for n in instance_tree(doc):
        d, f, s = n['doc'], n['file'], n['sub']
        for b in boxes(get_patcher(d, f, s)):
            if is_param(b):
                key = join(n['instance'], b['id'])
                vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
                found[key] = {'key': key, 'box': label(b, 40),
                              'file': f if d is doc else d.path,
                              'longname': vo.get('parameter_longname'),
                              'shortname': vo.get('parameter_shortname'),
                              'type': vo.get('parameter_type'),
                              'invisible': vo.get('parameter_invisible', 0),
                              'order': vo.get('parameter_order', 0),
                              'varname': b.get('varname')}
    regd = {k: v for k, v in reg.items() if isinstance(v, list)}
    out = []
    for k, it in found.items():
        if k in regd:
            it['registered_as'] = regd[k][0]
        else:
            it['registered_as'] = None
        out.append(it)
    stale = sorted(k for k in regd if k not in found)
    names = {}
    for k, v in regd.items():
        names.setdefault(v[0], []).append(k)
    dup = {n: ks for n, ks in names.items() if len(ks) > 1}
    return {'parameters': out, 'unregistered': [x['key'] for x in out if x['registered_as'] is None],
            'stale_registry_keys': stale, 'duplicate_names': dup,
            'banks': reg.get('parameterbanks'), 'has_registry': doc.kind != 'maxpat'}


def _free_name(base, used):
    if base not in used:
        return base
    m = re.match(r'^(.*)\[(\d+)\]$', base)
    stem = m.group(1) if m else base
    n = 1
    while '%s[%d]' % (stem, n) in used:
        n += 1
    return '%s[%d]' % (stem, n)


def register_param(doc: Document, file, sub, b, allow_rename=False):
    reg = registry(doc)
    if reg is None:
        return ['%s is a plain patcher file: its parameters are registered by each device that uses it '
                'when that device is saved in Max' % os.path.basename(doc.path)]
    vo = b.setdefault('saved_attribute_attributes', {}).setdefault('valueof', {})
    base = vo.get('parameter_longname')
    if not base:
        raise ValueError('%s: parameter_enable=1 but no parameter_longname' % label(b))
    short = vo.get('parameter_shortname', base)
    prefixes = instance_prefixes(doc, file, sub)
    if not prefixes:
        return ['%s is not instantiated anywhere in the device; nothing registered' % where(doc, file, sub)]
    used = {v[0] for k, v in reg.items() if isinstance(v, list)}
    notes = []
    for pre in prefixes:
        key = join(pre, b['id'])
        if key in reg and isinstance(reg[key], list):
            used.discard(reg[key][0])
        name = _free_name(base, used)
        if name != base and len(prefixes) == 1 and not allow_rename:
            raise ValueError('parameter longname %r is already used in this device; choose another '
                             '(Live identifies parameters by longname)' % base)
        reg[key] = [name, short, 0]
        used.add(name)
        if name != base:
            reg.setdefault('parameter_overrides', {})[key] = {'parameter_longname': name}
            notes.append('%s registered as %s' % (key, name))
        else:
            (reg.get('parameter_overrides') or {}).pop(key, None)
    doc.mark_dirty(doc.top_name)
    notes.append('registered %d instance(s): %s' % (len(prefixes), ', '.join(join(x, b['id']) for x in prefixes)))
    return notes


def unregister_params(doc: Document, file, sub, box_ids):
    reg = registry(doc)
    if reg is None:
        return 0
    ov = reg.get('parameter_overrides') or {}
    n = 0
    for pre in instance_prefixes(doc, file, sub):
        for bid in box_ids:
            k = join(pre, bid)
            for key in [x for x in list(reg) if x == k or x.startswith(k + SEP)]:
                if isinstance(reg[key], list):
                    del reg[key]
                    n += 1
            for key in [x for x in list(ov) if x == k or x.startswith(k + SEP)]:
                del ov[key]
    if n:
        doc.mark_dirty(doc.top_name)
    return n


# -------------------------------------------------------------- io infer ----
def infer_io(doc: Document, maxclass, text):
    """{numinlets, numoutlets, outlettype, source} or None."""
    if maxclass == 'newobj':
        toks = str(text or '').split()
        if toks and toks[0] not in ('p', 'patcher'):
            r = resolve_file(doc, toks[0] + '.maxpat')
            if r:
                try:
                    d2, f2 = open_target(doc, r[0], r[1])
                    p2 = d2.json(f2)['patcher']
                    ni = len([b for b in boxes(p2) if b.get('maxclass') == 'inlet'])
                    no = len([b for b in boxes(p2) if b.get('maxclass') == 'outlet'])
                    return {'numinlets': ni, 'numoutlets': no, 'outlettype': [''] * no,
                            'source': 'abstraction %s (%s)' % (toks[0], r[0])}
                except Exception:
                    pass
    r = objdb.lookup(maxclass, text or '')
    if r:
        return r
    # same text elsewhere in this document
    for name, sub, p in all_patchers(doc):
        for b in boxes(p):
            if b.get('maxclass') == maxclass and (maxclass != 'newobj' or b.get('text') == text):
                return {'numinlets': b.get('numinlets', 0), 'numoutlets': b.get('numoutlets', 0),
                        'outlettype': b.get('outlettype', []), 'source': 'same object in this device'}
    return None


def _subpatcher_io(pp):
    return (len([b for b in boxes(pp) if b.get('maxclass') == 'inlet']),
            len([b for b in boxes(pp) if b.get('maxclass') == 'outlet']))


# ------------------------------------------------------------- validate -----
def validate(doc: Document, check_io=True):
    errors, warnings, info = [], [], []
    for name in doc.json_entries():
        try:
            j = doc.json(name)
        except Exception as ex:
            errors.append('%s: JSON does not parse: %s' % (name, ex))
            continue
        if not isinstance(j, dict) or 'patcher' not in j:
            continue
    for name, sub, p in all_patchers(doc):
        w = name + (' :: ' + join(sub) if sub else '')
        ids = [b.get('id') for b in boxes(p)]
        dups = {i for i in ids if ids.count(i) > 1}
        if dups:
            errors.append('%s: duplicate box ids %s' % (w, sorted(dups)))
        m = box_map(p)
        seen_lines = set()
        for l in p.get('lines', []):
            s, d = l['patchline']['source'], l['patchline']['destination']
            t = (tuple(s), tuple(d))
            if t in seen_lines:
                warnings.append('%s: duplicate line %s:%d -> %s:%d' % (w, s[0], s[1], d[0], d[1]))
            seen_lines.add(t)
            if s[0] not in m or d[0] not in m:
                errors.append('%s: line %s:%d -> %s:%d references a missing box' % (w, s[0], s[1], d[0], d[1]))
                continue
            if s[1] >= m[s[0]].get('numoutlets', 0):
                errors.append('%s: line from %s outlet %d but it has %d outlets' % (
                    w, label(m[s[0]]), s[1], m[s[0]].get('numoutlets', 0)))
            if d[1] >= m[d[0]].get('numinlets', 0):
                errors.append('%s: line into %s inlet %d but it has %d inlets' % (
                    w, label(m[d[0]]), d[1], m[d[0]].get('numinlets', 0)))
        for b in boxes(p):
            no = b.get('numoutlets', 0)
            if 'outlettype' in b and len(b['outlettype']) != no:
                warnings.append('%s: %s has %d outlets but %d outlettypes' % (w, label(b), no, len(b['outlettype'])))
            if is_param(b):
                vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
                if not vo.get('parameter_longname'):
                    errors.append('%s: %s is a parameter without parameter_longname' % (w, label(b)))
            if isinstance(b.get('patcher'), dict) and b.get('maxclass') == 'newobj':
                ni, no2 = _subpatcher_io(b['patcher'])
                if (ni, no2) != (b.get('numinlets'), b.get('numoutlets')):
                    warnings.append('%s: subpatcher %s has %d inlet/%d outlet objects inside but box says %s/%s' % (
                        w, label(b), ni, no2, b.get('numinlets'), b.get('numoutlets')))
            elif check_io and b.get('maxclass') == 'newobj':
                r = objdb.lookup('newobj', b.get('text', ''))
                if r and r['confidence'] >= 0.97 and r.get('seen', 99) >= 20 and \
                        (r['numinlets'], r['numoutlets']) != (b.get('numinlets'), b.get('numoutlets')):
                    warnings.append('%s: %s has %s/%s in/outlets; Max usually gives this object %d/%d (%s)' % (
                        w, label(b), b.get('numinlets'), b.get('numoutlets'), r['numinlets'], r['numoutlets'],
                        r['source']))
            for kind, fn in file_refs(b):
                if kind == 'abstraction?':
                    continue
                if not resolve_file(doc, fn):
                    warnings.append(
                        '%s: %s refers to %s %r which is neither embedded nor on the search path' % (
                            w, label(b), kind, fn))
    if doc.kind != 'maxpat':
        try:
            pr = parameter_report(doc)
            for k in pr['unregistered']:
                warnings.append('parameter %s is not in the device parameter registry (Max adds it on the next '
                                'save from Max; patch_add_box registers automatically)' % k)
            for k in pr['stale_registry_keys']:
                warnings.append('parameter registry key %s points to no parameter box' % k)
            for n, ks in pr['duplicate_names'].items():
                errors.append('parameter longname %r used by %s' % (n, ks))
        except Exception as ex:
            warnings.append('parameter check failed: %s' % ex)
    if doc.kind == 'amxd-frozen':
        dc = doc.json(doc.top_name)['patcher'].get('dependency_cache') or []
        names = {n.lower() for n in doc.names()}
        miss = [d.get('name') for d in dc if d.get('name', '').lower() not in names]
        if miss:
            info.append('dependency_cache lists files that are not embedded (resolved from disk at load): %s'
                        % ', '.join(miss))
    if not doc.roundtrip_ok:
        errors.append('container did not rebuild byte-identically when opened; saving is disabled')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'info': info}


# ---------------------------------------------------------------- edits -----
def _next_id(p):
    n = 0
    for b in boxes(p):
        m = re.match(r'obj-(\d+)$', str(b.get('id')))
        if m:
            n = max(n, int(m.group(1)))
    return 'obj-%d' % (n + 1)


def _set_dotted(d, key, value):
    parts = key.split('.')
    for k in parts[:-1]:
        d = d.setdefault(k, {})
    d[parts[-1]] = value


def _del_dotted(d, key):
    parts = key.split('.')
    for k in parts[:-1]:
        d = d.get(k, {})
    d.pop(parts[-1], None)


PARAM_KEYS = {'longname', 'shortname', 'type', 'mmin', 'mmax', 'enum', 'initial', 'initial_enable', 'unitstyle',
              'units', 'exponent', 'steps', 'modmode', 'invisible', 'linknames', 'annotation_name', 'info',
              'order', 'defer', 'speedlim', 'osc_name'}


def _param_attrs(parameter: dict):
    out = {}
    for k, v in (parameter or {}).items():
        k2 = k if k.startswith('parameter_') else 'parameter_' + k
        out[k2] = v
    if 'parameter_mmax' not in out and 'parameter_enum' in out:
        out['parameter_mmax'] = len(out['parameter_enum']) - 1
    return out


def add_box(doc: Document, file=None, patcher=None, maxclass='newobj', text=None, x=None, y=None,
            width=None, height=None, numinlets=None, numoutlets=None, outlettype=None, attrs=None,
            id=None, presentation_rect=None, parameter=None, allow_rename=False):
    fname = doc.resolve_name(file)
    sub = split_path(patcher)
    p = get_patcher(doc, fname, sub)
    attrs = dict(attrs or {})
    notes = []
    bid = id or _next_id(p)
    if bid in box_map(p):
        raise ValueError('box id %s already exists in %s' % (bid, where(doc, fname, sub)))
    toks = str(text or '').split()
    if maxclass == 'newobj' and toks and toks[0] in ('p', 'patcher') and 'patcher' not in attrs:
        attrs['patcher'] = {'fileversion': 1, 'rect': [100.0, 100.0, 640.0, 480.0], 'boxes': [], 'lines': []}
    if numinlets is None or numoutlets is None:
        if maxclass == 'newobj' and isinstance(attrs.get('patcher'), dict):
            ni, no = _subpatcher_io(attrs['patcher'])
            io = {'numinlets': ni, 'numoutlets': no, 'outlettype': [''] * no, 'source': 'subpatcher contents'}
        else:
            io = infer_io(doc, maxclass, text)
        if io is None:
            raise ValueError('cannot infer inlet/outlet counts for %s %r; pass numinlets, numoutlets and '
                             'outlettype explicitly (check the object reference)' % (maxclass, text))
        numinlets = io['numinlets'] if numinlets is None else numinlets
        numoutlets = io['numoutlets'] if numoutlets is None else numoutlets
        if outlettype is None:
            outlettype = io['outlettype']
        notes.append('in/outlets %d/%d from %s' % (numinlets, numoutlets, io['source']))
    if outlettype is None:
        outlettype = [''] * numoutlets
    if len(outlettype) != numoutlets:
        raise ValueError('outlettype has %d entries but numoutlets is %d' % (len(outlettype), numoutlets))
    if x is None or y is None:
        ys = [(b.get('patching_rect') or [0, 0, 0, 0]) for b in boxes(p)]
        x = 20.0 if x is None else x
        y = (max([r[1] + r[3] for r in ys] or [0]) + 30.0) if y is None else y
    if width is None:
        width = max(30.0, 7.0 * len(text or '') + 12) if maxclass in ('newobj', 'message', 'comment') else 48.0
    if height is None:
        height = 22.0 if maxclass in ('newobj', 'message', 'comment') else 20.0
    b = {'id': bid, 'maxclass': maxclass, 'numinlets': numinlets, 'numoutlets': numoutlets,
         'patching_rect': [float(x), float(y), float(width), float(height)]}
    if numoutlets:
        b['outlettype'] = list(outlettype)
    if text is not None:
        b['text'] = text
    if presentation_rect is not None:
        b['presentation'] = 1
        b['presentation_rect'] = [float(v) for v in presentation_rect]
    is_param_class = maxclass != 'newobj' and objdb.valueof_template(maxclass) is not None
    newobj_param = maxclass == 'newobj' and re.search(r'@parameter_enable\s+1\b', text or '')
    if parameter is not None or newobj_param or (is_param_class and attrs.get('parameter_enable', 1)):
        if parameter is None:
            raise ValueError('%s is a Live parameter object: pass parameter={"longname": ..., "shortname": ...} '
                             '(or attrs={"parameter_enable": 0})' % (maxclass if not newobj_param else text))
        if maxclass == 'newobj':
            # non-UI objects (dict, coll, pattr, ...): hidden blob parameter by default
            vo = {'parameter_invisible': 1, 'parameter_modmode': 0, 'parameter_type': 3}
            b.setdefault('saved_object_attributes', {})['parameter_enable'] = 1
            if not newobj_param:
                b['text'] = (text or '') + ' @parameter_enable 1'
        else:
            vo = objdb.valueof_template(maxclass) or {}
            b['parameter_enable'] = 1
        for k in ('parameter_longname', 'parameter_shortname', 'parameter_annotation_name', 'parameter_info'):
            vo.pop(k, None)
        vo.update(_param_attrs(parameter))
        if 'parameter_shortname' not in vo:
            vo['parameter_shortname'] = vo.get('parameter_longname', '')
        b.setdefault('saved_attribute_attributes', {})['valueof'] = vo
        b['varname'] = attrs.pop('varname', vo.get('parameter_longname'))
    for k, v in attrs.items():
        if '.' in k:
            _set_dotted(b, k, v)
        else:
            b[k] = v
    p.setdefault('boxes', []).append({'box': b})
    doc.mark_dirty(fname)
    if is_param(b):
        try:
            notes += register_param(doc, fname, sub, b, allow_rename)
        except Exception:
            p['boxes'].pop()
            raise
    return {'id': bid, 'box': join(sub, bid), 'notes': notes}


def set_attrs(doc: Document, file, box_path, set=None, unset=None, allow_rename=False):
    fname = doc.resolve_name(file)
    p, b, sub = get_box(doc, fname, box_path)
    notes = []
    set = dict(set or {})
    if 'id' in set:
        raise ValueError('box ids cannot be changed (lines and the parameter registry refer to them)')
    old_param = is_param(b) and dict((b.get('saved_attribute_attributes') or {}).get('valueof') or {})
    if 'parameter' in set:
        for k, v in _param_attrs(set.pop('parameter')).items():
            set['saved_attribute_attributes.valueof.' + k] = v
    for k, v in set.items():
        if '.' in k:
            _set_dotted(b, k, v)
        else:
            b[k] = v
    for k in unset or []:
        _del_dotted(b, k) if '.' in k else b.pop(k, None)
    if 'presentation_rect' in set and 'presentation' not in set:
        b['presentation'] = 1
    if 'text' in set and b.get('maxclass') == 'newobj' and not isinstance(b.get('patcher'), dict) \
            and 'numinlets' not in set and 'numoutlets' not in set:
        io = infer_io(doc, 'newobj', b['text'])
        if io:
            b['numinlets'], b['numoutlets'] = io['numinlets'], io['numoutlets']
            if io['numoutlets']:
                b['outlettype'] = io['outlettype']
            else:
                b.pop('outlettype', None)
            notes.append('in/outlets now %d/%d (%s)' % (io['numinlets'], io['numoutlets'], io['source']))
        else:
            notes.append('could not infer in/outlets for the new text; counts unchanged')
        bad = [l for l in p.get('lines', [])
               if (l['patchline']['source'][0] == b['id'] and l['patchline']['source'][1] >= b['numoutlets']) or
               (l['patchline']['destination'][0] == b['id'] and l['patchline']['destination'][1] >= b['numinlets'])]
        if bad:
            raise ValueError('new text leaves %d line(s) on in/outlets that no longer exist: %s; '
                             'disconnect them first' % (len(bad), [l['patchline'] for l in bad]))
    doc.mark_dirty(fname)
    new_param = is_param(b) and (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
    if old_param and not is_param(b):
        unregister_params(doc, fname, sub, [b['id']])
        notes.append('parameter unregistered')
    elif is_param(b) and (not old_param or
                                        old_param.get('parameter_longname') != new_param.get('parameter_longname') or
                                        old_param.get('parameter_shortname') != new_param.get('parameter_shortname')):
        notes += register_param(doc, fname, sub, b, allow_rename)
    return {'box': summary(b, doc), 'notes': notes}


def remove_box(doc: Document, file, box_path):
    fname = doc.resolve_name(file)
    p, b, sub = get_box(doc, fname, box_path)
    n_lines = len(p.get('lines', []))
    p['lines'] = [l for l in p.get('lines', []) if b['id'] not in (l['patchline']['source'][0],
                                                                  l['patchline']['destination'][0])]
    p['boxes'] = [x for x in p['boxes'] if x['box'] is not b]
    n_par = unregister_params(doc, fname, sub, [b['id']])
    doc.mark_dirty(fname)
    return {'removed': label(b), 'lines_removed': n_lines - len(p['lines']), 'parameters_unregistered': n_par}


def _line_ends(doc, fname, src, dst):
    s_ids, d_ids = split_path(src), split_path(dst)
    if s_ids[:-1] != d_ids[:-1]:
        raise ValueError('source and destination must be in the same patcher (%s vs %s)' % (src, dst))
    p = get_patcher(doc, fname, s_ids[:-1])
    m = box_map(p)
    for i in (s_ids[-1], d_ids[-1]):
        if i not in m:
            raise KeyError('no box %s in %s' % (i, where(doc, fname, s_ids[:-1])))
    return p, m, s_ids[-1], d_ids[-1], s_ids[:-1]


def connect(doc: Document, file, src, outlet, dst, inlet):
    fname = doc.resolve_name(file)
    p, m, s, d, sub = _line_ends(doc, fname, src, dst)
    if not 0 <= outlet < m[s].get('numoutlets', 0):
        raise ValueError('%s has %d outlets; outlet %d does not exist' % (label(m[s]), m[s].get('numoutlets', 0), outlet))
    if not 0 <= inlet < m[d].get('numinlets', 0):
        raise ValueError('%s has %d inlets; inlet %d does not exist' % (label(m[d]), m[d].get('numinlets', 0), inlet))
    for l in p.get('lines', []):
        if l['patchline']['source'] == [s, outlet] and l['patchline']['destination'] == [d, inlet]:
            return {'line': '%s:%d -> %s:%d' % (s, outlet, d, inlet), 'notes': ['already connected']}
    p.setdefault('lines', []).append({'patchline': {'source': [s, outlet], 'destination': [d, inlet]}})
    doc.mark_dirty(fname)
    return {'line': '%s:%d -> %s:%d' % (label(m[s], 40), outlet, label(m[d], 40), inlet)}


def disconnect(doc: Document, file, src, outlet=None, dst=None, inlet=None):
    fname = doc.resolve_name(file)
    s_ids = split_path(src)
    p = get_patcher(doc, fname, s_ids[:-1])
    s = s_ids[-1]
    d = split_path(dst)[-1] if dst else None
    keep, gone = [], []
    for l in p.get('lines', []):
        ls, ld = l['patchline']['source'], l['patchline']['destination']
        if ls[0] == s and (outlet is None or ls[1] == outlet) and (d is None or ld[0] == d) and \
                (inlet is None or ld[1] == inlet):
            gone.append('%s:%d -> %s:%d' % (ls[0], ls[1], ld[0], ld[1]))
        else:
            keep.append(l)
    if not gone:
        raise ValueError('no matching line from %s' % src)
    p['lines'] = keep
    doc.mark_dirty(fname)
    return {'removed': gone}


def _subst(v, names):
    if isinstance(v, str):
        return re.sub(r'\$(\w+)', lambda m_: names.get(m_.group(1), m_.group(0)), v)
    if isinstance(v, list):
        return [_subst(x, names) for x in v]
    if isinstance(v, dict):
        return {k: _subst(x, names) for k, x in v.items()}
    return v


def apply_ops(doc: Document, file, ops):
    """All-or-nothing list of edits. add_box ops may carry "as": "name"; later
    ops can then use "$name" anywhere a box id/path is expected."""
    snap = doc.snapshot()
    names, results = {}, []
    try:
        for i, op in enumerate(ops):
            op = dict(op)
            kind = op.pop('op', None)
            alias = op.pop('as', None)
            op = _subst(op, names)
            f = op.pop('file', file)
            if kind == 'add_box':
                r = add_box(doc, f, **op)
                if alias:
                    names[alias] = r['box']
            elif kind == 'set':
                r = set_attrs(doc, f, op.pop('box'), **op)
            elif kind == 'remove_box':
                r = remove_box(doc, f, op['box'])
            elif kind == 'connect':
                r = connect(doc, f, op['src'], int(op.get('outlet', 0)), op['dst'], int(op.get('inlet', 0)))
            elif kind == 'disconnect':
                r = disconnect(doc, f, op['src'], op.get('outlet'), op.get('dst'), op.get('inlet'))
            else:
                raise ValueError('unknown op %r (add_box, set, remove_box, connect, disconnect)' % kind)
            results.append({'op': kind, **r})
    except Exception as ex:
        doc.restore(snap)
        raise type(ex)('op #%d failed, all ops rolled back: %s' % (i, ex)) from ex
    return {'results': results, 'aliases': names}


# ------------------------------------------------------------------ diff ----
def _flatten(p, sub, out_boxes, out_lines):
    for b in boxes(p):
        key = join(sub, b['id'])
        out_boxes[key] = b
        if isinstance(b.get('patcher'), dict):
            _flatten(b['patcher'], sub + [b['id']], out_boxes, out_lines)
    for l in p.get('lines', []):
        s, d = l['patchline']['source'], l['patchline']['destination']
        out_lines.add('%s%s:%d -> %s:%d' % (join(sub) + SEP if sub else '', s[0], s[1], d[0], d[1]))


def diff_json(a, b):
    ba, la, bb, lb = {}, set(), {}, set()
    _flatten(a.get('patcher', {}), [], ba, la)
    _flatten(b.get('patcher', {}), [], bb, lb)
    out = []
    for k in sorted(set(bb) - set(ba)):
        out.append('+ box %s' % (join(k.split(SEP)[:-1]) + SEP if SEP in k else '') + label(bb[k]))
    for k in sorted(set(ba) - set(bb)):
        out.append('- box %s' % (join(k.split(SEP)[:-1]) + SEP if SEP in k else '') + label(ba[k]))
    for k in sorted(set(ba) & set(bb)):
        x, y = dict(ba[k]), dict(bb[k])
        x.pop('patcher', None)
        y.pop('patcher', None)
        if x != y:
            keys = sorted(kk for kk in set(x) | set(y) if x.get(kk) != y.get(kk))
            det = []
            for kk in keys:
                vx, vy = x.get(kk), y.get(kk)
                if len(repr(vx)) + len(repr(vy)) < 160:
                    det.append('%s: %r -> %r' % (kk, vx, vy))
                else:
                    det.append(kk)
            out.append('~ box %s: %s' % (k, '; '.join(det)))
    for l in sorted(lb - la):
        out.append('+ line ' + l)
    for l in sorted(la - lb):
        out.append('- line ' + l)
    pa = {k: v for k, v in a.get('patcher', {}).items() if k not in ('boxes', 'lines')}
    pb = {k: v for k, v in b.get('patcher', {}).items() if k not in ('boxes', 'lines')}
    for k in sorted(set(pa) | set(pb)):
        if pa.get(k) != pb.get(k):
            if k == 'parameters':
                ra, rb = pa.get(k) or {}, pb.get(k) or {}
                for kk in sorted(set(ra) | set(rb)):
                    if ra.get(kk) != rb.get(kk):
                        out.append('~ parameters[%s]: %s -> %s' % (kk, _short(ra.get(kk)), _short(rb.get(kk))))
            else:
                out.append('~ patcher.%s changed' % k)
    other = {k: v for k, v in a.items() if k != 'patcher'}, {k: v for k, v in b.items() if k != 'patcher'}
    if other[0] != other[1]:
        out.append('~ top-level keys changed')
    return out


def _short(v):
    s = repr(v)
    return s if len(s) < 120 else s[:117] + '...'
