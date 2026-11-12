"""Draw a patcher's presentation view as a labelled wireframe PNG.

Not a pixel-accurate Max rendering: boxes are drawn at their
presentation_rect, coloured by class (panels use their own colour), with bpatcher
contents drawn recursively (honouring ``offset``) and clipped to the bpatcher.
Good enough to check placement, overlaps and what sits where.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from . import patch as P

FILL = {
    'live.dial': (70, 120, 170), 'live.numbox': (70, 120, 170), 'live.text': (60, 140, 120),
    'live.toggle': (60, 140, 120), 'live.button': (60, 140, 120), 'live.tab': (60, 140, 120),
    'live.menu': (110, 100, 160), 'umenu': (110, 100, 160), 'textbutton': (60, 140, 120),
    'number': (70, 120, 170), 'flonum': (70, 120, 170), 'message': (120, 120, 120),
    'live.drop': (150, 110, 60), 'jsui': (150, 90, 90), 'v8ui': (150, 90, 90),
    'live.slider': (70, 120, 170), 'slider': (70, 120, 170), 'multislider': (70, 120, 170),
}
DECOR_OUTLINE = (150, 150, 150)


def _color(v, default):
    if isinstance(v, dict):
        v = v.get('color')
    if isinstance(v, list) and len(v) >= 3:
        return tuple(int(max(0, min(1, float(c))) * 255) for c in v[:3])
    return default


def _font(px):
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def _draw(doc, file, sub, w, h, offset, depth, scale, bg):
    img = Image.new('RGB', (max(1, int(w * scale)), max(1, int(h * scale))), bg)
    dr = ImageDraw.Draw(img, 'RGBA')
    p = P.get_patcher(doc, file, sub)
    items = [b for b in P.boxes(p) if b.get('presentation') and not b.get('hidden')]
    items.sort(key=lambda b: 0 if b.get('background') or b.get('maxclass') == 'panel' else 1)
    font = _font(max(9, int(4 * scale)))
    ox, oy = offset
    for b in items:
        r = b.get('presentation_rect') or b.get('patching_rect')
        x0, y0 = (r[0] - ox) * scale, (r[1] - oy) * scale
        x1, y1 = x0 + r[2] * scale, y0 + r[3] * scale
        mc = b.get('maxclass')
        if mc == 'bpatcher':
            child = P._child_loc(doc, file, list(sub), b)
            if child and depth > 1:
                cd, cf, cs = child
                sub_img = _draw(cd, cf, cs, r[2], r[3], tuple(b.get('offset') or (0, 0)), depth - 1, scale,
                                _color(b.get('bgcolor'), bg))
                img.paste(sub_img, (int(x0), int(y0)))
            dr.rectangle([x0, y0, x1, y1], outline=(230, 200, 60), width=1)
            dr.text((x0 + 2, y0 + 1), b['id'], fill=(230, 200, 60), font=font)
            continue
        if mc == 'panel':
            dr.rectangle([x0, y0, x1, y1], fill=_color(b.get('bgfillcolor') or b.get('bgcolor'), (60, 60, 60)),
                         outline=DECOR_OUTLINE)
            continue
        if mc in ('comment', 'live.comment'):
            dr.text((x0 + 1, y0 + 1), str(b.get('text', ''))[:60], fill=(235, 235, 235), font=font)
            continue
        if mc in ('fpic', 'live.line'):
            dr.rectangle([x0, y0, x1, y1], outline=DECOR_OUTLINE)
            dr.text((x0 + 2, y0 + 1), 'pic ' + str(b.get('pic', '')).split('/')[-1][:30], fill=DECOR_OUTLINE, font=font)
            continue
        fill = FILL.get(mc, (100, 100, 100))
        if b.get('pictures') or b.get('usepicture'):
            fill = (170, 120, 40)
        # translucent so stacked/transparent click areas do not hide what is under them
        big = r[2] * r[3] > 2500
        dr.rectangle([x0, y0, x1, y1], fill=fill + ((40,) if big else (150,)), outline=fill + (255,), width=2)
        vo = (b.get('saved_attribute_attributes') or {}).get('valueof') or {}
        lab = vo.get('parameter_shortname') or b.get('text') or b.get('varname') or mc
        dr.text((x0 + 2, y0 + 1), str(lab)[:24], fill=(255, 255, 255), font=font)
        dr.text((x0 + 2, y1 - font.size - 1 if hasattr(font, 'size') else y0 + 12), b['id'],
                fill=(220, 220, 220), font=font)
    return img


def render(doc, file=None, sub=None, depth=3, scale=3.0, width=None, height=None):
    p = P.get_patcher(doc, file, sub)
    if width is None or height is None:
        is_top = not P.split_path(sub) and doc.resolve_name(file) == doc.top_name and doc.kind != 'maxpat'
        if is_top and p.get('devicewidth'):
            width, height = p['devicewidth'], 169.0
        else:
            rs = [(b.get('presentation_rect') or [0, 0, 0, 0]) for b in P.boxes(p) if b.get('presentation')]
            width = max([r[0] + r[2] for r in rs] or [100]) + 4
            height = max([r[1] + r[3] for r in rs] or [100]) + 4
    img = _draw(doc, file, P.split_path(sub), width, height, (0, 0), depth, scale,
                _color(p.get('bgcolor'), (40, 40, 40)))
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue(), (width, height)
