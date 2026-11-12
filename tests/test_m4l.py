"""Tests run against *copies* of real devices from this project."""
import json
import os
import shutil
import struct

import pytest

from m4l_mcp import container as C
from m4l_mcp import document as D
from m4l_mcp import patch as P

# Fixtures are real devices from a Live User Library; tests that need a missing one are skipped.
UL = os.environ.get('M4L_TEST_USER_LIBRARY', os.path.join(os.path.expanduser('~'), 'Documents', 'Ableton', 'User Library'))
FROZEN = os.path.join(UL, 'M4L', 'FCB 1010 FOOT CONTROL V1.2 TomEdits.amxd')
PLAIN = os.path.join(UL, 'Presets', 'Instruments', 'Max Instrument', 'More Simpler autoload.amxd')
MAXPAT = os.path.join(os.path.expanduser('~'), 'Documents', 'Max 9', 'Library', 'M4L.SimplerPolyVoiceRev~.maxpat')
SINGLE = 'FCB1010 Bpatcher SINGLE 2.maxpat'


def need(p):
    if not os.path.isfile(p):
        pytest.skip('missing fixture ' + p)
    return p


@pytest.fixture
def tmpcopy(tmp_path, monkeypatch):
    monkeypatch.setattr(D, 'BACKUP_DIR', str(tmp_path / 'backups'))
    D.REGISTRY.docs.clear()

    def mk(src):
        dst = tmp_path / os.path.basename(src)
        shutil.copyfile(need(src), dst)
        return str(dst)
    return mk


@pytest.mark.parametrize('src', [FROZEN, PLAIN, MAXPAT])
def test_roundtrip_identical(src):
    raw = open(need(src), 'rb').read()
    c = C.parse(raw, src)
    assert C.build(c) == raw


def test_save_without_edits_is_identical(tmpcopy):
    f = tmpcopy(FROZEN)
    raw = open(f, 'rb').read()
    d = D.REGISTRY.get(f)
    d.json(SINGLE)          # parse but do not modify
    d.save()
    assert open(f, 'rb').read() == raw


def test_frozen_edit_save_reopen(tmpcopy):
    f = tmpcopy(FROZEN)
    raw = open(f, 'rb').read()
    d = D.REGISTRY.get(f)
    r = P.add_box(d, SINGLE, None, 'newobj', 't b b', 10, 10)
    assert r['id'].startswith('obj-')
    m = P.add_box(d, SINGLE, None, 'message', 'hello', 10, 50)
    P.connect(d, SINGLE, r['box'], 1, m['box'], 0)
    res = d.save()
    assert res['backup'] and open(res['backup'], 'rb').read() == raw
    D.REGISTRY.docs.clear()
    d2 = D.REGISTRY.get(f)
    p = d2.json(SINGLE)['patcher']
    assert P.box_map(p)[r['id']]['numoutlets'] == 2
    assert any(l['patchline']['source'] == [r['id'], 1] for l in p['lines'])
    # untouched entries are byte-identical, top-level JSON still ends with \r\n\0
    old = {e.name: e.data for e in C.parse(raw, f).entries}
    for e in d2.c.entries:
        if e.name != SINGLE:
            assert e.data == old[e.name], e.name
    assert d2.c.entries[0].data.endswith(b'\r\n\x00')
    v = P.validate(d2, check_io=False)
    assert v['ok'], v['errors']


def test_plain_device_header_and_trailer(tmpcopy):
    f = tmpcopy(PLAIN)
    d = D.REGISTRY.get(f)
    P.add_box(d, None, None, 'comment', 'added by test', 10, 10)
    d.save()
    raw = open(f, 'rb').read()
    assert raw[:4] == b'ampf'
    i = raw.find(b'ptch')
    assert struct.unpack('<I', raw[i + 4:i + 8])[0] == len(raw) - i - 8
    assert raw.endswith(b'}\r\n\x00')
    assert 'added by test' in raw.decode('utf-8', 'replace')


def test_conflict_refused(tmpcopy):
    f = tmpcopy(PLAIN)
    d = D.REGISTRY.get(f)
    P.add_box(d, None, None, 'comment', 'x', 0, 0)
    with open(f, 'ab') as fh:            # someone else saves the file
        fh.write(b' ')
    with pytest.raises(RuntimeError, match='changed on disk'):
        d.save()
    d.save(force=True)                   # force overwrites, after backing up


def test_parameter_registered_per_instance(tmpcopy):
    f = tmpcopy(FROZEN)
    d = D.REGISTRY.get(f)
    reg = P.registry(d)
    before = len([v for v in reg.values() if isinstance(v, list)])
    r = P.add_box(d, SINGLE, None, 'live.text', None, 10, 10,
                  parameter={'longname': 'TestToggle', 'shortname': 'Test', 'enum': ['off', 'on']},
                  attrs={'mode': 1})
    keys = [k for k in reg if k.endswith('::' + r['id'])]
    assert len(keys) == 10                      # one per pedal bpatcher
    names = {reg[k][0] for k in keys}
    assert len(names) == 10 and 'TestToggle' in names
    assert len([v for v in reg.values() if isinstance(v, list)]) == before + 10
    rep = P.parameter_report(d)
    assert not rep['unregistered'] and not rep['duplicate_names']
    P.remove_box(d, SINGLE, r['box'])
    assert not [k for k in reg if k.endswith('::' + r['id'])]


def test_duplicate_longname_rejected_single_instance(tmpcopy):
    f = tmpcopy(PLAIN)
    d = D.REGISTRY.get(f)
    existing = next(v[0] for v in P.registry(d).values() if isinstance(v, list))
    with pytest.raises(ValueError, match='already used'):
        P.add_box(d, None, None, 'live.dial', None, 0, 0, parameter={'longname': existing})


def test_batch_rolls_back(tmpcopy):
    f = tmpcopy(FROZEN)
    d = D.REGISTRY.get(f)
    n = len(d.json(SINGLE)['patcher']['boxes'])
    with pytest.raises(ValueError, match='rolled back'):
        P.apply_ops(d, SINGLE, [
            {'op': 'add_box', 'text': 't b', 'as': 'a'},
            {'op': 'connect', 'src': '$a', 'outlet': 5, 'dst': '$a', 'inlet': 0},
        ])
    assert len(d.json(SINGLE)['patcher']['boxes']) == n
    r = P.apply_ops(d, SINGLE, [
        {'op': 'add_box', 'text': 't b', 'as': 'a'},
        {'op': 'add_box', 'text': 'print X', 'as': 'b'},
        {'op': 'connect', 'src': '$a', 'outlet': 0, 'dst': '$b', 'inlet': 0},
    ])
    assert r['aliases']['a'] != r['aliases']['b']


def test_text_change_guard(tmpcopy):
    f = tmpcopy(FROZEN)
    d = D.REGISTRY.get(f)
    r = P.apply_ops(d, SINGLE, [
        {'op': 'add_box', 'text': 't b b b', 'as': 'a'},
        {'op': 'add_box', 'text': 'print X', 'as': 'b'},
        {'op': 'connect', 'src': '$a', 'outlet': 2, 'dst': '$b', 'inlet': 0},
    ])
    with pytest.raises(ValueError, match='no longer exist'):
        P.set_attrs(d, SINGLE, r['aliases']['a'], {'text': 't b'})


def test_put_file_and_trace(tmpcopy):
    f = tmpcopy(FROZEN)
    d = D.REGISTRY.get(f)
    d.put_file('note.txt', b'hello')
    d.save()
    D.REGISTRY.docs.clear()
    d2 = D.REGISTRY.get(f)
    assert d2.entry('note.txt').data == b'hello'
    assert d2.roundtrip_ok
    # message -> s name  ==> r name in another embedded file -> print
    r = P.apply_ops(d2, SINGLE, [
        {'op': 'add_box', 'maxclass': 'message', 'text': 'bang', 'as': 'm'},
        {'op': 'add_box', 'text': 's ---m4lTest', 'as': 's'},
        {'op': 'connect', 'src': '$m', 'outlet': 0, 'dst': '$s', 'inlet': 0},
    ])
    P.apply_ops(d2, 'Parent', [
        {'op': 'add_box', 'text': 'r ---m4lTest', 'as': 'r'},
        {'op': 'add_box', 'text': 'print got', 'as': 'p'},
        {'op': 'connect', 'src': '$r', 'outlet': 0, 'dst': '$p', 'inlet': 0},
    ])
    t = P.trace(d2, SINGLE, r['aliases']['m'], 'out', depth=4)
    edges = [(h['edge'], h['to']) for h in t['hops']]
    assert any(e.startswith('send ---m4lTest') for e, _ in edges), edges
    assert any('print got' in to for _, to in edges), edges
    printer = P.find(d2, 'print got')[0][0]
    back = P.trace(d2, printer['file'], printer['box'], 'in', depth=4)
    assert any('bang' in h['to'] for h in back['hops']), back['hops']


def test_tree_and_render(tmpcopy):
    f = tmpcopy(FROZEN)
    d = D.REGISTRY.get(f)
    lines, usage = P.tree_report(d)
    assert len(usage[SINGLE]) == 10
    from m4l_mcp import render as R
    png, size = R.render(d, 'Parent', None, depth=2, scale=1.5)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
