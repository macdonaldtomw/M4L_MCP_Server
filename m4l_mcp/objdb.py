"""Inlet/outlet counts of Max objects, learned from patches Max itself saved.

Max fixes up ``numinlets``/``numoutlets`` when it loads a patch, but patch
lines to outlets that do not exist are dropped, so an editor has to know the
real counts to validate connections. Instead of a hand-written table, this
scans the patches shipped with Max and Live (thousands of them, saved by Max)
and records, per object, the most common (numinlets, numoutlets, outlettype):

* ``T:<exact text>``   when it disagrees with the arg-count majority
* ``N:<class>/<nargs>`` object class + number of arguments
* ``C:<class>``         object class alone
* ``M:<maxclass>``      UI objects (live.text, message, comment, ...)

plus one example ``valueof`` (parameter attributes) per live.* maxclass.
The index is cached in ``~/.m4l-mcp/objdb.json``; rebuild with
``python -m m4l_mcp.objdb --build``.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time

from . import container as C

CACHE = os.environ.get('M4L_MCP_OBJDB') or os.path.join(os.path.expanduser('~'), '.m4l-mcp', 'objdb.json')


def default_roots():
    env = os.environ.get('M4L_MCP_CORPUS')
    if env:
        return [p for p in env.split(os.pathsep) if p]
    cands = [
        r"C:\Program Files\Cycling '74\Max 9\resources",
        r"C:\Program Files\Cycling '74\Max 8\resources",
    ]
    pd = r'C:\ProgramData\Ableton'
    if os.path.isdir(pd):
        for live in sorted(os.listdir(pd)):
            if live.startswith('Live'):
                cands.append(os.path.join(pd, live, 'Resources', 'Core Library'))
                cands.append(os.path.join(pd, live, 'Resources', 'Max', 'resources'))
    return [p for p in cands if os.path.isdir(p)]


def _walk_patchers(p, out):
    for b in p.get('boxes', []):
        box = b.get('box', {})
        out.append(box)
        if isinstance(box.get('patcher'), dict):
            _walk_patchers(box['patcher'], out)


def _keys(box):
    mc = box.get('maxclass', '')
    if mc != 'newobj':
        return ['M:' + mc]
    toks = str(box.get('text', '')).split()
    if not toks:
        return []
    cls = toks[0]
    ks = ['N:%s/%d' % (cls, len(toks) - 1), 'C:' + cls]
    t = ' '.join(toks)
    if len(t) <= 80:
        ks.insert(0, 'T:' + t)
    return ks


def build(roots=None, verbose=False):
    roots = roots or default_roots()
    cnt = collections.defaultdict(collections.Counter)
    valueof = {}
    vo_count = collections.defaultdict(collections.Counter)
    nfiles = 0
    t0 = time.time()
    for root in roots:
        for dp, dn, fn in os.walk(root):
            for f in fn:
                if not f.lower().endswith(('.maxpat', '.maxhelp', '.amxd')):
                    continue
                path = os.path.join(dp, f)
                try:
                    c = C.parse(open(path, 'rb').read(), path)
                except Exception:
                    continue
                for e in c.entries:
                    if not e.is_json():
                        continue
                    try:
                        end = e.data.rfind(b'}')
                        j = json.loads(e.data[:end + 1].decode('utf-8'))
                    except Exception:
                        continue
                    if not isinstance(j, dict) or not isinstance(j.get('patcher'), dict):
                        continue
                    boxes = []
                    _walk_patchers(j['patcher'], boxes)
                    for box in boxes:
                        try:
                            sig = (int(box.get('numinlets', 0)), int(box.get('numoutlets', 0)),
                                   tuple(box.get('outlettype') or ()))
                        except Exception:
                            continue
                        for k in _keys(box):
                            cnt[k][sig] += 1
                        vo = (box.get('saved_attribute_attributes') or {}).get('valueof')
                        if box.get('parameter_enable') and isinstance(vo, dict):
                            mc = box.get('maxclass', '')
                            pt = vo.get('parameter_type')
                            vo_count[mc][pt] += 1
                            valueof.setdefault((mc, pt), vo)
                nfiles += 1
        if verbose:
            print('scanned', root, nfiles, 'files', round(time.time() - t0, 1), 's', file=sys.stderr)
    table = {}
    for k, c in cnt.items():
        (sig, n), tot = c.most_common(1)[0], sum(c.values())
        table[k] = [sig[0], sig[1], list(sig[2]), n, tot]
    # drop exact-text keys that agree with their arg-count majority
    for k in [k for k in table if k.startswith('T:')]:
        toks = k[2:].split()
        nk = 'N:%s/%d' % (toks[0], len(toks) - 1)
        if nk in table and table[nk][:3] == table[k][:3]:
            del table[k]
    vo_out = {}
    for mc, c in vo_count.items():
        pt = c.most_common(1)[0][0]
        vo_out[mc] = valueof[(mc, pt)]
    db = {'built': time.strftime('%Y-%m-%d %H:%M:%S'), 'roots': roots, 'files': nfiles,
          'table': table, 'valueof': vo_out}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w', encoding='utf-8') as f:
        json.dump(db, f)
    return db


_DB = None


def db():
    global _DB
    if _DB is None:
        if os.path.isfile(CACHE):
            with open(CACHE, encoding='utf-8') as f:
                _DB = json.load(f)
        else:
            _DB = build()
    return _DB


_T_TYPES = {'b': 'bang', 'i': 'int', 'f': 'float', 'l': '', 's': '', 'a': '', 'list': '', 'anything': '',
            'bang': 'bang', 'int': 'int', 'float': 'float', 'symbol': ''}


def _trigger_rule(toks):
    out = []
    for a in toks[1:]:
        if a in _T_TYPES:
            out.append(_T_TYPES[a])
        else:
            try:
                int(a)
                out.append('int')
            except ValueError:
                try:
                    float(a)
                    out.append('float')
                except ValueError:
                    out.append('')
    return [1, len(out), out]


def lookup(maxclass='newobj', text=''):
    """Return {numinlets, numoutlets, outlettype, source, confidence} or None."""
    t = db()['table']
    if maxclass != 'newobj':
        r = t.get('M:' + maxclass)
        if r:
            return _res(r, 'M:' + maxclass)
        return None
    toks = str(text).split()
    if not toks:
        return None
    k = 'T:' + ' '.join(toks)
    if k in t:
        return _res(t[k], k)
    if toks[0] in ('t', 'trigger') and len(toks) > 1:
        ni, no, ot = _trigger_rule(toks)
        return {'numinlets': ni, 'numoutlets': no, 'outlettype': ot, 'source': 'trigger rule',
                'confidence': 1.0}
    k = 'N:%s/%d' % (toks[0], len(toks) - 1)
    if k in t and t[k][3] / t[k][4] >= 0.75:
        return _res(t[k], k)
    k = 'C:' + toks[0]
    if k in t and t[k][3] / t[k][4] >= 0.95:
        return _res(t[k], k)
    return None


def _res(r, key):
    return {'numinlets': r[0], 'numoutlets': r[1], 'outlettype': r[2], 'source': key,
            'confidence': round(r[3] / r[4], 3), 'seen': r[4]}


def valueof_template(maxclass):
    return copy_dict(db()['valueof'].get(maxclass))


def copy_dict(d):
    return json.loads(json.dumps(d)) if d is not None else None


if __name__ == '__main__':
    if '--build' in sys.argv:
        d = build(verbose=True)
        print('objdb:', CACHE, d['files'], 'files,', len(d['table']), 'keys,', len(d['valueof']), 'valueof templates')
    else:
        for a in sys.argv[1:]:
            mc, _, txt = a.partition(':') if a.startswith(('live.', 'message:', 'comment:')) else ('newobj', '', a)
            print(a, '->', lookup(mc, txt))
