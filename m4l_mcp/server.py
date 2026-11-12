"""m4l MCP server: edit and debug Max for Live devices (.amxd) and Max patch
files on disk, and check them against the running Live set.

Edits are made in memory and written only by ``device_save`` (backup,
conflict check, verify, atomic replace). Run: ``python -m m4l_mcp.server``.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from typing import Any, Optional

try:                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server, Image
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image

from . import container as C
from . import live as L
from . import objdb
from . import patch as P
from . import winutil as W
from .document import REGISTRY, BACKUP_DIR, loads

mcp = _Server('m4l', instructions="""Edit and debug Max for Live devices (.amxd, plain or frozen) and .maxpat files on disk.
Workflow: device_info -> patch_tree / patch_find / patch_connections / patch_trace to understand ->
patch_* edits (in memory) -> patch_validate -> device_diff -> device_save. Nothing touches disk until device_save.
Box paths are 'obj-12' or 'obj-1137::obj-12' (through embedded subpatchers) inside one file; `file` picks the
embedded file of a frozen device (default: the top-level patcher; a unique substring is enough).
Never write a device while its Max editor window is open (device_save refuses): Max's reload fails and closing
the window has crashed Live. Ask the user to close it WITHOUT saving. With no editor open, Live hot-reloads the
device on save and keeps its state (no need to reopen the set). Check with live_check_reload.""")

ROOT = os.environ.get('M4L_MCP_ROOT') or os.getcwd()


# ------------------------------------------------------------------ util ----
def _resolve_path(path: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(ROOT, p)
    if os.path.isfile(p):
        return os.path.abspath(p)
    # bare device name: search the User Library / project for it
    name = os.path.basename(path).lower()
    roots = [r for r in (os.environ.get('ABLETON_USER_LIBRARY'), ROOT) if r and os.path.isdir(r)]
    hits = []
    for r in roots:
        for dp, dn, fn in os.walk(r):
            dn[:] = [d for d in dn if not d.startswith('.')]
            for f in fn:
                fl = f.lower()
                if fl.endswith(('.amxd', '.maxpat', '.maxhelp')) and (fl == name or name in fl):
                    hits.append(os.path.join(dp, f))
        if hits:
            break
    exact = [h for h in hits if os.path.basename(h).lower() in (name, name + '.amxd')]
    hits = exact or hits
    hits = list(dict.fromkeys(os.path.abspath(h) for h in hits))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError('no such file: %s (also searched the User Library for that name)' % path)
    raise FileNotFoundError('%r matches several files: %s' % (path, hits[:10]))


def _doc(path: str):
    return REGISTRY.get(_resolve_path(path))


def _state(doc) -> dict:
    s = {'unsaved_changes': doc.is_dirty}
    if doc.is_dirty:
        s['hint'] = 'edits are in memory only; review with device_diff and write with device_save'
    return s


# ------------------------------------------------------------- device I/O ---
@mcp.tool()
def device_info(path: str) -> dict:
    """Open a device (.amxd) or patch file (.maxpat) and describe it: format (plain/frozen), device type,
    embedded files, top-level patcher summary, parameter count and unsaved-edit state. `path` may be a full
    path or a device name found in the User Library."""
    d = _doc(path)
    top = d.json()
    p = top.get('patcher', {})
    files = []
    for e in d.c.entries:
        f = {'name': e.name, 'type': e.type, 'size': len(e.data)}
        if e.is_json():
            try:
                j = d.json(e.name)
                f['boxes'] = len(j.get('patcher', {}).get('boxes', []))
            except Exception as ex:
                f['error'] = str(ex)
        files.append(f)
    reg = P.registry(d)
    info = {'path': d.path, 'format': d.kind, 'device_type': d.c.device_type, 'size': d.disk_size,
            'roundtrip_identical': d.roundtrip_ok, 'files': files,
            'top': {'devicewidth': p.get('devicewidth'), 'boxes': len(p.get('boxes', [])),
                    'lines': len(p.get('lines', [])), 'openinpresentation': p.get('openinpresentation'),
                    'appversion': p.get('appversion')},
            'registered_parameters': len([v for v in (reg or {}).values() if isinstance(v, list)]),
            'disk_changed_since_read': REGISTRY.disk_changed(d)}
    info.update(_state(d))
    return info


@mcp.tool()
def device_read_file(path: str, name: Optional[str] = None, start_line: int = 1, max_lines: int = 200) -> dict:
    """Read an embedded file as text (JSON is pretty-printed from the in-memory state, so unsaved edits show).
    Binary files return size and type only. Use patch_* tools to inspect patchers; this is for scripts, small
    JSON and raw checks."""
    d = _doc(path)
    n = d.resolve_name(name)
    e = d.entry(n)
    if e.is_json():
        text = json.dumps(d.json(n), indent='\t', ensure_ascii=False)
    else:
        try:
            text = e.data.decode('utf-8')
        except UnicodeDecodeError:
            return {'name': n, 'type': e.type, 'size': len(e.data), 'binary': True}
    lines = text.splitlines()
    s = max(1, start_line)
    chunk = lines[s - 1:s - 1 + max_lines]
    return {'name': n, 'type': e.type, 'total_lines': len(lines), 'start_line': s,
            'text': '\n'.join(chunk), 'more': s - 1 + max_lines < len(lines)}


@mcp.tool()
def device_extract(path: str, out_dir: str, names: Optional[list[str]] = None) -> dict:
    """Write embedded files (all, or `names`) to a folder on disk, as currently held in memory."""
    d = _doc(path)
    d.serialize()      # flush in-memory JSON edits into entry bytes
    os.makedirs(out_dir, exist_ok=True)
    want = {d.resolve_name(n) for n in names} if names else None
    out = []
    for e in d.c.entries:
        if want and e.name not in want:
            continue
        fn = e.name
        if e is d.c.entries[0] and d.kind != 'maxpat' and not fn.lower().endswith('.maxpat'):
            fn = os.path.splitext(fn)[0] + '.top.maxpat'
        dest = os.path.join(out_dir, fn)
        with open(dest, 'wb') as f:
            f.write(e.data.rstrip(b'\0') if e.is_json() else e.data)
        out.append(dest)
    return {'written': out}


@mcp.tool()
def device_put_file(path: str, name: str, source: Optional[str] = None, text: Optional[str] = None,
                    type_code: Optional[str] = None, add_dependency: bool = True) -> dict:
    """Replace or add an embedded file in a frozen device, from a file on disk (`source`) or literal `text`.
    New files get a directory record modelled on Max's own and (for frozen devices) a dependency_cache entry.
    `type_code` is the 4-char collective type (default from extension: JSON, TEXT, PNG , svg , JPEG ...).
    In memory until device_save."""
    d = _doc(path)
    if (source is None) == (text is None):
        raise ValueError('pass exactly one of source or text')
    data = open(source, 'rb').read() if source is not None else text.encode('utf-8')
    if name.lower().endswith(('.maxpat', '.json')):
        loads(data)  # must be valid JSON
    if d.kind == 'maxpat' or (d.kind == 'amxd-plain' and name != d.top_name):
        raise ValueError('%s is not a frozen device; only frozen devices hold embedded files' % d.path)
    tc = type_code.encode('latin-1').ljust(4)[:4] if type_code else None
    status = d.put_file(name, data, tc)
    notes = []
    if status == 'added' and add_dependency and d.kind == 'amxd-frozen':
        top = d.json(d.top_name)['patcher']
        dc = top.setdefault('dependency_cache', [])
        if not any(x.get('name') == name for x in dc):
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            tmpl = next((x for x in dc if x.get('bootpath')), None)
            ent = {'name': name, 'bootpath': tmpl['bootpath'] if tmpl else '~/Documents',
                   'type': {'maxpat': 'JSON', 'js': 'TEXT', 'png': 'PNG', 'jpg': 'JPEG', 'svg': 'svg'}.get(ext, ext),
                   'implicit': 1}
            dc.append(ent)
            d.mark_dirty(d.top_name)
            notes.append('added dependency_cache entry')
    return {'file': name, 'status': status, 'size': len(data), 'notes': notes, **_state(d)}


@mcp.tool()
def device_remove_file(path: str, name: str) -> dict:
    """Remove an embedded file from a frozen device (and its dependency_cache entry). In memory until saved."""
    d = _doc(path)
    n = d.resolve_name(name)
    d.remove_file(n)
    top = d.json(d.top_name)['patcher']
    dc = top.get('dependency_cache') or []
    if any(x.get('name') == n for x in dc):
        top['dependency_cache'] = [x for x in dc if x.get('name') != n]
        d.mark_dirty(d.top_name)
    return {'removed': n, **_state(d)}


@mcp.tool()
def device_diff(path: str, against: Optional[str] = None, max_lines: int = 300) -> dict:
    """Structural diff. Default: unsaved in-memory edits vs the file as read from disk. With `against` (another
    .amxd/.maxpat path, e.g. a .bak): that file vs the current state. Lists boxes/lines added, removed and
    changed per embedded file, parameter registry changes, and binary file changes."""
    d = _doc(path)
    if against:
        raw = open(_resolve_path(against), 'rb').read()
        base = C.parse(raw, against)
    else:
        base = d.orig
    d_bytes = {e.name: e for e in base.entries}
    out = []
    cur_names = d.names()
    for n in cur_names:
        if n not in d_bytes:
            out.append('+ file %s' % n)
            continue
        e0 = d_bytes[n]
        if d.entry(n).is_json():
            try:
                a = loads(e0.data)
                b = d.json(n)
            except Exception as ex:
                out.append('! %s: %s' % (n, ex))
                continue
            if a != b:
                ch = P.diff_json(a, b)
                out.append('~ file %s (%d changes)' % (n, len(ch)))
                out += ['    ' + x for x in ch]
        elif e0.data != d.entry(n).data:
            out.append('~ file %s (binary, %d -> %d bytes)' % (n, len(e0.data), len(d.entry(n).data)))
    for n in d_bytes:
        if n not in cur_names:
            out.append('- file %s' % n)
    return {'base': against or 'on-disk version read at open', 'changes': out[:max_lines],
            'truncated': len(out) > max_lines, **_state(d)}


@mcp.tool()
def device_save(path: str, dest: Optional[str] = None, force: bool = False, overwrite: bool = False,
                ignore_open_editor: bool = False) -> dict:
    """Write the in-memory state. Refuses while a Max editor window for the target device is open (writing then
    has crashed Live: ask the user to close that window WITHOUT saving, then retry). Refuses if the file changed on
    disk since it was read (force=true overrides; the on-disk version is backed up first). Always backs up the file
    being replaced, re-parses and verifies the bytes, then writes a temp file and atomically replaces. `dest` saves
    elsewhere (overwrite=true to replace an existing file). ignore_open_editor only for a known false match."""
    d = _doc(path)
    target = os.path.abspath(dest) if dest else d.path
    editors = W.open_max_editors(target)
    if editors and not ignore_open_editor:
        raise RuntimeError('a Max editor window for this device is open (%s). Writing the file now makes Max\'s '
                           'reload fail and has crashed Live when the window was closed. Ask the user to close '
                           'that window WITHOUT saving, then save again.' % '; '.join(editors))
    v = P.validate(d, check_io=False)
    if v['errors'] and not force:
        raise ValueError('validation errors (fix them or pass force=true): %s' % v['errors'][:10])
    r = d.save(dest, force=force, overwrite=overwrite)
    r['backup_dir'] = BACKUP_DIR
    r['warnings'] = v['warnings'][:20]
    r['next'] = ('With no Max editor open, Live hot-reloads the device and keeps its state; the user does not need '
                 'to reopen the set. Verify with live_check_reload.')
    return r


@mcp.tool()
def device_revert(path: str) -> dict:
    """Discard in-memory edits and re-read the file from disk."""
    d = _doc(path)
    d.reload()
    return {'reloaded': d.path, **_state(d)}


@mcp.tool()
def device_status() -> dict:
    """List documents open in this server and whether they hold unsaved edits or changed on disk."""
    return {'open': [{'path': d.path, 'unsaved_changes': d.is_dirty, 'changed_files': sorted(d.changed),
                      'disk_changed_since_read': REGISTRY.disk_changed(d)} for d in REGISTRY.docs.values()]}


# ------------------------------------------------------------ queries -------
@mcp.tool()
def patch_tree(path: str, max_depth: int = 8) -> dict:
    """Instance hierarchy of a device: every embedded subpatcher, bpatcher and abstraction instance with its
    instance path (the key Live's parameter registry uses) and the file it shows. Also which files are used
    where."""
    d = _doc(path)
    lines, usage = P.tree_report(d, max_depth)
    return {'tree': lines, 'file_usage': usage}


@mcp.tool()
def patch_find(path: str, query: Optional[str] = None, maxclass: Optional[str] = None, id: Optional[str] = None,
               file: Optional[str] = None, attr: Optional[str] = None, limit: int = 50) -> dict:
    """Find boxes in all embedded patchers. `query` is a case-insensitive substring of text, varname, bpatcher
    name, parameter longname, hint or annotation (prefix 're:' for a regex). `maxclass` e.g. 'live.text',
    'newobj', 'bpatcher'. `attr` is 'key' (has attribute) or 'key=value'. Returns file + box path for each hit."""
    d = _doc(path)
    hits, trunc = P.find(d, query, maxclass, id, file, attr, limit)
    return {'hits': hits, 'truncated': trunc}


@mcp.tool()
def patch_box(path: str, box: str, file: Optional[str] = None, include_patcher: bool = False) -> dict:
    """Full JSON of one box. An embedded subpatcher is summarised unless include_patcher=true."""
    d = _doc(path)
    p, b, sub = P.get_box(d, file, box)
    out = dict(b)
    if isinstance(out.get('patcher'), dict) and not include_patcher:
        pp = out['patcher']
        out['patcher'] = '<embedded patcher: %d boxes, %d lines; address its boxes as %s::obj-N>' % (
            len(pp.get('boxes', [])), len(pp.get('lines', [])), P.join(sub, b['id']))
    r = P.ref_summary(d, b)
    return {'file': d.resolve_name(file), 'box_path': P.join(sub, b['id']), 'box': out, 'refers_to': r}


@mcp.tool()
def patch_connections(path: str, box: Optional[str] = None, file: Optional[str] = None,
                      patcher: Optional[str] = None, limit: int = 300) -> dict:
    """Connections of one box (incoming per inlet, outgoing per outlet, plus named send/receive partners
    anywhere in the device), or with no `box`, every line in a patcher (`patcher` = subpatcher path)."""
    d = _doc(path)
    if box:
        return P.connections(d, file, box)
    lines, total = P.all_lines(d, file, patcher, limit)
    return {'where': P.where(d, file, P.split_path(patcher)), 'lines': lines, 'total': total}


@mcp.tool()
def patch_trace(path: str, box: str, file: Optional[str] = None, direction: str = 'out', depth: int = 4,
                port: Optional[int] = None, follow_names: bool = True, max_hops: int = 150) -> dict:
    """Follow the message flow from a box for `depth` hops: direction 'out' (downstream) or 'in' (upstream).
    `port` limits the first hop to one outlet (out) or inlet (in). Crosses into/out of subpatchers, bpatchers and
    abstractions via inlet/outlet objects, and (follow_names) jumps send->receive (s/r, send~/receive~, value,
    message-box ';name' sends) across the whole device."""
    d = _doc(path)
    return P.trace(d, file, box, direction, depth, port, follow_names, max_hops)


@mcp.tool()
def patch_presentation(path: str, file: Optional[str] = None, patcher: Optional[str] = None, depth: int = 1,
                       render: bool = False, scale: float = 3.0) -> Any:
    """Presentation layout of a patcher: every presentation box with its rect (sorted top-left to
    bottom-right), what each bpatcher shows, overlapping controls, and controls outside the device area.
    depth>1 includes bpatcher contents. render=true also returns a labelled wireframe PNG (bpatchers drawn with
    their contents)."""
    d = _doc(path)
    res = P.presentation(d, file, patcher, depth)
    if not render:
        return res
    from . import render as R
    png, size = R.render(d, file, patcher, depth=max(depth, 3), scale=scale)
    summary = {k: v for k, v in res.items() if k != 'items'}
    summary['items'] = len(res['items'])
    summary['rendered_size'] = size
    return [json.dumps(summary, ensure_ascii=False), Image(data=png, format='png')]


@mcp.tool()
def patch_parameters(path: str, query: Optional[str] = None) -> dict:
    """Live parameters of a device: every parameter-enabled box (UI live.* objects and non-UI objects with
    @parameter_enable) per instance, with its registry key and registered longname, plus unregistered parameters,
    stale registry keys and duplicate names."""
    d = _doc(path)
    r = P.parameter_report(d)
    if query:
        q = query.lower()
        r['parameters'] = [x for x in r['parameters'] if q in json.dumps(x).lower()]
    return r


@mcp.tool()
def patch_validate(path: str, check_io: bool = True) -> dict:
    """Check a device: JSON of every embedded patcher, duplicate ids, lines to missing boxes or non-existent
    inlets/outlets, outlettype counts, in/outlet counts vs what Max gives the object (check_io), subpatcher
    in/outlet counts, parameters without longname, parameter registry consistency, missing pictures/scripts/
    bpatcher files, and whether the container round-trips."""
    d = _doc(path)
    return P.validate(d, check_io)


@mcp.tool()
def object_io(text: str = '', maxclass: str = 'newobj', path: Optional[str] = None) -> dict:
    """How many inlets/outlets (and outlet types) Max gives an object, learned from patches saved by Max.
    With `path`, abstractions embedded in / next to that device are resolved too."""
    if path:
        r = P.infer_io(_doc(path), maxclass, text)
    else:
        r = objdb.lookup(maxclass, text)
    return r or {'unknown': True, 'hint': 'pass numinlets/numoutlets/outlettype explicitly when adding it'}


# -------------------------------------------------------------- edits -------
@mcp.tool()
def patch_add_box(path: str, text: Optional[str] = None, maxclass: str = 'newobj', file: Optional[str] = None,
                  patcher: Optional[str] = None, x: Optional[float] = None, y: Optional[float] = None,
                  width: Optional[float] = None, height: Optional[float] = None,
                  numinlets: Optional[int] = None, numoutlets: Optional[int] = None,
                  outlettype: Optional[list[str]] = None, attrs: Optional[dict] = None, id: Optional[str] = None,
                  presentation_rect: Optional[list[float]] = None, parameter: Optional[dict] = None,
                  allow_rename: bool = False) -> dict:
    """Add a box. maxclass 'newobj' (object box, give `text`), 'message', 'comment', or a UI class
    ('live.text', 'live.dial', 'bpatcher', ...). In/outlet counts are inferred (Max-saved patch corpus,
    abstractions, subpatcher contents); pass them explicitly if unknown. `patcher` = subpatcher path to add into.
    `attrs` are extra box attributes (dotted keys allowed: 'saved_attribute_attributes.valueof.parameter_mmax').
    Live parameters: live.* UI objects (and newobj text with '@parameter_enable 1') need
    parameter={'longname','shortname', optional 'type' (0 float,1 int,2 enum,3 blob),'mmin','mmax','enum',
    'initial','initial_enable','unitstyle',...}; the box gets saved_attribute_attributes.valueof and is registered
    in the device's parameter registry for every instance of that patcher. id defaults to the next free obj-N."""
    d = _doc(path)
    r = P.add_box(d, file, patcher, maxclass, text, x, y, width, height, numinlets, numoutlets, outlettype,
                  attrs, id, presentation_rect, parameter, allow_rename)
    return {**r, **_state(d)}


@mcp.tool()
def patch_set_attrs(path: str, box: str, set: Optional[dict] = None, unset: Optional[list[str]] = None,
                    file: Optional[str] = None, allow_rename: bool = False) -> dict:
    """Set/remove box attributes (dotted keys reach nested dicts). Changing an object's `text` re-infers its
    in/outlets and refuses if existing lines would dangle. set={'parameter': {...}} edits the Live parameter
    attributes and re-registers the longname. Ids cannot change."""
    d = _doc(path)
    return {**P.set_attrs(d, file, box, set, unset, allow_rename), **_state(d)}


@mcp.tool()
def patch_remove_box(path: str, box: str, file: Optional[str] = None) -> dict:
    """Delete a box, its lines, and its (and any nested) parameter registry entries."""
    d = _doc(path)
    return {**P.remove_box(d, file, box), **_state(d)}


@mcp.tool()
def patch_connect(path: str, src: str, outlet: int, dst: str, inlet: int, file: Optional[str] = None) -> dict:
    """Connect src outlet -> dst inlet (0-based). Both boxes must be in the same patcher; indices are checked
    against the boxes' numoutlets/numinlets."""
    d = _doc(path)
    return {**P.connect(d, file, src, outlet, dst, inlet), **_state(d)}


@mcp.tool()
def patch_disconnect(path: str, src: str, outlet: Optional[int] = None, dst: Optional[str] = None,
                     inlet: Optional[int] = None, file: Optional[str] = None) -> dict:
    """Remove lines from src (optionally only from `outlet`, to `dst`, into `inlet`)."""
    d = _doc(path)
    return {**P.disconnect(d, file, src, outlet, dst, inlet), **_state(d)}


@mcp.tool()
def patch_edit_batch(path: str, ops: list[dict], file: Optional[str] = None) -> dict:
    """Apply many edits atomically (all roll back if one fails). Each op is a dict with "op":
    add_box (same fields as patch_add_box, plus "as": "alias"), set (box, set, unset), remove_box (box),
    connect (src, outlet, dst, inlet), disconnect (src, outlet?, dst?, inlet?). Ops may override "file".
    Later ops refer to boxes added earlier as "$alias", e.g.
    [{"op":"add_box","text":"t b b","as":"t"},{"op":"connect","src":"$t","outlet":0,"dst":"obj-5","inlet":0}]."""
    d = _doc(path)
    return {**P.apply_ops(d, file, ops), **_state(d)}


# --------------------------------------------------------------- Live -------
@mcp.tool()
def live_find_devices(name: Optional[str] = None, m4l_only: bool = True) -> dict:
    """Find device instances in the running Live set (tracks, returns, master, recursively inside rack
    chains) via the ableton-live-mcp bridge. `name` filters by substring of the device name. Returns LOM
    paths usable with live_device_parameters and the ableton MCP's live_get/live_set."""
    return {'devices': L.find_devices(name, m4l_only)}


@mcp.tool()
def live_device_parameters(device_path: str, query: Optional[str] = None) -> dict:
    """Parameters of a device instance in Live (name, value, range, display string, enum items).
    `device_path` is a LOM path like 'live_set tracks 2 devices 0'."""
    r = L.device_parameters(device_path)
    if query:
        q = query.lower()
        r['parameters'] = [p for p in r['parameters'] if q in str(p.get('name', '')).lower()]
    return r


@mcp.tool()
def live_check_reload(path: str, device_path: Optional[str] = None) -> dict:
    """Has Live picked up the device file? Compares the parameters registered in the file on disk with the
    parameters of the device instance(s) in Live (found by name unless `device_path` is given). New or renamed
    parameters missing in Live mean the instance still runs the old version. If the file's parameter set did not
    change, this cannot tell (say so, and fall back to observing behaviour or the Max console)."""
    d = _doc(path)
    if d.is_dirty:
        note = 'this server holds unsaved edits for the file; comparing against the saved on-disk version'
    else:
        note = None
    from .document import Document
    disk = Document(d.path)
    # Live lists only automatable parameters (parameter_invisible 0), shows their SHORT names, and orders
    # them by parameter_order, then long name (case-sensitive).
    rep = P.parameter_report(disk)
    vis = [x for x in rep['parameters'] if not x.get('invisible') and x.get('type') != 3]
    vis.sort(key=lambda x: (x.get('order') or 0, x['registered_as'] or x['longname'] or ''))  # case-sensitive
    expected = [x['shortname'] or x['registered_as'] or x['longname'] for x in vis]
    by_short = {}
    for x in vis:
        by_short.setdefault(x['shortname'], []).append(x['registered_as'] or x['longname'])
    if device_path:
        targets = [{'path': device_path}]
    else:
        stem = os.path.splitext(os.path.basename(d.path))[0]
        targets = [x for x in L.find_devices(None, True) if x['name'].lower() == stem.lower()]
        if not targets:
            return {'found': False, 'hint': 'no M4L device named %r in the set; pass device_path '
                                            '(see live_find_devices)' % stem}
    out = []
    for t in targets:
        r = L.device_parameters(t['path'])
        live = [p['name'] for p in r['parameters'] if p['name'] != 'Device On' or p['index'] != 0]
        ms_f, ms_l = Counter(expected), Counter(live)
        missing = sorted((ms_f - ms_l).elements())
        extra = sorted((ms_l - ms_f).elements())
        if missing or extra:
            verdict = 'stale: Live runs a different version of the device (parameter sets differ)'
        elif live != expected:
            verdict = 'same parameters, different order: probably a different version (renamed long names)'
        else:
            verdict = ('parameters match: if this edit added/renamed parameters, Live has reloaded it; '
                       'otherwise this check cannot tell')
        out.append({'device_path': t['path'], 'track': t.get('track'), 'name': r['name'],
                    'live_parameters': len(live), 'file_parameters': len(expected),
                    'in_file_not_in_live': [{'shortname': s, 'longnames': by_short.get(s)} for s in missing][:30],
                    'in_live_not_in_file': extra[:30], 'verdict': verdict})
    res = {'file': d.path, 'instances': out}
    if note:
        res['note'] = note
    return res


@mcp.tool()
def live_bridge_status() -> dict:
    """Is the ableton-live-mcp bridge (Remote Script in Live) reachable?"""
    try:
        return {'reachable': True, 'status': L.status()}
    except L.BridgeError as ex:
        return {'reachable': False, 'error': str(ex)}


def main():
    mcp.run()


if __name__ == '__main__':
    main()
