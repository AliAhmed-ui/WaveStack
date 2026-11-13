#!/usr/bin/env python3
"""
WaveStack pixel-art scene generator (Wallpaper Mode backdrops).

Paints detailed 320x180 pixel-art scenes procedurally -- layered skies,
shaded cumulus clouds, faceted mountains, ruins, cities, factories --
using limited colour ramps and 4x4 ordered dithering, the way hand-made
pixel art is shaded. Every scene is seeded from (vibe, index), so the
output is deterministic and can be regenerated at any time.

Run directly to (re)write assets/visuals/<vibe>/image_NN.png:

    ./venv/bin/python3 wavestack_art.py            # only empty folders
    ./venv/bin/python3 wavestack_art.py --force    # overwrite everything

Needs numpy and Pillow (both already in requirements.txt). No Tk access.
"""

import math
import os
import sys
import zlib

import numpy as np
from PIL import Image, ImageDraw

W, H = 320, 180
SCENES_PER_VIBE = 12
VIBES = ("cyber_neon", "industrial_grunge", "midnight_dusk", "default")

_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                    [3, 11, 1, 9], [15, 7, 13, 5]], float) / 16.0
BAYER = np.tile(_BAYER4, (H // 4 + 1, W // 4 + 1))[:H, :W]
YY, XX = np.mgrid[0:H, 0:W]


# --------------------------------------------------------------------------
# Colour / noise primitives
# --------------------------------------------------------------------------

def hexc(s):
    """'#rrggbb' (or an RGB triple / ramp entry) -> uint8 array."""
    if not isinstance(s, str):
        return np.asarray(s, np.uint8)[:3]
    s = s.lstrip("#")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)


def ramp(*colours):
    """A shading ramp, darkest first."""
    return np.array([hexc(c) for c in colours], np.uint8)


def hazed(rmp, colour, amount):
    """Pull a ramp toward *colour* (atmospheric perspective)."""
    c = hexc(colour).astype(float)
    return (rmp.astype(float) * (1 - amount) + c * amount).astype(np.uint8)


def ramp_fill(C, mask, rmp, t, dither=0.6):
    """Paint mask pixels from ramp by tone t (0 = darkest, 1 = lightest),
    ordered-dithered across band edges so gradients read as pixel art."""
    n = len(rmp)
    v = np.clip(t, 0, 1) * (n - 1) + (BAYER - 0.5) * dither
    idx = np.clip(np.rint(v), 0, n - 1).astype(int)
    C[mask] = rmp[idx][mask]


def tint(C, mask, colour, amount):
    c = hexc(colour).astype(float)
    C[mask] = (C[mask] * (1 - amount) + c * amount).astype(np.uint8)


def fill(C, mask, colour):
    C[mask] = hexc(colour)


def rect(x0, y0, x1, y1):
    return (XX >= x0) & (XX < x1) & (YY >= y0) & (YY < y1)


def glow(C, mask, colour, radius=3, strength=0.7):
    """Dithered halo around *mask*: neon signs, windows, suns."""
    col = hexc(colour).astype(float)
    cur = mask.copy()
    seen = mask.copy()
    for r in range(1, radius + 1):
        grown = (cur | np.roll(cur, 1, 0) | np.roll(cur, -1, 0)
                 | np.roll(cur, 1, 1) | np.roll(cur, -1, 1))
        ring = grown & ~seen
        s = strength * (1 - (r - 1) / radius)
        sel = ring & (BAYER < s)
        C[sel] = (C[sel] * 0.45 + col * 0.55).astype(np.uint8)
        seen |= grown
        cur = grown


def noise2d(rng, cx, cy):
    g = rng.random((cy + 1, cx + 1)).astype(np.float32)
    return np.asarray(Image.fromarray(g).resize((W, H), Image.BICUBIC))


def fbm2d(rng, cells=4, octaves=4, aspect=None):
    total = np.zeros((H, W), np.float32)
    amp, norm = 1.0, 0.0
    for _ in range(octaves):
        cy = max(1, int(cells * (aspect if aspect else H / W)))
        total += amp * noise2d(rng, cells, cy)
        norm += amp
        amp *= 0.5
        cells *= 2
    total /= norm
    lo, hi = total.min(), total.max()
    return (total - lo) / max(1e-6, hi - lo)


def fbm1d(rng, n, base=3, octaves=6, persistence=0.5):
    xs = np.linspace(0, 1, max(2, n))
    total = np.zeros(len(xs))
    amp, norm, cells = 1.0, 0.0, base
    for _ in range(octaves):
        pts = rng.random(cells + 1)
        total += amp * np.interp(xs * cells, np.arange(cells + 1), pts)
        norm += amp
        amp *= persistence
        cells *= 2
    lo, hi = total.min(), total.max()
    return (total - lo) / max(1e-6, hi - lo)


def with_draw(C, fn):
    """Run a PIL ImageDraw callback over the numpy canvas."""
    img = Image.fromarray(C)
    fn(ImageDraw.Draw(img))
    C[:] = np.asarray(img)


# --------------------------------------------------------------------------
# Scene elements
# --------------------------------------------------------------------------

def sky(C, rmp, y0=0, y1=H, dither=1.0):
    """Vertical gradient; rmp runs top colour -> horizon colour."""
    t = (YY - y0) / max(1, y1 - y0)
    ramp_fill(C, (YY >= y0) & (YY < y1), rmp, t, dither)


def stars(C, rng, count, y1, colours=("#ffffff", "#cfd8ff", "#ffe9b0")):
    for _ in range(count):
        x, y = int(rng.integers(0, W)), int(rng.integers(0, max(1, y1)))
        col = hexc(colours[int(rng.integers(0, len(colours)))])
        C[y, x] = col
        if rng.random() < 0.08 and 1 <= x < W - 1 and 1 <= y < H - 1:
            dim = (col.astype(int) * 2 // 3).astype(np.uint8)
            C[y - 1, x] = C[y + 1, x] = C[y, x - 1] = C[y, x + 1] = dim


def celestial(C, rng, cx, cy, r, rmp, halo=None, craters=True, bands=0,
              band_colour=None):
    d = np.hypot(XX - cx, YY - cy)
    disk = d <= r
    if halo:
        glow(C, disk, halo, radius=min(6, max(3, r // 3)), strength=0.55)
    lt = 1 - np.hypot(XX - (cx - r * 0.4), YY - (cy - r * 0.4)) / (1.8 * r)
    ramp_fill(C, disk, rmp, lt + 0.25, 0.8)
    if craters:
        for _ in range(int(r // 2) + 2):
            a, rr = rng.uniform(0, 6.28), rng.uniform(0, r * 0.7)
            kx, ky = cx + math.cos(a) * rr, cy + math.sin(a) * rr
            kr = rng.uniform(1, max(1.5, r / 5))
            m = disk & (np.hypot(XX - kx, YY - ky) <= kr)
            tint(C, m, "#000000", 0.18)
    if bands:
        for k in range(bands):
            y = int(cy + r * (0.1 + k * 0.22))
            h = 1 + k
            fill(C, disk & (YY >= y) & (YY < y + h), band_colour)


def puffs(C, blobs, rmp, bottom=None, dither=0.7, light=(-0.35, -0.55)):
    """Cauliflower cloud / smoke: overlapping lit spheres, lower (front)
    blobs drawn over upper ones so each puff gets its own bright cap."""
    mask = np.zeros((H, W), bool)
    lt = np.zeros((H, W), np.float32)
    for bx, by, r in sorted(blobs, key=lambda b: b[1]):
        x0, x1 = max(0, int(bx - r - 1)), min(W, int(bx + r + 2))
        y0, y1 = max(0, int(by - r - 1)), min(H, int(by + r + 2))
        if x0 >= x1 or y0 >= y1:
            continue
        X, Y = XX[y0:y1, x0:x1], YY[y0:y1, x0:x1]
        inside = (X - bx) ** 2 + (Y - by) ** 2 <= r * r
        l = 1 - np.hypot(X - (bx + light[0] * r),
                         Y - (by + light[1] * r)) / (1.55 * r)
        sub = lt[y0:y1, x0:x1]
        lt[y0:y1, x0:x1] = np.where(inside, l, sub)
        mask[y0:y1, x0:x1] |= inside
    if bottom is not None:
        mask &= YY <= bottom
        lt -= np.clip((YY - (bottom - 6)) / 6, 0, 1) * 0.3
    ramp_fill(C, mask, rmp, lt, dither)
    return mask


def cumulus(rng, cx, base, width, height):
    blobs = []
    n = max(3, int(width / 8))
    for i in range(n):
        u = (i + 0.5) / n
        hump = math.sin(math.pi * u)
        x = cx - width / 2 + u * width + rng.uniform(-2, 2)
        r = 2.5 + height * 0.42 * hump * rng.uniform(0.7, 1.2) + rng.uniform(0, 2)
        y = base - r * 0.5 - height * 0.3 * hump * rng.uniform(0.5, 1.0)
        blobs.append((x, y, r))
    for _ in range(max(1, int(n * 0.7))):
        u = rng.uniform(0.15, 0.85)
        hump = math.sin(math.pi * u)
        x = cx - width / 2 + u * width
        r = 2 + height * 0.36 * hump * rng.uniform(0.6, 1.1)
        y = base - height * hump * rng.uniform(0.55, 0.95) + r * 0.4
        blobs.append((x, y, r))
    return blobs


def cloud_layer(C, rng, rmp, y_lo, y_hi, count, w_range=(30, 90),
                h_range=(10, 30), dither=0.7):
    for _ in range(count):
        base = rng.uniform(y_lo, y_hi)
        depth = (base - y_lo) / max(1, y_hi - y_lo)
        w = rng.uniform(*w_range) * (0.6 + 0.6 * depth)
        h = rng.uniform(*h_range) * (0.6 + 0.6 * depth)
        puffs(C, cumulus(rng, rng.uniform(-20, W + 20), base, w, h), rmp,
              bottom=base, dither=dither)


def streaks(C, rng, rmp, y_lo, y_hi, count):
    """Thin stratus bands."""
    for _ in range(count):
        y = rng.uniform(y_lo, y_hi)
        x = rng.uniform(-40, W)
        length = rng.uniform(30, 110)
        blobs = [(x + k * 3, y + rng.uniform(-0.8, 0.8), rng.uniform(1.8, 3.2))
                 for k in range(int(length / 3))]
        puffs(C, blobs, rmp, bottom=y + 1, dither=0.5)


def massif(C, rng, x0, x1, base_y, height, rmp, bottom=H, cells=10,
           taper=False, dither=0.55, strata=0.0, snow=None, octaves=6,
           light_bias=0.0):
    """Mountain range / rock mound with lit left faces, shadowed right
    faces and crag lines -- the faceted look of painted pixel rocks."""
    n = max(2, x1 - x0)
    prof = fbm1d(rng, n, base=3, octaves=octaves)
    if taper:
        prof = prof * np.sin(np.linspace(0, math.pi, n)) ** 0.5
    xs = np.arange(x0, x0 + n)
    keep = (xs >= 0) & (xs < W)
    ridge = np.full(W, 1e9)
    ridge[xs[keep]] = (base_y - height * prof)[keep]
    mask = (YY >= ridge[None, :]) & (YY < bottom)
    F = fbm2d(rng, cells, 3, aspect=0.35)
    dF = np.gradient(F, axis=1) * (W / cells)
    slope = np.gradient(np.where(ridge > 1e8, base_y, ridge))
    depth = np.clip((YY - ridge[None, :]) / max(1.0, height), 0, 1)
    t = (0.55 + light_bias - np.clip(dF, -1.5, 1.5) * 0.28
         - np.clip(slope, -2, 2)[None, :] * 0.12 * (1 - depth)
         - depth * 0.18)
    if strata:
        t += np.sin(YY * 0.9 + F * 9) * strata
    ramp_fill(C, mask, rmp, t, dither)
    if snow is not None:
        cap = mask & (YY - ridge[None, :] < 3 + F * 14) & \
            (ridge[None, :] < base_y - height * 0.45)
        ramp_fill(C, cap, snow, t + 0.15, dither)
    return ridge


def column(C, rng, x, w, top, bottom, rmp, broken=0, capital=True,
           drum=11):
    x, w, top, bottom = int(x), int(w), int(top), int(bottom)
    tops = np.full(W, 1e9)
    jag = np.full(w, float(top))
    if broken:
        walk = np.cumsum(rng.integers(-2, 3, size=w)).astype(float)
        jag = top + np.clip(walk - walk.min(), 0, broken)
    for i in range(w):
        if 0 <= x + i < W:
            tops[x + i] = jag[i]
    XS = XX - x
    mask = (XS >= 0) & (XS < w) & (YY < bottom) & (YY >= tops[None, :])
    rel = XS / max(1, w - 1)
    flute = np.where((XS % 3 == 2) & (XS < w - 1), -0.22, 0.0)
    F = fbm2d(rng, 24, 2)
    joints = np.where(((YY - top) % drum) == 0, -0.3, 0.0)
    t = 0.92 - 0.75 * rel + flute + (F - 0.5) * 0.25 + joints
    ramp_fill(C, mask, rmp, t, 0.4)
    if capital and not broken:
        cap = rect(x - 2, top - 4, x + w + 2, top)
        ramp_fill(C, cap, rmp, 0.95 - 0.7 * (XX - x + 2) / (w + 4)
                  - np.where(YY == top - 1, 0.35, 0), 0.3)
    base = rect(x - 2, bottom - 3, x + w + 2, bottom)
    ramp_fill(C, base, rmp, 0.85 - 0.7 * (XX - x + 2) / (w + 4)
              - np.where(YY == bottom - 3, -0.1, 0), 0.3)


def block(C, x0, y0, x1, y1, rmp, rng, light_left=True, mortar=0):
    """Shaded stone block / wall; optional brick courses."""
    m = rect(x0, y0, x1, y1)
    rel = (XX - x0) / max(1, x1 - x0 - 1)
    F = fbm2d(rng, 20, 2)
    t = (0.8 - 0.55 * rel if light_left else 0.3 + 0.4 * rel) + (F - 0.5) * 0.3
    t = t + np.where(YY == y0, 0.25, 0) - np.where(YY == y1 - 1, 0.3, 0)
    if mortar:
        course = (YY - y0) // mortar
        joint = ((YY - y0) % mortar == 0) | \
            (((XX - x0) + course * (mortar + 2)) % (mortar * 2 + 3) == 0)
        t = t - np.where(joint, 0.28, 0)
    ramp_fill(C, m, rmp, t, 0.45)


def stairs(C, rng, x0, x1, y_top, steps, rise, run, rmp, left=True):
    for k in range(steps):
        y = y_top + k * rise
        if left:
            block(C, x0, y, x1 + k * run, y + rise, rmp, rng)
        else:
            block(C, x0 - k * run, y, x1, y + rise, rmp, rng)


def ground(C, rng, y0, rmp, tiles=True, highlight=0.3):
    mask = YY >= y0
    d = np.clip((YY - y0) / max(1, H - y0), 0, 1)
    F = fbm2d(rng, 10, 3)
    G = fbm2d(rng, 16, 2)
    t = 0.38 + (F - 0.5) * 0.45 + d * 0.15
    if tiles:
        rows = np.floor(np.clip((YY - y0) + 2, 0, None) ** 1.35 / 5)
        row_edge = rows != np.roll(rows, 1, axis=0)
        span = 10 + rows * 5
        vjoint = ((XX + rows * 17) % np.maximum(4, span)) == 0
        t = t - np.where(row_edge | vjoint, 0.22, 0)
    t = t + np.where(G > 0.74, highlight, 0)
    ramp_fill(C, mask, rmp, t, 0.6)


def water(C, rng, y0, rmp, mirror_from=None, ripple=1.0, strength=0.55):
    """Lake / wet street: mirrors the scene above y0 with ripples."""
    rows = np.arange(y0, H)
    for y in rows:
        src = 2 * y0 - y - 1 if mirror_from is None else mirror_from - (y - y0)
        src = int(np.clip(src, 0, H - 1))
        shift = int(round(math.sin(y * 0.9 + rng.uniform(0, 0.3)) * ripple
                          * (1 + (y - y0) / 25)))
        C[y] = np.roll(C[src], shift, axis=0)
    m = YY >= y0
    t = 0.35 + np.where(((YY + XX // 7) % 5 == 0) & (BAYER < 0.5), 0.35, 0)
    ref = C.copy()
    ramp_fill(ref, m, rmp, t, 0.6)
    C[m] = (C[m] * (1 - strength) + ref[m] * strength).astype(np.uint8)


def grass(C, rng, x0, x1, y_base, count, colour, max_h=16, lean=0.4):
    def draw(d):
        for _ in range(count):
            x = rng.uniform(x0, x1)
            h = rng.uniform(max_h * 0.3, max_h)
            dx = rng.uniform(-lean, lean) * h
            d.line((x, y_base, x + dx, y_base - h), fill=colour)
    with_draw(C, draw)


def rain(C, rng, count, colour, alpha=0.5, length=(3, 7)):
    col = hexc(colour).astype(float)
    for _ in range(count):
        x, y = rng.integers(0, W), rng.integers(0, H)
        n = int(rng.integers(*length))
        for k in range(n):
            xx, yy = (x - k // 2) % W, y + k
            if yy < H:
                C[yy, xx] = (C[yy, xx] * (1 - alpha) + col * alpha).astype(np.uint8)


# -- city --------------------------------------------------------------

def building(C, rng, x, w, top, bottom, body, windows, sign=None,
             far=False, ledge_every=0):
    x, w, top, bottom = int(x), int(w), int(top), int(bottom)
    m = rect(x, top, x + w, bottom)
    if not m.any():
        return
    rel = (XX - x) / max(1, w - 1)
    F = fbm2d(rng, 30, 2)
    t = 0.45 - 0.25 * rel + (F - 0.5) * 0.15 - (YY - top) / max(1, H) * 0.2
    t = t + np.where(XX - x < (1 if far else 2), 0.35, 0)
    if ledge_every:
        t = t + np.where(((YY - top) % ledge_every == 0), 0.25, 0)
    ramp_fill(C, m, body, t, 0.5)
    sx, sy = (3, 4) if far else (rng.choice([3, 4]), rng.choice([4, 5, 6]))
    ww, wh = (1, 1) if far else (rng.choice([1, 2]), rng.choice([1, 2, 3]))
    cx, cy = (XX - x - 2) // sx, (YY - top - 3) // sy
    cell = (((XX - x - 2) % sx) < ww) & (((YY - top - 3) % sy) < wh) & \
        (XX >= x + 2) & (XX < x + w - 2) & (YY >= top + 3) & (YY < bottom - 2)
    table = rng.random((H // 2 + 2, W // 2 + 2))
    pick = rng.integers(0, len(windows), size=table.shape)
    cyc = np.clip(cy, 0, table.shape[0] - 1)
    cxc = np.clip(cx, 0, table.shape[1] - 1)
    lit = cell & m & (table[cyc, cxc] < (0.28 if far else 0.32))
    for k, col in enumerate(windows):
        fill(C, lit & (pick[cyc, cxc] == k), col)
    dark = cell & m & ~lit
    tint(C, dark, "#000000", 0.35)
    if not far:  # roof clutter
        def roof(d):
            if rng.random() < 0.6:
                ax = x + int(rng.integers(2, max(3, w - 2)))
                d.line((ax, top, ax, top - rng.integers(4, 14)), fill=tuple(body[1]))
            if rng.random() < 0.5:
                bx = x + int(rng.integers(1, max(2, w - 6)))
                d.rectangle((bx, top - 3, bx + 4, top - 1), fill=tuple(body[1]))
        with_draw(C, roof)
    if sign:
        colour, vertical = sign
        if vertical:
            sx0 = x + w - 1 if rng.random() < 0.5 else x - 4
            sy0 = top + int(rng.integers(4, max(5, (bottom - top) // 3)))
            sm = rect(sx0, sy0, sx0 + 4, sy0 + int(rng.integers(14, 28)))
        else:
            sy0 = top + int(rng.integers(6, max(7, (bottom - top) // 2)))
            sm = rect(x + 2, sy0, x + w - 2, sy0 + 4)
        fill(C, sm, "#14081c")
        border = sm & ~(np.roll(sm, 1, 0) & np.roll(sm, -1, 0)
                        & np.roll(sm, 1, 1) & np.roll(sm, -1, 1))
        fill(C, border, colour)
        fill(C, sm & ~border & (((XX + YY) % 3) == 0), colour)
        glow(C, sm, colour, radius=4, strength=0.75)


def skyline(C, rng, y_base, h_lo, h_hi, body, windows, far=False,
            signs=(), sign_p=0.0, w_lo=10, w_hi=28):
    x = int(rng.integers(-10, 0))
    while x < W:
        w = int(rng.integers(w_lo, w_hi))
        h = int(rng.integers(h_lo, h_hi))
        sign = None
        if signs and rng.random() < sign_p:
            sign = (signs[int(rng.integers(0, len(signs)))], rng.random() < 0.6)
        building(C, rng, x, w, y_base - h, y_base, body, windows, sign, far,
                 ledge_every=0 if far else int(rng.choice([0, 8, 12])))
        x += w + int(rng.integers(0, 3))


# -- industry ----------------------------------------------------------

def chimney(C, rng, x, w, top, bottom, rmp, band=None):
    x, w = int(x), int(w)
    m = rect(x, top, x + w, bottom)
    rel = (XX - x) / max(1, w - 1)
    t = 0.25 + 0.65 * np.cos((rel - 0.3) * 2.2).clip(0, 1) + (fbm2d(rng, 30, 2) - 0.5) * 0.2
    ramp_fill(C, m, rmp, t, 0.4)
    if band is not None:
        for y in range(int(top) + 3, int(top) + 3 + 3 * 7, 7):
            bm = m & (YY >= y) & (YY < y + 3)
            ramp_fill(C, bm, band, t, 0.4)
    lip = rect(x - 1, top - 2, x + w + 1, top)
    ramp_fill(C, lip, rmp, 0.8 - 0.6 * (XX - x + 1) / (w + 2), 0.3)


def smoke_plume(C, rng, x, y, rmp, length=60, drift=1.0, r0=3):
    blobs = []
    cx, cy, r = x, y, r0
    for k in range(int(length / 3)):
        blobs.append((cx + rng.uniform(-1, 1), cy, r))
        cy -= rng.uniform(2, 4)
        cx += drift * rng.uniform(1.0, 2.5)
        r += rng.uniform(0.2, 0.7)
    puffs(C, blobs, rmp, dither=0.8)


def tank(C, rng, x, w, top, bottom, rmp):
    m = rect(x, top, x + w, bottom)
    rel = (XX - x) / max(1, w - 1)
    t = 0.2 + 0.7 * np.cos((rel - 0.3) * 2.0).clip(0, 1) + (fbm2d(rng, 30, 2) - 0.5) * 0.25
    t = t - np.where((YY - top) % 7 == 0, 0.2, 0)
    ramp_fill(C, m, rmp, t, 0.4)
    dome = (((XX - (x + w / 2)) / (w / 2)) ** 2 + ((YY - top) / (w / 5)) ** 2 <= 1) & (YY < top)
    ramp_fill(C, dome, rmp, 0.85 - 0.6 * rel, 0.4)


def pipe_h(C, x0, x1, y, d, rmp, joints=18):
    m = rect(x0, y, x1, y + d)
    rel = (YY - y) / max(1, d - 1)
    t = 0.95 - 0.85 * rel
    t = t - np.where(((XX - x0) % joints) < 2, 0.25, 0)
    ramp_fill(C, m, rmp, t, 0.35)


def factory(C, rng, x, w, top, bottom, rmp, window_colour, saw=True):
    block(C, x, top, x + w, bottom, rmp, rng)
    if saw:
        def teeth(d):
            tw = 10
            for tx in range(int(x), int(x + w - tw + 1), tw):
                d.polygon([(tx, top), (tx + tw - 1, top - 7), (tx + tw - 1, top)],
                          fill=tuple(rmp[2]))
                d.line((tx + tw - 1, top - 7, tx + tw - 1, top), fill=tuple(rmp[0]))
        with_draw(C, teeth)
    for wx in range(int(x) + 3, int(x + w) - 5, 7):
        if rng.random() < 0.55:
            wm = rect(wx, top + 5, wx + 4, top + 9)
            fill(C, wm, window_colour)
            glow(C, wm, window_colour, radius=2, strength=0.5)
        else:
            fill(C, rect(wx, top + 5, wx + 4, top + 9), "#120e0c")


def girders(C, x0, x1, y0, y1, colour):
    def draw(d):
        d.line((x0, y0, x1, y0), fill=colour)
        d.line((x0, y1, x1, y1), fill=colour)
        step = 6
        for gx in range(int(x0), int(x1), step):
            d.line((gx, y0, gx + step, y1), fill=colour)
            d.line((gx, y1, gx + step, y0), fill=colour)
    with_draw(C, draw)


def crane(C, rng, x, base, h, colour):
    def draw(d):
        d.line((x, base, x, base - h), fill=colour, width=2)
        arm = int(rng.integers(40, 70))
        d.line((x - 12, base - h, x + arm, base - h), fill=colour, width=2)
        d.line((x, base - h - 8, x + arm, base - h), fill=colour)
        d.line((x, base - h - 8, x - 12, base - h), fill=colour)
        hx = x + int(arm * rng.uniform(0.4, 0.9))
        d.line((hx, base - h, hx, base - h + int(rng.integers(15, 40))), fill=colour)
    with_draw(C, draw)
    girders(C, x - 2, x + 2, base - h, base, colour)


def fence(C, y_top, y_bot, colour):
    def draw(d):
        for fx in range(0, W, 7):
            d.line((fx, y_top, fx, y_bot), fill=colour)
        for fy in range(int(y_top) + 2, int(y_bot), 4):
            for fx in range(0, W, 4):
                d.point((fx + (fy // 4) % 2 * 2, fy), fill=colour)
        d.line((0, y_top, W, y_top), fill=colour)
    with_draw(C, draw)


# --------------------------------------------------------------------------
# Palettes
# --------------------------------------------------------------------------

LANDSCAPE_DAY = [
    dict(  # clear afternoon, teal sky (closest to the classic ruins look)
        sky=ramp("#24435a", "#2f5a72", "#3f7489", "#5b93a2", "#82b3b8"),
        cloud=ramp("#5c7f94", "#7da2af", "#a6c5c4", "#d1e1d2", "#f3efdb"),
        far=ramp("#52707f", "#62818e", "#7795a0", "#8fabb2"),
        mid=ramp("#3b4f63", "#4c6377", "#62798a", "#7b919e"),
        rock=ramp("#2d2028", "#4c3036", "#6e4541", "#94604f", "#b98063", "#d6a07c"),
        stone=ramp("#3e2c32", "#5f4245", "#83605a", "#a8806f", "#c9a183", "#e6c49f"),
        ground=ramp("#4b3a3e", "#6b5250", "#8d6f63", "#ae8f7a", "#d1b597", "#ecd9bb"),
        snow=ramp("#a9bfc6", "#cddbdc", "#eef3ee"),
        haze="#86aab2", plant="#1c1822"),
    dict(  # golden hour
        sky=ramp("#2c2550", "#583c69", "#9a5a6c", "#d4876c", "#f2be84"),
        cloud=ramp("#6a4362", "#995f6e", "#cd8a78", "#efb68b", "#fde2b1"),
        far=ramp("#6a4a6c", "#835c72", "#9d717b", "#b98b85"),
        mid=ramp("#3f2f4c", "#55405a", "#6d5266", "#876874"),
        rock=ramp("#241826", "#432736", "#683a3e", "#8f5446", "#b97552", "#dc9c66"),
        stone=ramp("#3b2632", "#5d3b40", "#85574d", "#ad7a5c", "#d29f70", "#f0c78d"),
        ground=ramp("#47303a", "#6b4846", "#916552", "#b98763", "#dcad7c", "#f6d59e"),
        snow=ramp("#c7a0a0", "#e8c4b0", "#fde6cc"),
        haze="#c38a84", plant="#1a1220"),
    dict(  # misty green valley
        sky=ramp("#2e4a4a", "#3e605a", "#56796d", "#77967f", "#a0b596"),
        cloud=ramp("#617a70", "#80998a", "#a6b8a4", "#cbd6c2", "#eef0e0"),
        far=ramp("#5a7569", "#6c8778", "#839b89", "#9db09b"),
        mid=ramp("#34483f", "#455b4f", "#5a7060", "#728874"),
        rock=ramp("#1f2522", "#333b34", "#4b5446", "#68705a", "#8a8f73", "#adae8f"),
        stone=ramp("#323631", "#4b5048", "#676b60", "#868a7b", "#a7a998", "#c9c8b6"),
        ground=ramp("#2c3a2a", "#3f5136", "#566a44", "#728652", "#93a366", "#b6c084"),
        snow=ramp("#b4c4b8", "#d4ded2", "#f0f4ea"),
        haze="#9db29c", plant="#141c16"),
    dict(  # desert noon
        sky=ramp("#2f5d86", "#3f73a0", "#5a8fb6", "#7fabc6", "#b5cdd2"),
        cloud=ramp("#8199b0", "#a2b6c6", "#c4d3da", "#e3ebe9", "#fbfaf2"),
        far=ramp("#8a8ea6", "#9d9fb0", "#b2b0b8", "#c8c2c0"),
        mid=ramp("#7a5e5c", "#916f62", "#a9836c", "#c09a7b"),
        rock=ramp("#3b2420", "#5f3527", "#86492f", "#ad6439", "#cf8649", "#eaa962"),
        stone=ramp("#4a3226", "#6e4a33", "#946642", "#b98754", "#d9a96b", "#f2cb8a"),
        ground=ramp("#7a5232", "#966a3f", "#b2844e", "#cc9f62", "#e2bb7c", "#f5d79c"),
        snow=ramp("#d6cfc6", "#ebe5dc", "#fdfbf4"),
        haze="#c3b9b0", plant="#2a1a14"),
]

LANDSCAPE_NIGHT = [
    dict(  # moonlit blue
        sky=ramp("#070a1c", "#0d1430", "#152147", "#21335e", "#334a78"),
        cloud=ramp("#141c36", "#1f2b4c", "#2f3f63", "#475a80", "#6a7ea0"),
        far=ramp("#1a2443", "#222f52", "#2c3b61", "#384a70"),
        mid=ramp("#0f1630", "#162040", "#1f2b50", "#2a3860"),
        rock=ramp("#0b0c18", "#151627", "#222238", "#33314a", "#48445e", "#615b76"),
        stone=ramp("#121322", "#1d1d33", "#2c2b45", "#3e3b58", "#55506e", "#706a88"),
        ground=ramp("#0e0f1d", "#18192b", "#24243a", "#33324a", "#45425c", "#5b5672"),
        snow=ramp("#6d7ea6", "#93a4c6", "#c2d0e8"),
        moon=ramp("#8f97b0", "#b8bfd2", "#dfe3ee", "#fbfbf4"),
        haze="#2c3e66", plant="#06070f", water=ramp("#0a1028", "#16224a", "#5b74a8")),
    dict(  # violet dusk
        sky=ramp("#120a26", "#26123c", "#43205a", "#6e3470", "#a65383"),
        cloud=ramp("#2d1838", "#46244c", "#663466", "#8f4e7e", "#c27a9a"),
        far=ramp("#3a2350", "#4a2c5c", "#5c3868", "#704676"),
        mid=ramp("#21143a", "#2c1a46", "#392354", "#482d62"),
        rock=ramp("#0f0a16", "#1d1226", "#2e1c38", "#43294c", "#5c3a62", "#7a4f7a"),
        stone=ramp("#191223", "#281b34", "#3a2848", "#4f385e", "#684c76", "#866490"),
        ground=ramp("#140e1e", "#20172c", "#30223c", "#43304e", "#5a4262", "#755878"),
        snow=ramp("#9a7aa6", "#c4a2c4", "#ecd2e4"),
        moon=ramp("#c48a9e", "#e2b0b6", "#f6d6cc", "#fff3e4"),
        haze="#6a3a6e", plant="#0a0610", water=ramp("#120a24", "#3a1c4c", "#c06a96")),
    dict(  # aurora
        sky=ramp("#040d14", "#082028", "#0c3438", "#16564a", "#2f8a64"),
        cloud=ramp("#0c1e24", "#123038", "#1c4448", "#2b6058", "#4a8a70"),
        far=ramp("#10262e", "#163038", "#1e3c42", "#284a4e"),
        mid=ramp("#0a171e", "#0f2028", "#152a32", "#1d363c"),
        rock=ramp("#060a0e", "#0d1418", "#162024", "#212e32", "#2f3f42", "#415456"),
        stone=ramp("#0c1216", "#151e22", "#202c30", "#2e3c40", "#3f5052", "#556868"),
        ground=ramp("#081010", "#0f1a1a", "#172626", "#223434", "#2f4444", "#405858"),
        snow=ramp("#5a8a86", "#86b4aa", "#c4e6d6"),
        moon=ramp("#88a8a0", "#b0ccc0", "#d8ece0", "#f6fff6"),
        haze="#1a4a44", plant="#030707", water=ramp("#04121a", "#0c3438", "#56c48e")),
    dict(  # blood moon
        sky=ramp("#0c0408", "#1e0810", "#36101a", "#561a24", "#7e2a2c"),
        cloud=ramp("#1e0a10", "#32121a", "#4c1c24", "#6c2c30", "#934240"),
        far=ramp("#2e1018", "#3a161e", "#481c24", "#58242a"),
        mid=ramp("#1c0a10", "#260e16", "#32141c", "#401a22"),
        rock=ramp("#0a0406", "#16080c", "#240e14", "#36161c", "#4a2026", "#622c30"),
        stone=ramp("#120808", "#200e10", "#321618", "#462024", "#5e2c2e", "#7a3c3a"),
        ground=ramp("#0e0608", "#1a0c0e", "#281216", "#381a1e", "#4a2428", "#603034"),
        snow=ramp("#8a4a4a", "#b06a62", "#d89a86"),
        moon=ramp("#8a1e1a", "#b8342a", "#e0583e", "#ff8a5c"),
        haze="#4a1820", plant="#050203", water=ramp("#10040a", "#36101a", "#d0503c")),
]

CITY = [
    dict(sky=ramp("#05030f", "#0d0624", "#1c0a3a", "#371052", "#5e1a6a"),
         cloud=ramp("#1a0c30", "#2c1244", "#46185c", "#6c2476", "#a03a8c"),
         far=ramp("#140a2a", "#1c0f36", "#251542", "#2e1b4e"),
         body=ramp("#06040e", "#0c0818", "#140e24", "#1e1632", "#2a2042"),
         windows=("#ff3fa4", "#2ee6ff", "#ffe066", "#b06cff"),
         signs=("#ff2fa0", "#2ef0ff", "#ffe040"),
         water=ramp("#06040e", "#2a0c48", "#ff4fb0"), rain="#7fd8ff",
         sun=ramp("#ff2a6d", "#ff5a7a", "#ff9a6a", "#ffd166"), grid="#ff2fa0",
         grid2="#2ee6ff"),
    dict(sky=ramp("#020810", "#04142a", "#062444", "#0a3a5e", "#12567a"),
         cloud=ramp("#08182c", "#0c2640", "#123a58", "#1c5274", "#2e7494"),
         far=ramp("#061424", "#0a1c30", "#0e243c", "#142e48"),
         body=ramp("#02060c", "#060c16", "#0a1420", "#101c2c", "#18283a"),
         windows=("#ff8c1a", "#38f2d0", "#fff1a8", "#ff4a3a"),
         signs=("#ff7a00", "#00ffd0", "#ff3050"),
         water=ramp("#02060c", "#0a2a40", "#ff8c1a"), rain="#9fe8ff",
         sun=ramp("#ff4a1a", "#ff7a2a", "#ffae3a", "#ffe08a"), grid="#ff7a00",
         grid2="#00ffd0"),
    dict(sky=ramp("#030a06", "#08180e", "#0e2a18", "#184226", "#245e34"),
         cloud=ramp("#0a1c10", "#102a18", "#1a4024", "#285a34", "#3e7c48"),
         far=ramp("#08160e", "#0c1e14", "#10281a", "#163220"),
         body=ramp("#020604", "#060c08", "#0a140e", "#101c14", "#18281c"),
         windows=("#5aff7a", "#c06cff", "#e6ff5a", "#3affd8"),
         signs=("#3aff6a", "#c040ff", "#e8ff3a"),
         water=ramp("#020604", "#0a2a14", "#5aff7a"), rain="#b0ffc8",
         sun=ramp("#2aff8a", "#7aff6a", "#c8ff5a", "#f6ff9a"), grid="#3aff6a",
         grid2="#c040ff"),
    dict(sky=ramp("#0a0206", "#1e0410", "#34081c", "#520c28", "#781838"),
         cloud=ramp("#1e0612", "#320a1e", "#4c102c", "#6e1a3e", "#9a2a54"),
         far=ramp("#18040e", "#220816", "#2c0c1e", "#381226"),
         body=ramp("#060104", "#0e0408", "#16080e", "#200c16", "#2c1220"),
         windows=("#ff3048", "#ffb020", "#3ad0ff", "#ff6ad0"),
         signs=("#ff1a40", "#ffc020", "#30c8ff"),
         water=ramp("#060104", "#3a0818", "#ff3048"), rain="#ffb0c0",
         sun=ramp("#ff1a40", "#ff4a40", "#ff8a3a", "#ffd25a"), grid="#ff1a40",
         grid2="#ffc020"),
]

INDUSTRY = [
    dict(  # rust sunset smog
        sky=ramp("#2a1612", "#4a2216", "#7a3418", "#b4521c", "#e08a3a"),
        smoke=ramp("#2a201e", "#433430", "#5e4a42", "#7c6455", "#9c7f6a"),
        far=ramp("#3a2420", "#4a2c24", "#5c3628", "#6e402e"),
        metal=ramp("#120e0e", "#221a18", "#382a24", "#544034", "#745a46", "#987a5c"),
        rust=ramp("#2a120a", "#4a2010", "#743216", "#a04a1e", "#c86a2a"),
        ground=ramp("#120c0a", "#1e1612", "#2c201a", "#3c2c22", "#4e3a2c", "#62482f"),
        window="#ffb03a", sun=ramp("#e04a1a", "#f07a2a", "#ffb04a", "#ffe08a"),
        haze="#8a4a28", dark="#0c0806", water=ramp("#0c0806", "#3a2014", "#e08a3a")),
    dict(  # toxic green
        sky=ramp("#0c140a", "#162410", "#243a14", "#3c5a1a", "#6a8a26"),
        smoke=ramp("#1a2016", "#2a3222", "#3e4830", "#566240", "#728050"),
        far=ramp("#1c2616", "#24301c", "#2e3c22", "#384828"),
        metal=ramp("#0a0c0a", "#161a14", "#242a20", "#363e2e", "#4c543e", "#666e52"),
        rust=ramp("#1e1a0a", "#343010", "#4e4a16", "#6c6a1e", "#8e8c2a"),
        ground=ramp("#0a0c08", "#141810", "#1e2418", "#2a3220", "#38422a", "#485436"),
        window="#c8ff3a", sun=ramp("#8ab41a", "#b0d42a", "#d8f04a", "#f4ff9a"),
        haze="#4a6a20", dark="#060806", water=ramp("#060806", "#1e3010", "#b4f03a")),
    dict(  # ash grey overcast
        sky=ramp("#1a1c20", "#2a2c30", "#3c3e42", "#525458", "#6c6e70"),
        smoke=ramp("#222326", "#343538", "#48494c", "#606164", "#7c7d7e"),
        far=ramp("#2a2c30", "#323438", "#3c3e42", "#46484c"),
        metal=ramp("#0c0c0e", "#18181a", "#262628", "#383838", "#4e4c4a", "#68645e"),
        rust=ramp("#24140e", "#3e2014", "#5e301a", "#82441e", "#a85c26"),
        ground=ramp("#0e0e0e", "#181818", "#222222", "#2e2e2c", "#3c3a38", "#4c4a46"),
        window="#ffd25a", sun=ramp("#6c6e70", "#8a8a8a", "#a8a8a6", "#c8c8c2"),
        haze="#4a4c50", dark="#080808", water=ramp("#080808", "#2a2c30", "#8a8c8e")),
    dict(  # furnace night
        sky=ramp("#06040a", "#0e0812", "#1a0e16", "#2e141a", "#4a1c1a"),
        smoke=ramp("#140c0e", "#221418", "#341e20", "#4c2a28", "#6c3c30"),
        far=ramp("#140a0e", "#1a0e12", "#221216", "#2a161a"),
        metal=ramp("#060406", "#0e0a0c", "#181214", "#261c1c", "#382826", "#4e3830"),
        rust=ramp("#1a0a06", "#2e1008", "#4a1a0a", "#6e280e", "#963a14"),
        ground=ramp("#060404", "#0e0a08", "#16100c", "#201812", "#2c2018", "#3a2a1e"),
        window="#ff6a1a", sun=ramp("#ff3a0a", "#ff6a1a", "#ffa03a", "#ffd88a"),
        haze="#3a1418", dark="#040203", water=ramp("#040203", "#2a0c0a", "#ff6a1a")),
]


# --------------------------------------------------------------------------
# Compositions
# --------------------------------------------------------------------------

def _canvas():
    return np.zeros((H, W, 3), np.uint8)


def landscape(rng, p, comp, night):
    C = _canvas()
    horizon = int(rng.integers(108, 124))
    sky(C, p["sky"], 0, horizon + 20)
    if night:
        stars(C, rng, 140, horizon)
        mx, my = int(rng.integers(40, 280)), int(rng.integers(18, 50))
        celestial(C, rng, mx, my, int(rng.integers(9, 15)), p["moon"],
                  halo=tuple_hex(p["moon"][1]))
    cloud_layer(C, rng, p["cloud"], 18, horizon - 30,
                count=int(rng.integers(4, 7)) if not night else 3,
                w_range=(40, 110), h_range=(10, 28))
    streaks(C, rng, p["cloud"], 10, 60, 2)
    massif(C, rng, -10, W + 10, horizon, int(rng.integers(35, 60)),
           hazed(p["far"], p["haze"], 0.25), cells=12,
           snow=hazed(p["snow"], p["haze"], 0.2) if comp == 2 else None)
    massif(C, rng, -10, W + 10, horizon + 6, int(rng.integers(14, 26)),
           p["mid"], cells=16, octaves=7)
    if comp in (1, 2):
        lake = horizon + 6
        water(C, rng, lake, p.get("water", hazed(p["mid"], p["haze"], 0.4)),
              ripple=0.8, strength=0.45)
    else:
        streaks(C, rng, hazed(p["cloud"], p["haze"], 0.5), horizon - 4, horizon + 4, 3)
    ground_y = int(rng.integers(150, 160))

    if comp == 0:  # temple tower left, broken columns right
        tx = int(rng.integers(-6, 14))
        tw = int(rng.integers(60, 80))
        massif(C, rng, tx - 20, tx + tw + 60, ground_y, 60, p["rock"], cells=14,
               taper=True, strata=0.06, light_bias=0.05)
        top = int(rng.integers(6, 22))
        block(C, tx, top, tx + tw, top + 10, p["stone"], rng, mortar=5)
        block(C, tx + 4, top - 8, tx + tw - 4, top, p["stone"], rng, mortar=4)
        for k in range(3):
            column(C, rng, tx + 4 + k * (tw - 14) // 2, 9, top + 14, ground_y - 22,
                   p["stone"], capital=True)
        block(C, tx - 4, top + 10, tx + tw + 4, top + 14, p["stone"], rng)
        stairs(C, rng, tx - 10, tx + tw, ground_y - 22, 6, 4, 5, p["stone"])
        rx = int(rng.integers(220, 260))
        mound = massif(C, rng, rx - 40, W + 30, ground_y, 46, p["rock"],
                       cells=14, taper=True, strata=0.07)
        for cx, cw, ch in ((rx, 12, 55), (rx + 26, 13, 76)):
            foot = int(min(ground_y, mound[cx: cx + cw].max() + 3))
            column(C, rng, cx, cw, foot - ch, foot, p["stone"],
                   broken=8, capital=False)
    elif comp == 1:  # cliffs framing a lake, arch ruin
        massif(C, rng, -30, 110, ground_y + 10, 140, p["rock"], cells=10,
               taper=True, strata=0.08, light_bias=0.08)
        massif(C, rng, 220, W + 40, ground_y + 10, 120, p["rock"], cells=10,
               taper=True, strata=0.08, light_bias=-0.05)
        ax = int(rng.integers(130, 180))
        column(C, rng, ax, 10, ground_y - 60, ground_y - 4, p["stone"])
        column(C, rng, ax + 40, 10, ground_y - 60, ground_y - 4, p["stone"],
               broken=12, capital=False)
        block(C, ax - 4, ground_y - 68, ax + 30, ground_y - 62, p["stone"], rng,
              mortar=3)
    else:  # plateau: lone column, rocks, snowy peaks behind
        massif(C, rng, -20, 150, ground_y + 6, 50, p["rock"], cells=14,
               taper=True, strata=0.06)
        cx = int(rng.integers(190, 250))
        column(C, rng, cx, 14, ground_y - 96, ground_y - 2, p["stone"])
        block(C, cx - 18, ground_y - 6, cx + 34, ground_y, p["stone"], rng,
              mortar=3)

    ground(C, rng, ground_y, p["ground"])
    grass(C, rng, W - 70, W, H, 70, tuple_hex(p["plant"]), max_h=22)
    grass(C, rng, 0, 50, H, 40, tuple_hex(p["plant"]), max_h=14)
    return C


def tuple_hex(c):
    if isinstance(c, str):
        return tuple(int(v) for v in hexc(c))
    return tuple(int(v) for v in c)


def city(rng, p, comp):
    C = _canvas()
    if comp == 2:  # synthwave horizon: big sun, grid floor, small skyline
        horizon = 112
        sky(C, p["sky"], 0, horizon)
        stars(C, rng, 90, horizon - 30)
        celestial(C, rng, 160, horizon - 18, 38, p["sun"], halo=p["signs"][0],
                  craters=False, bands=5, band_colour=tuple_hex(p["sky"][1]))
        streaks(C, rng, p["cloud"], 30, 80, 6)
        massif(C, rng, -10, W + 10, horizon, 34, hazed(p["far"], p["grid"], 0.15),
               cells=10)
        skyline(C, rng, horizon, 8, 34, p["far"], p["windows"], far=True,
                w_lo=6, w_hi=14)
        fill(C, YY >= horizon, "#05020c")
        def grid(d):
            for k in range(-26, 27):
                d.line((160 + k * 6, horizon, 160 + k * 40, H), fill=tuple_hex(p["grid"]))
            y, step = horizon + 1, 1.5
            while y < H:
                d.line((0, int(y), W, int(y)), fill=tuple_hex(p["grid2"]))
                y += step
                step *= 1.35
        with_draw(C, grid)
        glow(C, (YY == horizon), p["grid"], radius=3, strength=0.6)
        return C

    sky(C, p["sky"], 0, H)
    stars(C, rng, 60, 70)
    if rng.random() < 0.6:
        celestial(C, rng, int(rng.integers(40, 280)), int(rng.integers(16, 40)),
                  int(rng.integers(8, 13)), ramp("#8a8aa0", "#b8b8c8", "#e6e6ee", "#ffffff"),
                  halo=p["windows"][1])
    cloud_layer(C, rng, p["cloud"], 20, 70, 3, w_range=(60, 140), h_range=(8, 18))
    if comp == 0:  # street canyon
        street = 146
        skyline(C, rng, street, 50, 110, p["far"], p["windows"], far=True,
                w_lo=8, w_hi=18)
        tint(C, YY < street, p["sky"][3], 0.15)
        for side in (0, 1):
            x = -4 if side == 0 else W - 1
            for _ in range(3):
                w = int(rng.integers(34, 56))
                bx = x if side == 0 else x - w
                top = int(rng.integers(-20, 40))
                building(C, rng, bx, w, top, street, p["body"], p["windows"],
                         sign=(p["signs"][int(rng.integers(0, len(p["signs"])))], True),
                         ledge_every=int(rng.choice([6, 9])))
                x = bx + w + 2 if side == 0 else bx - 2
        water(C, rng, street, p["water"], ripple=1.2, strength=0.5)
        rain(C, rng, 260, p["rain"], 0.3)
    else:  # skyline across the bay
        shore = 128
        skyline(C, rng, shore, 20, 70, p["far"], p["windows"], far=True,
                w_lo=6, w_hi=16)
        skyline(C, rng, shore, 30, 95, p["body"], p["windows"],
                signs=p["signs"], sign_p=0.45, w_lo=14, w_hi=30)
        water(C, rng, shore, p["water"], ripple=1.0, strength=0.45)
        if rng.random() < 0.5:
            rain(C, rng, 400, p["rain"], 0.3)
    return C


def industry(rng, p, comp):
    C = _canvas()
    horizon = int(rng.integers(118, 132))
    sky(C, p["sky"], 0, horizon + 10)
    sx, sy = int(rng.integers(60, 260)), horizon - int(rng.integers(10, 40))
    celestial(C, rng, sx, sy, int(rng.integers(14, 24)), p["sun"],
              halo=p["window"], craters=False)
    cloud_layer(C, rng, p["smoke"], 20, horizon - 40, 4, w_range=(60, 140),
                h_range=(10, 24), dither=0.9)
    streaks(C, rng, p["smoke"], 30, horizon - 10, 6)
    massif(C, rng, -10, W + 10, horizon, 24, hazed(p["far"], p["haze"], 0.2),
           cells=8)

    if comp == 0:  # factory skyline with chimneys
        stacks = []
        for _ in range(int(rng.integers(3, 6))):
            cx = int(rng.integers(10, W - 20))
            cw = int(rng.integers(8, 14))
            top = int(rng.integers(20, 70))
            stacks.append((cx, cw, top))
        for cx, cw, top in stacks:
            smoke_plume(C, rng, cx + cw / 2, top - 3, p["smoke"],
                        length=int(rng.integers(40, 90)), drift=rng.uniform(0.6, 1.6))
        for cx, cw, top in stacks:
            chimney(C, rng, cx, cw, top, horizon + 10, p["metal"], band=p["rust"])
        x = int(rng.integers(-20, 0))
        while x < W:
            fw = int(rng.integers(50, 90))
            factory(C, rng, x, fw, horizon - int(rng.integers(14, 30)), horizon + 14,
                    p["metal"], p["window"])
            x += fw + int(rng.integers(0, 10))
        if rng.random() < 0.7:
            crane(C, rng, int(rng.integers(30, 290)), horizon, int(rng.integers(50, 80)),
                  tuple_hex(p["metal"][1]))
        ground(C, rng, horizon + 14, p["ground"], tiles=False, highlight=0.15)
        fence(C, 150, H, tuple_hex(p["dark"]))
    elif comp == 1:  # refinery close-up: tanks, pipes, catwalks, steam
        for _ in range(int(rng.integers(3, 5))):
            tx = int(rng.integers(-10, W - 30))
            tw = int(rng.integers(30, 56))
            tt = int(rng.integers(60, 100))
            smoke_plume(C, rng, tx + tw * 0.6, tt - 6, p["smoke"], length=50,
                        drift=rng.uniform(0.4, 1.2), r0=2)
            tank(C, rng, tx, tw, tt, H, p["metal"])
        for k in range(int(rng.integers(3, 6))):
            y = int(rng.integers(110, 170))
            pipe_h(C, -2, W + 2, y, int(rng.integers(3, 7)),
                   p["rust"] if k % 2 else p["metal"])
        girders(C, 0, W, 98, 104, tuple_hex(p["metal"][2]))
        for gx in range(0, W, 40):
            fill(C, rect(gx, 104, gx + 2, H), tuple_hex_str(p["metal"][1]))
        for _ in range(8):
            lx, ly = int(rng.integers(0, W)), int(rng.integers(100, 170))
            lm = rect(lx, ly, lx + 2, ly + 2)
            fill(C, lm, p["window"])
            glow(C, lm, p["window"], radius=3, strength=0.7)
    else:  # wasteland: distant plant, puddles, wreck, fence
        x = int(rng.integers(60, 180))
        for k in range(3):
            chimney(C, rng, x + k * 14, 6, horizon - int(rng.integers(30, 55)),
                    horizon + 2, hazed(p["metal"], p["haze"], 0.3))
            smoke_plume(C, rng, x + k * 14 + 3, horizon - 58, p["smoke"],
                        length=60, drift=1.4, r0=2)
        factory(C, rng, x - 30, 100, horizon - 14, horizon + 2,
                hazed(p["metal"], p["haze"], 0.3), p["window"])
        ground(C, rng, horizon + 2, p["ground"], tiles=False, highlight=0.2)
        for _ in range(4):
            px, py = int(rng.integers(0, W - 40)), int(rng.integers(horizon + 14, H - 6))
            pw = int(rng.integers(20, 60))
            pm = (((XX - px - pw / 2) / (pw / 2)) ** 2 + ((YY - py) / 3) ** 2) <= 1
            ramp_fill(C, pm, p["water"], 0.4 + 0.3 * (BAYER > 0.6), 0.4)
        massif(C, rng, -20, 120, H, 40, p["metal"], cells=20, taper=True,
               strata=0.1, octaves=7)
        massif(C, rng, 200, W + 20, H, 34, p["rust"], cells=20, taper=True,
               strata=0.1, octaves=7)
        fence(C, 136, 168, tuple_hex(p["dark"]))
    rain(C, rng, 300 if comp != 1 else 120, "#000000", 0.18, (2, 4))  # grit
    return C


def tuple_hex_str(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_scene(vibe, index):
    """Return a 320x180 PIL image for scene *index* (0-based) of *vibe*."""
    seed = zlib.crc32(f"{vibe}:{index}".encode())
    rng = np.random.default_rng(seed)
    comp = (index // 4) % 3
    if vibe == "cyber_neon":
        C = city(rng, CITY[index % len(CITY)], comp)
    elif vibe == "industrial_grunge":
        C = industry(rng, INDUSTRY[index % len(INDUSTRY)], comp)
    elif vibe == "midnight_dusk":
        C = landscape(rng, LANDSCAPE_NIGHT[index % len(LANDSCAPE_NIGHT)], comp,
                      night=True)
    else:
        C = landscape(rng, LANDSCAPE_DAY[index % len(LANDSCAPE_DAY)], comp,
                      night=False)
    return Image.fromarray(C, "RGB")


def generate_all(out_dir, force=False, vibes=VIBES, log=None):
    written = 0
    for vibe in vibes:
        folder = os.path.join(out_dir, vibe)
        os.makedirs(folder, exist_ok=True)
        existing = [n for n in os.listdir(folder) if n.lower().endswith(".png")]
        if existing and not force:
            continue
        for n in range(SCENES_PER_VIBE):
            generate_scene(vibe, n).save(os.path.join(folder, f"image_{n + 1:02d}.png"))
            written += 1
            if log:
                log(f"{vibe}/image_{n + 1:02d}.png")
    return written


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    count = generate_all(os.path.join(here, "assets", "visuals"),
                         force="--force" in sys.argv, log=print)
    print(f"{count} image(s) written")
