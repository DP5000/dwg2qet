#!/usr/bin/env python3
"""dwg2qet - convert DWG/DXF drawings into QElectroTech elements (.elmt).

What it adds to a plain geometry conversion:

* DWG input (through LibreDWG's ``dwg2dxf`` when it is installed).
* ``symbol`` mode for schematics: finds the terminals (free line ends on the
  outline), scales the drawing so they land on QElectroTech's 10 px grid,
  and adds label / comment fields, so the symbol can be wired straight away.
* ``thumbnail`` mode for cabinet layouts: a front view at a fixed px per mm,
  with the real width / height / depth stored in the element information,
  ready for 2D panel drawings and 3D export.
* Element information (manufacturer, article number, EAN, dimensions ...) and
  translated names, so a placed element fills the bill of materials.
* Batch conversion from a CSV manifest into a categorised collection.
* An SVG preview of every element, to check a conversion without QElectroTech.

Python 3.9+ standard library only. MIT licence.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# DXF reading
# --------------------------------------------------------------------------

CODEPAGES = {"ANSI_1250": "cp1250", "ANSI_1251": "cp1251", "ANSI_1252": "cp1252",
             "ANSI_1253": "cp1253", "ANSI_1254": "cp1254", "ANSI_1257": "cp1257",
             "ANSI_932": "cp932", "ANSI_936": "gbk", "ANSI_949": "cp949", "ANSI_950": "cp950"}


def _decode(raw: bytes) -> str:
    """DXF text: UTF-8 from AutoCAD 2007 on, the drawing's code page before."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    head = raw[:20000].decode("latin-1")
    m = re.search(r"\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n\s*(\S+)", head)
    enc = CODEPAGES.get(m.group(1).upper(), "cp1252") if m else "cp1252"
    return raw.decode(enc, errors="replace")


def _unescape(s: str) -> str:
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)


def read_pairs(path: Path) -> list:
    text = _decode(path.read_bytes())
    lines = text.splitlines()
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            out.append((int(lines[i].strip()), lines[i + 1].rstrip("\r")))
        except ValueError:
            continue
    return out


@dataclass
class Entity:
    type: str
    codes: list = field(default_factory=list)      # [(code, value), ...] in file order
    children: list = field(default_factory=list)   # VERTEX / ATTRIB after a POLYLINE / INSERT

    def get(self, code, default=None, cast=str):
        for c, v in self.codes:
            if c == code:
                try:
                    return cast(v.strip()) if cast is not str else v
                except ValueError:
                    return default
        return default

    def all(self, code, cast=float):
        out = []
        for c, v in self.codes:
            if c == code:
                try:
                    out.append(cast(v.strip()))
                except ValueError:
                    pass
        return out

    @property
    def layer(self):
        return self.get(8, "0").strip()


@dataclass
class Drawing:
    header: dict
    layers: dict          # name -> {"off": bool, "frozen": bool}
    blocks: dict          # name -> (base (x, y), [Entity])
    entities: list


def _entities(pairs, start, stop_values):
    """Read entities from pairs[start] until a (0, v) with v in stop_values."""
    ents, i = [], start
    cur = None
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0:
            vs = v.strip()
            if vs in stop_values:
                if cur:
                    ents.append(cur)
                return ents, i
            if vs in ("VERTEX", "ATTRIB") and cur is not None and cur.type in ("POLYLINE", "INSERT"):
                child = Entity(vs)
                i += 1
                while i < len(pairs) and pairs[i][0] != 0:
                    child.codes.append(pairs[i]); i += 1
                cur.children.append(child)
                continue
            if vs == "SEQEND":
                i += 1
                while i < len(pairs) and pairs[i][0] != 0:
                    i += 1
                continue
            if cur:
                ents.append(cur)
            cur = Entity(vs)
        elif cur is not None:
            cur.codes.append((c, v))
        i += 1
    if cur:
        ents.append(cur)
    return ents, i


def read_dxf(path: Path) -> Drawing:
    pairs = read_pairs(path)
    header, layers, blocks, entities = {}, {}, {}, []
    i = 0
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0 and v.strip() == "SECTION" and i + 1 < len(pairs):
            name = pairs[i + 1][1].strip()
            i += 2
            if name == "HEADER":
                var = None
                while i < len(pairs) and not (pairs[i][0] == 0 and pairs[i][1].strip() == "ENDSEC"):
                    c, v = pairs[i]
                    if c == 9:
                        var = v.strip(); header[var] = {}
                    elif var:
                        header[var][c] = v.strip()
                    i += 1
            elif name == "TABLES":
                while i < len(pairs) and not (pairs[i][0] == 0 and pairs[i][1].strip() == "ENDSEC"):
                    c, v = pairs[i]
                    if c == 0 and v.strip() == "LAYER":
                        rec = {}
                        i += 1
                        while i < len(pairs) and pairs[i][0] != 0:
                            rec.setdefault(pairs[i][0], pairs[i][1].strip()); i += 1
                        nm = rec.get(2)
                        if nm:
                            flags = int(rec.get(70, "0") or 0)
                            color = int(rec.get(62, "7") or 7)
                            layers[nm] = {"frozen": bool(flags & 1), "off": color < 0}
                        continue
                    i += 1
            elif name == "BLOCKS":
                while i < len(pairs) and not (pairs[i][0] == 0 and pairs[i][1].strip() == "ENDSEC"):
                    c, v = pairs[i]
                    if c == 0 and v.strip() == "BLOCK":
                        rec = []
                        i += 1
                        while i < len(pairs) and pairs[i][0] != 0:
                            rec.append(pairs[i]); i += 1
                        bname, bx, by = None, 0.0, 0.0
                        for cc, vv in rec:
                            if cc == 2 and bname is None:
                                bname = vv.strip()
                            elif cc == 10:
                                bx = float(vv)
                            elif cc == 20:
                                by = float(vv)
                        ents, i = _entities(pairs, i, ("ENDBLK",))
                        if bname:
                            blocks[bname] = ((bx, by), ents)
                        continue
                    i += 1
            elif name == "ENTITIES":
                entities, i = _entities(pairs, i, ("ENDSEC",))
        i += 1
    return Drawing(header, layers, blocks, entities)


def dwg_to_dxf(src: Path, workdir: Path) -> Path:
    exe = shutil.which("dwg2dxf")
    if not exe:
        raise RuntimeError("DWG input needs LibreDWG's dwg2dxf (macOS: brew install libredwg; "
                           "Debian/Ubuntu: apt install libredwg-tools), or convert to DXF first")
    out = workdir / (src.stem + ".dxf")
    subprocess.run([exe, "-y", "-o", str(out), str(src)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=120)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"dwg2dxf could not convert {src}")
    return out


def load(path: Path, workdir: Path | None = None) -> Drawing:
    path = Path(path)
    if path.suffix.lower() == ".dwg":
        with tempfile.TemporaryDirectory() as tmp:
            return read_dxf(dwg_to_dxf(path, Path(workdir or tmp)))
    return read_dxf(path)


# --------------------------------------------------------------------------
# Geometry: flatten everything into primitives in drawing units (y up)
# --------------------------------------------------------------------------

@dataclass
class Prim:
    kind: str                 # polyline | circle | arc | ellipse | text
    pts: list = field(default_factory=list)   # polyline points
    closed: bool = False
    filled: str = ""          # "" | "solid" | "hatch"
    cx: float = 0.0
    cy: float = 0.0
    r: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    a0: float = 0.0           # arc start, degrees CCW (drawing coordinates)
    a1: float = 0.0           # arc end
    text: str = ""
    h: float = 0.0
    rot: float = 0.0
    layer: str = ""


class Xf:
    """2D affine transform (block inserts)."""

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, e=0.0, f=0.0):
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    def __call__(self, x, y):
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def then(self, o: "Xf") -> "Xf":   # self, then o
        return Xf(o.a * self.a + o.c * self.b, o.b * self.a + o.d * self.b,
                  o.a * self.c + o.c * self.d, o.b * self.c + o.d * self.d,
                  o.a * self.e + o.c * self.f + o.e, o.b * self.e + o.d * self.f + o.f)

    @property
    def scale(self):
        return math.sqrt(abs(self.a * self.d - self.b * self.c)) or 1.0

    @property
    def angle(self):
        return math.degrees(math.atan2(self.b, self.a))

    @property
    def mirrored(self):
        return self.a * self.d - self.b * self.c < 0


def _bulge_points(p0, p1, bulge, seg_deg=10.0):
    """Points of the arc from p0 to p1 with a DXF bulge (excluding p0)."""
    if abs(bulge) < 1e-9:
        return [p1]
    theta = 4 * math.atan(bulge)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return [p1]
    r = chord / (2 * math.sin(abs(theta) / 2))
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    h = r * math.cos(theta / 2)
    nx, ny = -dy / chord, dx / chord
    sgn = 1 if bulge > 0 else -1
    cx, cy = mx + sgn * h * nx * (1 if abs(theta) <= math.pi else -1), my + sgn * h * ny * (1 if abs(theta) <= math.pi else -1)
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    n = max(2, int(abs(math.degrees(theta)) / seg_deg))
    return [(cx + r * math.cos(a0 + theta * k / n), cy + r * math.sin(a0 + theta * k / n)) for k in range(1, n + 1)]


def _poly_from_vertices(verts, bulges, closed):
    pts = [verts[0]]
    count = len(verts) if closed else len(verts) - 1
    for k in range(count):
        p0, p1 = verts[k], verts[(k + 1) % len(verts)]
        pts += _bulge_points(p0, p1, bulges[k] if k < len(bulges) else 0.0)
    return pts


def _bspline(ctrl, degree, knots, n=40):
    """Evaluate a B-spline (de Boor) at n+1 points."""
    if not knots or len(knots) != len(ctrl) + degree + 1:
        return list(ctrl)
    lo, hi = knots[degree], knots[len(ctrl)]
    out = []
    for k in range(n + 1):
        t = lo + (hi - lo) * k / n
        s = degree
        while s < len(ctrl) - 1 and t >= knots[s + 1]:
            s += 1
        d = [list(ctrl[j + s - degree]) for j in range(degree + 1)]
        for rr in range(1, degree + 1):
            for j in range(degree, rr - 1, -1):
                i = j + s - degree
                den = knots[i + degree - rr + 1] - knots[i]
                alpha = 0 if den == 0 else (t - knots[i]) / den
                d[j] = [(1 - alpha) * d[j - 1][q] + alpha * d[j][q] for q in (0, 1)]
        out.append(tuple(d[degree]))
    return out


def _mtext_plain(s: str) -> str:
    s = s.replace("\\P", "\n").replace("\\~", " ")
    s = re.sub(r"\\[A-Za-z][^;\\{}]*;", "", s)   # \fArial|b0;  \H2.5;  \C1; ...
    s = re.sub(r"\\[LlOoKk]", "", s)
    s = s.replace("{", "").replace("}", "")
    return _unescape(s)


def flatten(dwg: Drawing, include=None, exclude=None, keep_dimensions=False,
            keep_attdefs=False, max_depth=8) -> list:
    """All visible geometry as primitives in drawing coordinates."""
    prims: list = []

    def visible(layer):
        info = dwg.layers.get(layer, {})
        if info.get("off") or info.get("frozen"):
            return False
        if include and not any(re.fullmatch(p, layer, re.I) for p in include):
            return False
        if exclude and any(re.fullmatch(p, layer, re.I) for p in exclude):
            return False
        return True

    def emit(ent: Entity, xf: Xf, depth: int, inherited_layer: str):
        t = ent.type
        layer = ent.layer
        if layer == "0" and inherited_layer:
            layer = inherited_layer
        if t == "INSERT":
            name = ent.get(2, "").strip()
            if name.startswith("*D") and not keep_dimensions:
                return
            if name not in dwg.blocks or depth >= max_depth:
                return
            (bx, by), bents = dwg.blocks[name]
            ix, iy = ent.get(10, 0.0, float), ent.get(20, 0.0, float)
            sx, sy = ent.get(41, 1.0, float), ent.get(42, 1.0, float)
            rot = math.radians(ent.get(50, 0.0, float))
            cols, rows = int(ent.get(70, 1, float) or 1), int(ent.get(71, 1, float) or 1)
            cdx, rdy = ent.get(44, 0.0, float), ent.get(45, 0.0, float)
            for ci in range(max(1, cols)):
                for ri in range(max(1, rows)):
                    ox, oy = ix + ci * cdx * math.cos(rot) - ri * rdy * math.sin(rot), \
                             iy + ci * cdx * math.sin(rot) + ri * rdy * math.cos(rot)
                    local = Xf(e=-bx, f=-by).then(Xf(a=sx, d=sy)).then(
                        Xf(a=math.cos(rot), b=math.sin(rot), c=-math.sin(rot), d=math.cos(rot))).then(Xf(e=ox, f=oy))
                    for be in bents:
                        emit(be, local.then(xf), depth + 1, layer)
            for att in ent.children:
                if att.type == "ATTRIB" and not (int(att.get(70, 0, float) or 0) & 1):
                    emit_text(att, xf, layer, att.get(1, ""))
            return
        if not visible(layer):
            return
        if t == "LINE":
            p0 = xf(ent.get(10, 0.0, float), ent.get(20, 0.0, float))
            p1 = xf(ent.get(11, 0.0, float), ent.get(21, 0.0, float))
            prims.append(Prim("polyline", [p0, p1], layer=layer))
        elif t == "LWPOLYLINE":
            xs, ys = ent.all(10), ent.all(20)
            verts = list(zip(xs, ys))
            if len(verts) < 2:
                return
            # bulges belong to the vertex they follow
            bul, k = [0.0] * len(verts), -1
            for c, v in ent.codes:
                if c == 10:
                    k += 1
                elif c == 42 and 0 <= k < len(verts):
                    bul[k] = float(v)
            closed = bool(int(ent.get(70, 0, float) or 0) & 1)
            pts = [xf(*p) for p in _poly_from_vertices(verts, bul, closed)]
            prims.append(Prim("polyline", pts, closed=closed, layer=layer))
        elif t == "POLYLINE":
            verts, bul = [], []
            for vx in ent.children:
                if vx.type == "VERTEX":
                    verts.append((vx.get(10, 0.0, float), vx.get(20, 0.0, float)))
                    bul.append(vx.get(42, 0.0, float))
            if len(verts) >= 2:
                closed = bool(int(ent.get(70, 0, float) or 0) & 1)
                prims.append(Prim("polyline", [xf(*p) for p in _poly_from_vertices(verts, bul, closed)],
                                  closed=closed, layer=layer))
        elif t in ("CIRCLE", "ARC"):
            cx, cy = xf(ent.get(10, 0.0, float), ent.get(20, 0.0, float))
            r = ent.get(40, 0.0, float) * xf.scale
            if t == "CIRCLE":
                prims.append(Prim("circle", cx=cx, cy=cy, r=r, layer=layer))
            else:
                a0, a1 = ent.get(50, 0.0, float), ent.get(51, 360.0, float)
                off = xf.angle
                if xf.mirrored:
                    a0, a1 = 180 - a1, 180 - a0
                prims.append(Prim("arc", cx=cx, cy=cy, r=r, a0=a0 + off, a1=a1 + off, layer=layer))
        elif t == "ELLIPSE":
            cx, cy = ent.get(10, 0.0, float), ent.get(20, 0.0, float)
            mx, my = ent.get(11, 1.0, float), ent.get(21, 0.0, float)
            ratio = ent.get(40, 1.0, float)
            t0, t1 = ent.get(41, 0.0, float), ent.get(42, 2 * math.pi, float)
            if t1 < t0:
                t1 += 2 * math.pi
            a = math.hypot(mx, my); b = a * ratio; ang = math.atan2(my, mx)
            n = max(12, int((t1 - t0) / (2 * math.pi) * 72))
            pts = []
            for k in range(n + 1):
                tt = t0 + (t1 - t0) * k / n
                x, y = a * math.cos(tt), b * math.sin(tt)
                pts.append(xf(cx + x * math.cos(ang) - y * math.sin(ang), cy + x * math.sin(ang) + y * math.cos(ang)))
            prims.append(Prim("polyline", pts, closed=abs(t1 - t0 - 2 * math.pi) < 1e-6, layer=layer))
        elif t == "SPLINE":
            fit = list(zip(ent.all(11), ent.all(21)))
            ctrl = list(zip(ent.all(10), ent.all(20)))
            deg = int(ent.get(71, 3, float) or 3)
            knots = ent.all(40)
            pts = fit if len(fit) >= 2 else _bspline(ctrl, deg, knots)
            if len(pts) >= 2:
                prims.append(Prim("polyline", [xf(*p) for p in pts],
                                  closed=bool(int(ent.get(70, 0, float) or 0) & 1), layer=layer))
        elif t in ("SOLID", "TRACE", "3DFACE"):
            p = [(ent.get(10 + k, None, float), ent.get(20 + k, None, float)) for k in range(4)]
            p = [q for q in p if q[0] is not None]
            if t != "3DFACE" and len(p) == 4:
                p = [p[0], p[1], p[3], p[2]]
            if len(p) >= 3:
                prims.append(Prim("polyline", [xf(*q) for q in p], closed=True,
                                  filled="solid" if t != "3DFACE" else "", layer=layer))
        elif t == "HATCH":
            solid = int(ent.get(70, 0, float) or 0) == 1
            for loop in _hatch_loops(ent):
                if len(loop) >= 3:
                    prims.append(Prim("polyline", [xf(*q) for q in loop], closed=True,
                                      filled="solid" if solid else "hatch", layer=layer))
        elif t in ("TEXT", "ATTRIB") or (t == "ATTDEF" and keep_attdefs):
            emit_text(ent, xf, layer, ent.get(1, ""))
        elif t == "MTEXT":
            s = "".join(v for c, v in ent.codes if c == 3) + (ent.get(1, "") or "")
            emit_text(ent, xf, layer, _mtext_plain(s), mtext=True)
        elif t == "LEADER":
            pts = list(zip(ent.all(10), ent.all(20)))
            if len(pts) >= 2:
                prims.append(Prim("polyline", [xf(*p) for p in pts], layer=layer))
        elif t == "DIMENSION" and keep_dimensions:
            name = ent.get(2, "").strip()
            if name in dwg.blocks:
                (bx, by), bents = dwg.blocks[name]
                for be in bents:
                    emit(be, xf, depth + 1, layer)

    def emit_text(ent, xf, layer, s, mtext=False):
        s = _unescape((s or "").strip())
        if not s or not visible(layer):
            return
        h = ent.get(40, 2.5, float) * xf.scale
        x, y = ent.get(10, 0.0, float), ent.get(20, 0.0, float)
        if not mtext and (int(ent.get(72, 0, float) or 0) or int(ent.get(73, 0, float) or 0)):
            ax, ay = ent.get(11, None, float), ent.get(21, None, float)
            if ax is not None:
                x, y = ax, ay
        px, py = xf(x, y)
        rot = ent.get(50, 0.0, float) + xf.angle
        halign = int(ent.get(72, 0, float) or 0) if not mtext else (int(ent.get(71, 1, float) or 1) - 1) % 3
        prims.append(Prim("text", cx=px, cy=py, h=h, rot=rot, text=s, layer=layer,
                          a0=halign, a1=(2 if mtext else int(ent.get(73, 0, float) or 0))))

    for e in dwg.entities:
        if e.type == "ATTDEF" and not keep_attdefs:
            continue
        emit(e, Xf(), 0, "")
    return prims


def _hatch_loops(ent: Entity):
    """Boundary loops of a HATCH as point lists (polyline and line/arc edges)."""
    codes = ent.codes
    loops, i = [], 0
    while i < len(codes):
        c, v = codes[i]
        if c == 92:
            typ = int(float(v)); i += 1
            pts = []
            if typ & 2:   # polyline loop
                has_bulge = closed = 0; n = 0
                while i < len(codes) and codes[i][0] in (72, 73, 93):
                    if codes[i][0] == 72: has_bulge = int(float(codes[i][1]))
                    if codes[i][0] == 73: closed = int(float(codes[i][1]))
                    if codes[i][0] == 93: n = int(float(codes[i][1]))
                    i += 1
                verts, bul = [], []
                while i < len(codes) and len(verts) < n:
                    if codes[i][0] == 10:
                        x = float(codes[i][1]); y = float(codes[i + 1][1]); i += 2
                        b = 0.0
                        if has_bulge and i < len(codes) and codes[i][0] == 42:
                            b = float(codes[i][1]); i += 1
                        verts.append((x, y)); bul.append(b)
                    else:
                        i += 1
                if verts:
                    pts = _poly_from_vertices(verts, bul, True)
            else:         # edge loop
                n = int(float(codes[i][1])) if i < len(codes) and codes[i][0] == 93 else 0
                i += 1
                for _ in range(n):
                    while i < len(codes) and codes[i][0] != 72:
                        i += 1
                    if i >= len(codes):
                        break
                    et = int(float(codes[i][1])); i += 1
                    vals = {}
                    while i < len(codes) and codes[i][0] not in (72, 97, 92, 75):
                        vals.setdefault(codes[i][0], []).append(float(codes[i][1])); i += 1
                    if et == 1 and 10 in vals and 11 in vals:
                        pts += [(vals[10][0], vals[20][0]), (vals[11][0], vals[21][0])]
                    elif et == 2 and 10 in vals:
                        cx, cy, r = vals[10][0], vals[20][0], vals[40][0]
                        a0, a1 = vals[50][0], vals[51][0]
                        ccw = int(vals.get(73, [1])[0])
                        if not ccw:
                            a0, a1 = -a0, -a1
                        if a1 < a0:
                            a1 += 360
                        for k in range(11):
                            a = math.radians(a0 + (a1 - a0) * k / 10)
                            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
            if pts:
                loops.append(pts)
            continue
        i += 1
    return loops


# --------------------------------------------------------------------------
# Terminals
# --------------------------------------------------------------------------

@dataclass
class Terminal:
    x: float
    y: float
    orientation: str      # n s e w  (QElectroTech, y down)
    name: str = ""
    lead: float = 0.0     # length of the lead segment, drawing units


def bbox(prims, texts=False):
    xs, ys = [], []
    for p in prims:
        if p.kind == "polyline":
            xs += [q[0] for q in p.pts]; ys += [q[1] for q in p.pts]
        elif p.kind in ("circle", "arc"):
            xs += [p.cx - p.r, p.cx + p.r]; ys += [p.cy - p.r, p.cy + p.r]
        elif p.kind == "text" and texts:
            xs += [p.cx, p.cx + 0.6 * p.h * len(p.text)]; ys += [p.cy, p.cy + p.h]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# what a terminal designation looks like: 1, 14, A1, L1, PE, N, 2/T1, X1:3, U, +, -
TERMINAL_LABEL = r"[A-Za-z]{0,3}\d{0,3}[A-Za-z]?(/[A-Za-z]{0,2}\d{0,2})?|[+\-~]|[A-Z]{1,2}\d?:\d{1,3}"


def _seg_cross(p1, p2, p3, p4):
    """Do segments p1-p2 and p3-p4 intersect (touching counts)?"""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = orient(p1, p2, p3), orient(p1, p2, p4), orient(p3, p4, p1), orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    def on(a, b, c):
        return min(a[0], b[0]) - 1e-12 <= c[0] <= max(a[0], b[0]) + 1e-12 and \
               min(a[1], b[1]) - 1e-12 <= c[1] <= max(a[1], b[1]) + 1e-12
    return (o1 == 0 and on(p1, p2, p3)) or (o2 == 0 and on(p1, p2, p4)) or \
           (o3 == 0 and on(p3, p4, p1)) or (o4 == 0 and on(p3, p4, p2))


def find_terminals(prims, edge_tol=0.04, join_tol=0.01, points=None,
                   min_lead=0.06, side_ratio=0.7, exposed=True) -> list:
    """Free line ends on the outline of the symbol, with the side they face.

    A line end is free when no other line, arc or circle touches it. It is a
    terminal when it lies on the outer box of the drawing and its segment runs
    perpendicular to that side (a lead), which keeps the corners of frames out.
    """
    geo = [p for p in prims if p.kind != "text"]
    x0, y0, x1, y1 = bbox(geo)
    size = max(x1 - x0, y1 - y0, 1e-9)
    et, jt = edge_tol * size, join_tol * size
    if points:
        out = []
        for (px, py) in points:
            side = min((abs(py - y1), "n"), (abs(py - y0), "s"), (abs(px - x0), "w"), (abs(px - x1), "e"))[1]
            out.append(Terminal(px, py, side))
        return out
    segs = []
    for p in geo:
        if p.kind == "polyline":
            pts = p.pts + ([p.pts[0]] if p.closed else [])
            for a, b in zip(pts, pts[1:]):
                segs.append((a, b, not p.closed))
    ends = []
    for p in geo:
        if p.kind == "polyline" and not p.closed and len(p.pts) >= 2:
            ends.append((p.pts[0], p.pts[1]))
            ends.append((p.pts[-1], p.pts[-2]))

    def touches(pt, own):
        for a, b, _ in segs:
            if (a, b) == own or (b, a) == own:
                continue
            # endpoint of another segment, or on its interior
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            if L2 < 1e-18:
                continue
            t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / L2))
            if math.hypot(a[0] + t * dx - pt[0], a[1] + t * dy - pt[1]) < jt:
                return True
        for p in geo:
            if p.kind in ("circle", "arc") and abs(math.hypot(pt[0] - p.cx, pt[1] - p.cy) - p.r) < jt:
                return True
        return False

    def clear_ray(pt, d):
        """True when a ray from pt in direction d meets no geometry (texts aside)."""
        ux, uy = {"n": (0, 1), "s": (0, -1), "e": (1, 0), "w": (-1, 0)}[d]
        far = (pt[0] + ux * 2 * size, pt[1] + uy * 2 * size)
        start = (pt[0] + ux * jt * 2, pt[1] + uy * jt * 2)
        for a, b, _ in segs:
            if _seg_cross(start, far, a, b):
                return False
        for p in geo:
            if p.kind in ("circle", "arc"):
                # distance from centre to the ray
                vx, vy = p.cx - start[0], p.cy - start[1]
                t = vx * ux + vy * uy
                if t > 0 and math.hypot(vx - t * ux, vy - t * uy) <= p.r:
                    return False
        return True

    labels = [p for p in prims if p.kind == "text" and re.fullmatch(TERMINAL_LABEL, p.text.strip())]

    def nearest_label(pt, taken):
        best, bd = None, None
        for k, p in enumerate(labels):
            if k in taken:
                continue
            d = math.hypot(p.cx + 0.3 * p.h * len(p.text) - pt[0], p.cy + 0.5 * p.h - pt[1])
            if d < 2.5 * max(p.h, 1e-9) and (bd is None or d < bd):
                best, bd = k, d
        return best

    out, inner = [], []
    for pt, nxt in ends:
        if touches(pt, (pt, nxt)) or touches(pt, (nxt, pt)):
            continue
        dx, dy = pt[0] - nxt[0], pt[1] - nxt[1]
        L = math.hypot(dx, dy) or 1.0
        if L < min_lead * size:          # a tick or a hatch stroke, not a lead
            continue
        vertical = abs(dy) / L > 0.9
        horizontal = abs(dx) / L > 0.9
        side = None
        if vertical and abs(pt[1] - y1) < et and dy > 0:
            side = "n"
        elif vertical and abs(pt[1] - y0) < et and dy < 0:
            side = "s"
        elif horizontal and abs(pt[0] - x0) < et and dx < 0:
            side = "w"
        elif horizontal and abs(pt[0] - x1) < et and dx > 0:
            side = "e"
        elif exposed and (vertical or horizontal):
            d = ("n" if dy > 0 else "s") if vertical else ("e" if dx > 0 else "w")
            inner.append(Terminal(pt[0], pt[1], d, lead=L))
        if side:
            out.append(Terminal(pt[0], pt[1], side, lead=L))
    # A symbol fed from top and bottom keeps a side terminal only when its lead
    # is as long as the main ones (a coil or aux contact), not a short mark.
    vert = [t for t in out if t.orientation in "ns"]
    main = max((t.lead for t in out), default=0.0)
    if vert:
        out = [t for t in out if t.orientation in "ns" or t.lead >= side_ratio * max(t.lead for t in vert)]
    # A lead inside the drawing (a coil next to contacts, A1/A2) is a terminal
    # when it carries a label of its own -- not one the outline terminals use --
    # is not a short tick, and nothing lies beyond its end.
    taken = set()
    for t in out:
        k = nearest_label((t.x, t.y), taken)
        if k is not None:
            taken.add(k)
    for t in inner:
        if main and t.lead < 0.35 * main:
            continue
        k = nearest_label((t.x, t.y), taken)
        if k is not None and clear_ray((t.x, t.y), t.orientation):
            taken.add(k)
            out.append(t)
    # one terminal per point
    uniq = []
    for t in out:
        if not any(math.hypot(t.x - u.x, t.y - u.y) < jt for u in uniq):
            uniq.append(t)
    return uniq


def name_terminals(terms, prims, radius_factor=3.0):
    """Give each terminal the short text (1, 2, L, N, A1 ...) written next to it."""
    texts = [p for p in prims if p.kind == "text" and p.text.strip() and re.fullmatch(TERMINAL_LABEL, p.text.strip())]
    used = set()
    for t in terms:
        best, bd = None, None
        for k, p in enumerate(texts):
            if k in used:
                continue
            tx, ty = p.cx + 0.3 * p.h * len(p.text), p.cy + 0.5 * p.h
            d = math.hypot(tx - t.x, ty - t.y)
            if d < radius_factor * max(p.h, 1e-6) and (bd is None or d < bd):
                best, bd = k, d
        if best is not None:
            used.add(best)
            t.name = texts[best].text.strip()
    return used


# --------------------------------------------------------------------------
# Scaling to the QElectroTech grid
# --------------------------------------------------------------------------

def choose_scale(terms, prims, target=60.0, pitch_px=None):
    """px per drawing unit so that terminals land on the 10 px grid.

    Multi-pole: the smallest spacing of terminals on one side becomes
    ``pitch_px`` (20 px if the result is not too large, otherwise 10).
    Single pole: the distance between opposite terminals is rounded to a
    multiple of 10 close to ``target``.
    """
    x0, y0, x1, y1 = bbox([p for p in prims if p.kind != "text"])
    height = max(y1 - y0, x1 - x0, 1e-9)
    pitches = []
    for side in "nswe":
        vals = sorted((t.x if side in "ns" else t.y) for t in terms if t.orientation == side)
        pitches += [b - a for a, b in zip(vals, vals[1:]) if b - a > 1e-6]
    if pitches:
        p = min(pitches)
        for px in ([pitch_px] if pitch_px else [20.0, 10.0]):
            s = px / p
            if height * s <= max(3 * target, 160) or px == 10.0:
                return s
    ns = [t for t in terms if t.orientation in "ns"]
    tops = [t.y for t in ns if t.orientation == "n"]
    bots = [t.y for t in ns if t.orientation == "s"]
    if tops and bots:
        span = max(tops) - min(bots)
        if span > 1e-9:
            return max(10.0, round(target / 10.0) * 10.0) / span
    return target / height


def snap(v, g=10.0):
    return round(v / g) * g


def simplify(pts, tol):
    """Douglas-Peucker: drop points closer than tol to the line through their neighbours."""
    if tol <= 0 or len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bd = None, tol
        for k in range(a + 1, b):
            px, py = pts[k]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L if L > 1e-12 else math.hypot(px - ax, py - ay)
            if d > bd:
                best, bd = k, d
        if best is not None:
            keep[best] = True
            stack += [(a, best), (best, b)]
    return [p for p, k in zip(pts, keep) if k]


# --------------------------------------------------------------------------
# Writing .elmt
# --------------------------------------------------------------------------

def _fmt(v):
    v = round(float(v), 2)
    return str(int(v)) if v.is_integer() else str(v)


FONT = "Liberation Sans,{},-1,5,50,0,0,0,0,0,Regular"


def _style(weight="normal", fill="none"):
    return f"line-style:normal;line-weight:{weight};filling:{fill};color:black"


@dataclass
class Options:
    mode: str = "symbol"              # symbol | thumbnail
    scale: float | None = None        # px per drawing unit (overrides automatic)
    px_per_mm: float = 2.0            # thumbnail mode
    units_mm: float = 1.0             # drawing unit in mm (from $INSUNITS when known)
    target: float = 60.0              # symbol mode: rough height in px
    pitch_px: float | None = None
    terminal_points: list | None = None
    texts: bool = True                # keep drawing texts
    min_text_px: float = 4.0          # drop texts smaller than this (px)
    thin_layers: list | None = None   # regex: layers drawn thin
    fill_hatch: str = "lightgray"
    label: bool = True                # add label / comment fields
    link_type: str | None = None
    names: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)        # elementInformations
    kind: dict = field(default_factory=dict)        # kindInformations
    author: str = ""
    grid: float = 10.0
    fallback_terminal: bool = True    # symbol without a lead (lamp, socket): one terminal on top
    simplify_px: float = 0.3          # Douglas-Peucker tolerance for polylines, px (0 = keep all points)


INSUNITS_MM = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 13: 0.001, 14: 0.1}


def convert(dwg: Drawing, opts: Options, include=None, exclude=None) -> tuple:
    """Drawing -> (definition Element, report dict)."""
    prims = flatten(dwg, include=include, exclude=exclude)
    geo = [p for p in prims if p.kind != "text"]
    if not geo:
        raise ValueError("no visible geometry (check the layer filters)")
    report = {"primitives": len(prims)}
    terms: list = []
    synthetic = False
    if opts.mode == "symbol":
        terms = find_terminals(prims, points=opts.terminal_points)
        name_terminals(terms, prims)
        gx0, gy0, gx1, gy1 = bbox(geo)
        w, h = gx1 - gx0, gy1 - gy0
        if not terms and opts.fallback_terminal and h > 0 and 0.4 <= w / h <= 2.5:
            # a lamp, socket or meter drawn without a lead: one supply terminal on top
            terms = [Terminal((gx0 + gx1) / 2, gy1, "n")]
            synthetic = True
        s = opts.scale or choose_scale(terms, prims, opts.target, opts.pitch_px)
    else:
        unit = opts.units_mm
        ins = dwg.header.get("$INSUNITS", {}).get(70)
        if ins and int(ins) in INSUNITS_MM and opts.units_mm == 1.0:
            unit = INSUNITS_MM[int(ins)]
        s = opts.scale or opts.px_per_mm * unit
    report["scale_px_per_unit"] = s

    x0, y0, x1, y1 = bbox(geo)
    # origin: first terminal (top-left) in symbol mode, top-left corner otherwise
    if terms:
        t0 = sorted(terms, key=lambda t: (-t.y, t.x))[0]
        ox, oy = t0.x, t0.y
    else:
        ox, oy = x0, y1

    def tr(x, y):                     # drawing (y up) -> element (y down), px
        return ((x - ox) * s, (oy - y) * s)

    # snapped terminals, and where each lead end must move to
    moves = {}
    qterms = []
    stubs = []
    for t in terms:
        qx, qy = tr(t.x, t.y)
        sx, sy = snap(qx, opts.grid), snap(qy, opts.grid)
        if synthetic:                 # short lead up to a grid point above the drawing
            sy = math.floor((qy - 5) / opts.grid) * opts.grid
            stubs.append(("line", {"x1": sx, "y1": qy, "x2": sx, "y2": sy, "end1": "none", "end2": "none",
                                   "length1": "1.5", "length2": "1.5", "style": _style(), "antialias": "false"}))
        moves[(round(qx, 6), round(qy, 6))] = (sx, sy)
        o = {"n": "n", "s": "s", "e": "e", "w": "w"}[t.orientation]
        qterms.append((sx, sy, o, t.name))
    # QElectroTech wants every terminal named, and each name once: unnamed ones
    # are numbered in index order (top to bottom, then left to right), a repeated
    # name (N in and N out) gets a prime.
    order = sorted(range(len(qterms)), key=lambda k: (qterms[k][1], qterms[k][0]))
    used = {t[3] for t in qterms if t[3]}
    num = 1
    for k in order:
        if not qterms[k][3]:
            while str(num) in used:
                num += 1
            qterms[k] = qterms[k][:3] + (str(num),)
            used.add(str(num))
    seen = set()
    for k in order:
        nm = qterms[k][3]
        while nm in seen:
            nm += "'"
        seen.add(nm)
        qterms[k] = qterms[k][:3] + (nm,)
    report["terminals"] = len(qterms)
    if synthetic:
        report["note"] = "no lead found: added one supply terminal on top"

    def mv(p):
        q = (round(p[0], 6), round(p[1], 6))
        return moves.get(q, p)

    thin = [re.compile(r, re.I) for r in (opts.thin_layers or [])]
    parts = list(stubs)
    for p in prims:
        weight = "thin" if any(r.fullmatch(p.layer) for r in thin) else "normal"
        if p.kind == "polyline":
            pts = [mv(tr(*q)) for q in p.pts]
            pts = simplify(pts, opts.simplify_px)
            if len(pts) < 2 or (len(pts) == 2 and math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]) < 1e-3):
                continue
            if len(pts) == 2 and not p.filled:
                (ax, ay), (bx, by) = pts
                # end1/end2/length/antialias left out: QElectroTech's defaults
                parts.append(("line", {"x1": ax, "y1": ay, "x2": bx, "y2": by, "style": _style(weight)}))
            else:
                fill = "none"
                if p.filled == "solid":
                    fill = "black"
                elif p.filled == "hatch":
                    fill = opts.fill_hatch
                a = {}
                for k, (qx, qy) in enumerate(pts, 1):
                    a[f"x{k}"], a[f"y{k}"] = qx, qy
                if not p.closed:
                    a["closed"] = "false"
                a["style"] = _style(weight, fill)
                parts.append(("polygon", a))
        elif p.kind == "circle":
            cx, cy = tr(p.cx, p.cy); r = p.r * s
            parts.append(("circle", {"x": cx - r, "y": cy - r, "diameter": 2 * r,
                                     "style": _style(weight)}))
        elif p.kind == "arc":
            cx, cy = tr(p.cx, p.cy); r = p.r * s
            a0 = p.a0 % 360; span = (p.a1 - p.a0) % 360 or 360
            parts.append(("arc", {"x": cx - r, "y": cy - r, "width": 2 * r, "height": 2 * r,
                                  "start": a0, "angle": span, "style": _style(weight)}))
        elif p.kind == "text" and opts.texts:
            hpx = p.h * s
            if hpx < opts.min_text_px:
                continue
            size = max(4, int(round(hpx / 1.33)))
            tx, ty = tr(p.cx, p.cy)
            # QElectroTech places a text by its baseline-left point
            w = 0.6 * hpx * len(p.text)
            if int(p.a0) == 1:
                tx -= w / 2
            elif int(p.a0) == 2:
                tx -= w
            parts.append(("text", {"x": tx, "y": ty, "rotation": -p.rot % 360 if p.rot else 0,
                                   "text": p.text, "font": FONT.format(size), "color": "#000000"}))

    # box of the drawing in element coordinates
    xs, ys = [], []
    for kind, a in parts:
        for k, v in a.items():
            if re.fullmatch(r"x\d*|x[12]", k):
                xs.append(float(v))
            elif re.fullmatch(r"y\d*|y[12]", k):
                ys.append(float(v))
        if kind == "circle":
            xs.append(float(a["x"]) + float(a["diameter"])); ys.append(float(a["y"]) + float(a["diameter"]))
        if kind == "arc":
            xs.append(float(a["x"]) + float(a["width"])); ys.append(float(a["y"]) + float(a["height"]))
    for (tx, ty, *_r) in qterms:
        xs.append(tx); ys.append(ty)
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)

    if opts.label and opts.mode == "symbol":
        vertical = all(o in "ns" for _x, _y, o, _n in qterms) if qterms else True
        lx, ly = (bx1 + 4, by0) if vertical else (bx0, by0 - 30)
        parts.append(("dynamic_text", {"x": lx, "y": ly, "info": "label", "size": 9}))
        parts.append(("dynamic_text", {"x": lx, "y": ly + 13, "info": "comment", "size": 8}))
    elif opts.label and opts.mode == "thumbnail":
        parts.append(("dynamic_text", {"x": bx0, "y": by0 - 14, "info": "label", "size": 8}))

    gx0 = math.floor(min(bx0, 0) / 10) * 10
    gy0 = math.floor(min(by0, 0) / 10) * 10
    gx1 = math.ceil(bx1 / 10) * 10
    gy1 = math.ceil(by1 / 10) * 10
    gx1, gy1 = max(gx1, gx0 + 10), max(gy1, gy0 + 10)   # QElectroTech refuses a box of zero size
    link = opts.link_type or ("simple" if opts.mode == "symbol" else "thumbnail")
    if link in ("simple", "master", "slave", "terminal") and not qterms:
        link = "thumbnail"
        report["note"] = "no terminal found: written as a thumbnail (give --terminal x,y to force one)"
    root = ET.Element("definition", {"version": "0.100.0", "type": "element", "link_type": link,
                                     "width": str(int(gx1 - gx0)), "height": str(int(gy1 - gy0)),
                                     "hotspot_x": str(int(-gx0)), "hotspot_y": str(int(-gy0))})
    ET.SubElement(root, "uuid", {"uuid": "{" + str(uuidlib.uuid4()) + "}"})
    names = ET.SubElement(root, "names")
    for lang in sorted(opts.names or {"en": "Converted element"}):
        ET.SubElement(names, "name", {"lang": lang}).text = (opts.names or {"en": "Converted element"})[lang]
    if opts.kind:
        k = ET.SubElement(root, "kindInformations")
        for key in sorted(opts.kind):
            ET.SubElement(k, "kindInformation", {"name": key}).text = str(opts.kind[key])
    if opts.info:
        ei = ET.SubElement(root, "elementInformations")
        for key, val in opts.info.items():
            if val not in (None, ""):
                ET.SubElement(ei, "elementInformation", {"name": key, "show": "1"}).text = str(val)
    ET.SubElement(root, "informations").text = (opts.author + "\n" if opts.author else "") + \
        f"Converted with dwg2qet {__version__}"
    desc = ET.SubElement(root, "description")
    for kind, a in parts:
        if kind == "dynamic_text":
            d = ET.SubElement(desc, "dynamic_text", {
                "x": _fmt(a["x"]), "y": _fmt(a["y"]), "z": "10", "text_width": "-1",
                "Halignment": "AlignLeft", "Valignment": "AlignTop", "frame": "false", "rotation": "0",
                "keep_visual_rotation": "false", "text_from": "ElementInfo",
                "uuid": "{" + str(uuidlib.uuid4()) + "}", "font": FONT.format(a["size"])})
            ET.SubElement(d, "text").text = ""
            ET.SubElement(d, "info_name").text = a["info"]
        else:
            ET.SubElement(desc, kind, {k: (_fmt(v) if isinstance(v, (int, float)) else str(v)) for k, v in a.items()})
    for (tx, ty, o, nm) in qterms:
        a = {"uuid": "{" + str(uuidlib.uuid4()) + "}", "x": _fmt(tx), "y": _fmt(ty), "orientation": o, "type": "Generic"}
        a["name"] = nm or ""
        ET.SubElement(desc, "terminal", a)
    report.update({"width": int(gx1 - gx0), "height": int(gy1 - gy0), "link_type": link,
                   "terminal_list": [{"x": t[0], "y": t[1], "orientation": t[2], "name": t[3]} for t in qterms]})
    return root, report


def write_elmt(root: ET.Element, path: Path):
    ET.indent(root, space="    ")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(root, encoding="utf-8"))


# --------------------------------------------------------------------------
# SVG preview of an .elmt (no QElectroTech needed)
# --------------------------------------------------------------------------

def preview_svg(elmt: Path, out: Path, pad=10):
    r = ET.parse(elmt).getroot()
    w, h = float(r.get("width")), float(r.get("height"))
    hx, hy = float(r.get("hotspot_x")), float(r.get("hotspot_y"))
    body = []
    for n in r.find("description"):
        a = n.attrib
        st = a.get("style", "")
        fill = re.search(r"filling:([a-z]+)", st)
        fill = fill.group(1) if fill and fill.group(1) not in ("none", "") else "none"
        sw = "0.5" if "line-weight:thin" in st else "1"
        if n.tag == "line":
            body.append(f'<line x1="{a["x1"]}" y1="{a["y1"]}" x2="{a["x2"]}" y2="{a["y2"]}" stroke="black" stroke-width="{sw}"/>')
        elif n.tag == "polygon":
            pts = []; i = 1
            while f"x{i}" in a:
                pts.append(f'{a[f"x{i}"]},{a[f"y{i}"]}'); i += 1
            tag = "polyline" if a.get("closed") == "false" else "polygon"
            body.append(f'<{tag} points="{" ".join(pts)}" fill="{fill if tag == "polygon" else "none"}" stroke="black" stroke-width="{sw}"/>')
        elif n.tag in ("rect",):
            body.append(f'<rect x="{a["x"]}" y="{a["y"]}" width="{a["width"]}" height="{a["height"]}" fill="{fill}" stroke="black" stroke-width="{sw}"/>')
        elif n.tag == "circle":
            d = float(a["diameter"])
            body.append(f'<circle cx="{float(a["x"]) + d / 2}" cy="{float(a["y"]) + d / 2}" r="{d / 2}" fill="{fill}" stroke="black" stroke-width="{sw}"/>')
        elif n.tag == "ellipse":
            body.append(f'<ellipse cx="{float(a["x"]) + float(a["width"]) / 2}" cy="{float(a["y"]) + float(a["height"]) / 2}" rx="{float(a["width"]) / 2}" ry="{float(a["height"]) / 2}" fill="{fill}" stroke="black" stroke-width="{sw}"/>')
        elif n.tag == "arc":
            x, y, ww, hh = (float(a[k]) for k in ("x", "y", "width", "height"))
            st0, span = float(a["start"]), float(a["angle"])
            cx, cy, rx, ry = x + ww / 2, y + hh / 2, ww / 2, hh / 2
            p0 = (cx + rx * math.cos(math.radians(st0)), cy - ry * math.sin(math.radians(st0)))
            p1 = (cx + rx * math.cos(math.radians(st0 + span)), cy - ry * math.sin(math.radians(st0 + span)))
            large = 1 if span > 180 else 0
            body.append(f'<path d="M{p0[0]:.2f},{p0[1]:.2f} A{rx:.2f},{ry:.2f} 0 {large} 0 {p1[0]:.2f},{p1[1]:.2f}" fill="none" stroke="black" stroke-width="{sw}"/>')
        elif n.tag == "text":
            size = float(a.get("font", "x,9").split(",")[1]) * 1.33
            body.append(f'<text x="{a["x"]}" y="{a["y"]}" font-family="Liberation Sans, Arial" font-size="{size:.1f}">{html.escape(a.get("text", ""))}</text>')
        elif n.tag == "dynamic_text":
            body.append(f'<text x="{a["x"]}" y="{float(a["y"]) + 9}" font-family="Arial" font-size="8" fill="#2a6fdb">{html.escape("%{" + (n.findtext("info_name") or "") + "}")}</text>')
        elif n.tag == "terminal":
            x, y = float(a["x"]), float(a["y"])
            dx, dy = {"n": (0, -5), "s": (0, 5), "e": (5, 0), "w": (-5, 0)}[a["orientation"]]
            body.append(f'<line x1="{x}" y1="{y}" x2="{x + dx}" y2="{y + dy}" stroke="red" stroke-width="1.5"/><circle cx="{x}" cy="{y}" r="1.8" fill="#1060ff"/>')
            if a.get("name"):
                body.append(f'<text x="{x + 3}" y="{y - 3}" font-size="6" fill="red">{html.escape(a["name"])}</text>')
    vb = f"{-hx - pad} {-hy - pad} {w + 2 * pad} {h + 2 * pad}"
    out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{(w + 2 * pad) * 2:.0f}" height="{(h + 2 * pad) * 2:.0f}">'
                   f'<rect x="{-hx - pad}" y="{-hy - pad}" width="{w + 2 * pad}" height="{h + 2 * pad}" fill="white"/>'
                   + "".join(body) + "</svg>", encoding="utf-8")


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------

def write_category(folder: Path, names: dict):
    """qet_directory: the translated name QElectroTech shows for a folder."""
    folder.mkdir(parents=True, exist_ok=True)
    root = ET.Element("qet-directory")
    n = ET.SubElement(root, "names")
    for lang in sorted(names):
        ET.SubElement(n, "name", {"lang": lang}).text = names[lang]
    ET.indent(root, space="    ")
    (folder / "qet_directory").write_bytes(ET.tostring(root, encoding="utf-8"))


def check_with_qelectrotech(path: Path, binary: str | None = None) -> str:
    import glob
    exe = binary or os.environ.get("QET_BINARY") or shutil.which("qelectrotech") or next(
        iter(sorted(glob.glob("/Applications/*[Qq][Ee]lectro[Tt]ech*.app/Contents/MacOS/qelectrotech"), reverse=True)), None)
    if not exe:
        return "qelectrotech not found: skipped --check"
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    p = subprocess.run([exe, "--check-elements", str(path)], capture_output=True, text=True, env=env, timeout=600)
    lines = [l for l in (p.stdout + p.stderr).splitlines() if re.search(r"^(OK|WARN|FAIL)|file\(s\)", l)]
    return "\n".join(lines[-50:])


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"expected key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def _options_from(ns, names=None, info=None) -> Options:
    pts = None
    if ns.terminal:
        pts = [tuple(float(c) for c in t.split(",")[:2]) for t in ns.terminal]
    return Options(mode=ns.mode, scale=ns.scale, px_per_mm=ns.px_per_mm, target=ns.target,
                   pitch_px=ns.pitch, terminal_points=pts, texts=not ns.no_texts,
                   thin_layers=ns.thin_layer, label=not ns.no_label, link_type=ns.link_type,
                   names=names or _parse_kv(ns.name) or {"en": Path(ns.input).stem if getattr(ns, "input", None) else "element"},
                   info=info if info is not None else _parse_kv(ns.info),
                   kind=_parse_kv(ns.kind), author=ns.author or "")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dwg2qet", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help=".dxf or .dwg file (or use --batch)")
    ap.add_argument("-o", "--output", help=".elmt to write (default: next to the input)")
    ap.add_argument("--mode", choices=["symbol", "thumbnail"], default="symbol",
                    help="symbol: schematic symbol with terminals on the 10 px grid; "
                         "thumbnail: front view for cabinet layouts at --px-per-mm")
    ap.add_argument("--layer", action="append", help="only these layers (regex, repeatable)")
    ap.add_argument("--exclude-layer", action="append", help="skip these layers (regex, repeatable)")
    ap.add_argument("--thin-layer", action="append", help="draw these layers thin (regex)")
    ap.add_argument("--scale", type=float, help="px per drawing unit (overrides automatic scaling)")
    ap.add_argument("--px-per-mm", type=float, default=2.0, help="thumbnail scale (default 2)")
    ap.add_argument("--target", type=float, default=60.0, help="symbol: rough height in px (default 60)")
    ap.add_argument("--pitch", type=float, help="symbol: px between poles (default 20, or 10 if too big)")
    ap.add_argument("--terminal", action="append", help="force a terminal at x,y (drawing units)")
    ap.add_argument("--name", action="append", help="lang=name, repeatable (e.g. en=Breaker sl=Odklopnik)")
    ap.add_argument("--info", action="append", help="element information key=value (manufacturer=..., "
                    "manufacturer_reference=..., width=..., height=..., depth=...)")
    ap.add_argument("--kind", action="append", help="kindInformation key=value")
    ap.add_argument("--link-type", choices=["simple", "master", "slave", "terminal", "thumbnail"])
    ap.add_argument("--author", help="text for the element's informations field")
    ap.add_argument("--no-texts", action="store_true", help="drop the drawing's texts")
    ap.add_argument("--no-label", action="store_true", help="do not add label/comment fields")
    ap.add_argument("--preview", action="store_true", help="also write an .svg preview")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--check", action="store_true", help="run qelectrotech --check-elements on the result")
    ap.add_argument("--batch", help="CSV manifest: one element per row (see README)")
    ap.add_argument("--out-dir", help="batch: collection folder to write into")
    ap.add_argument("--version", action="version", version=__version__)
    ns = ap.parse_args(argv)

    if ns.batch:
        return run_batch(ns)
    if not ns.input:
        ap.error("give an input file or --batch")
    src = Path(ns.input)
    out = Path(ns.output) if ns.output else src.with_suffix(".elmt")
    root, rep = convert(load(src), _options_from(ns), include=ns.layer, exclude=ns.exclude_layer)
    write_elmt(root, out)
    rep["output"] = str(out)
    if ns.preview:
        preview_svg(out, out.with_suffix(".svg")); rep["preview"] = str(out.with_suffix(".svg"))
    if ns.check:
        rep["check"] = check_with_qelectrotech(out)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if ns.json else
          f"{out}  {rep['width']}x{rep['height']} px, {rep['terminals']} terminals, {rep['link_type']}"
          + (f"\n{rep['note']}" if rep.get("note") else "") + (f"\n{rep['check']}" if rep.get("check") else ""))
    return 0


def run_batch(ns):
    """CSV columns: source, output (relative to --out-dir), mode, layer, exclude_layer,
    name_<lang>..., info_<key>..., kind_<key>..., category_<lang> (names for the output folder)."""
    out_dir = Path(ns.out_dir or ".")
    cache: dict = {}
    done = failed = 0
    log = []
    with open(ns.batch, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                src = Path(row["source"])
                key = (str(src), row.get("mode") or ns.mode, row.get("layer", ""), row.get("exclude_layer", ""))
                if key not in cache:
                    cache.clear() if len(cache) > 256 else None
                    cache[key] = load(src)
                names = {k[5:]: v for k, v in row.items() if k.startswith("name_") and v}
                info = {k[5:]: v for k, v in row.items() if k.startswith("info_") and v}
                kind = {k[5:]: v for k, v in row.items() if k.startswith("kind_") and v}
                o = _options_from(ns, names=names or {"en": src.stem}, info=info)
                o.mode = row.get("mode") or ns.mode
                o.kind = kind or o.kind
                if row.get("link_type"):
                    o.link_type = row["link_type"]
                inc = [x for x in (row.get("layer") or "").split("|") if x] or ns.layer
                exc = [x for x in (row.get("exclude_layer") or "").split("|") if x] or ns.exclude_layer
                root, rep = convert(cache[key], o, include=inc, exclude=exc)
                out = out_dir / row["output"]
                write_elmt(root, out)
                cat = {k[9:]: v for k, v in row.items() if k.startswith("category_") and v}
                if cat and not (out.parent / "qet_directory").exists():
                    write_category(out.parent, cat)
                done += 1
                log.append({"output": str(out), **{k: rep[k] for k in ("terminals", "width", "height", "link_type")}})
            except Exception as exc:   # one bad drawing must not stop a library
                failed += 1
                log.append({"source": row.get("source"), "error": str(exc)})
    print(json.dumps({"written": done, "failed": failed, "log": log[-20:]}, ensure_ascii=False, indent=1))
    if ns.check:
        print(check_with_qelectrotech(out_dir))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
