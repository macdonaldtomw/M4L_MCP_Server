"""Talk to the ableton-live-mcp Remote Script bridge (JSON-RPC, one JSON
object per line, 127.0.0.1:8765 by default) to find device instances in the
running Live set and compare them with the device file on disk."""
from __future__ import annotations

import itertools
import json
import os
import socket

HOST = os.environ.get('ABLETON_MCP_HOST', '127.0.0.1')
PORT = int(os.environ.get('ABLETON_MCP_PORT', '8765'))
TIMEOUT = float(os.environ.get('M4L_MCP_LIVE_TIMEOUT', '20'))
_ids = itertools.count(1)


class BridgeError(RuntimeError):
    pass


def request(method, params=None, timeout=TIMEOUT):
    payload = {'jsonrpc': '2.0', 'id': next(_ids), 'method': method, 'params': params or {}}
    try:
        s = socket.create_connection((HOST, PORT), 2.0)
    except OSError as ex:
        raise BridgeError(
            'cannot reach the Ableton bridge at %s:%d (%s). Is Live running with the "Ableton_Live_MCP" '
            'control surface selected in Settings > Link/Tempo/MIDI?' % (HOST, PORT, ex)) from None
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(payload, separators=(',', ':')) + '\n').encode('utf-8'))
        buf = b''
        while b'\n' not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        raise BridgeError('Ableton bridge timed out after %gs on %s' % (timeout, method)) from None
    finally:
        s.close()
    if not buf:
        raise BridgeError('empty response from Ableton bridge')
    msg = json.loads(buf.split(b'\n', 1)[0].decode('utf-8'))
    if 'error' in msg:
        e = msg['error']
        raise BridgeError('%s %s' % (e.get('code', ''), e.get('message', e)))
    return msg.get('result')


def status():
    return request('bridge_status', {'timeout': 3}, timeout=5)


_FIND = r'''
out = []
def walk(devs, path, track, depth):
    for i, d in enumerate(devs):
        p = "%s devices %d" % (path, i)
        cn = getattr(d, "class_name", "")
        item = {"path": p, "track": track, "name": d.name, "class_name": cn,
                "is_active": bool(getattr(d, "is_active", True)), "n_params": len(d.parameters),
                "depth": depth}
        out.append(item)
        if getattr(d, "can_have_chains", False):
            for ci, ch in enumerate(d.chains):
                walk(ch.devices, "%s chains %d" % (p, ci), track + " > " + d.name + " > " + ch.name, depth + 1)
            if getattr(d, "can_have_drum_pads", False):
                pass
            for ci, ch in enumerate(getattr(d, "return_chains", []) or []):
                walk(ch.devices, "%s return_chains %d" % (p, ci), track + " > " + d.name + " > " + ch.name, depth + 1)
for ti, t in enumerate(song.tracks):
    walk(t.devices, "live_set tracks %d" % ti, t.name, 0)
for ti, t in enumerate(song.return_tracks):
    walk(t.devices, "live_set return_tracks %d" % ti, t.name, 0)
walk(song.master_track.devices, "live_set master_track", "Master", 0)
result = out
'''


def find_devices(name=None, m4l_only=True):
    devs = request('exec', {'code': _FIND, 'timeout': 30, 'max_items': -1}) or []
    q = (name or '').lower()
    out = []
    for d in devs:
        if not isinstance(d, dict) or 'path' not in d:
            continue
        if m4l_only and not str(d.get('class_name', '')).startswith('Mx'):
            continue
        if q and q not in str(d.get('name', '')).lower():
            continue
        out.append(d)
    return out


_PARAMS = r'''
d = obj
out = []
for i, p in enumerate(d.parameters):
    item = {"index": i, "name": p.name, "original_name": getattr(p, "original_name", p.name),
            "value": p.value, "min": p.min, "max": p.max, "is_quantized": bool(p.is_quantized)}
    try:
        if p.is_quantized:
            item["value_items"] = list(p.value_items)
            item["display"] = p.str_for_value(p.value)
        else:
            item["display"] = p.str_for_value(p.value)
    except Exception:
        pass
    out.append(item)
result = {"name": d.name, "class_name": getattr(d, "class_name", ""), "parameters": out}
'''


def device_parameters(path):
    return request('exec', {'code': _PARAMS, 'ref': {'path': path}, 'timeout': 30, 'max_items': -1})
