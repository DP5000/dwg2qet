#!/usr/bin/env python3
"""terminals2qet - draw a QElectroTech device symbol from its terminal table.

The manual of a drive, PLC or inverter lists every terminal: block X13,
terminal 1, DI00, "/Controller inhibit". Write that table as JSON or CSV and
terminals2qet draws the schematic symbol:

* a box with every terminal on the 10 px grid, named the way the manual does
  (X13:1), its designation inside (DI00) and its number outside;
* each block framed like a terminal strip; power terminals at the top and the
  bottom, control terminals at the sides, or wherever the table puts them;
* label and comment fields and the element information for the bill of materials.

With the manufacturer's front view (DXF/DWG) and STEP model the same run also
writes the cabinet view through dwg2qet, with width / height / depth taken from
the 3D model, so one spec gives the whole device.

Python 3.9+ standard library only. MIT licence.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dwg2qet as D  # noqa: E402

__version__ = "0.2.0"

SIDES = ("top", "bottom", "left", "right")
ORIENTATION = {"top": "n", "bottom": "s", "left": "w", "right": "e"}
# where a terminal goes when the table does not say
TOP_NAMES = {"L", "L1", "L2", "L3", "N", "L+", "L-", "+24V", "24V", "0V"}
BOTTOM_NAMES = {"U", "V", "W", "U1", "V1", "W1", "+R", "-R", "R+", "R-", "+UZ", "-UZ", "BR+", "BR-"}
FRONT_VIEW = {"en": "front view", "de": "Vorderansicht", "fr": "vue de face", "it": "vista frontale",
              "es": "vista frontal", "sl": "pogled od spredaj", "hr": "prednji pogled", "pl": "widok z przodu"}


@dataclass
class Term:
    block: str
    number: str
    name: str
    function: str = ""
    side: str = ""
    qet_name: str = ""


@dataclass
class Block:
    name: str
    side: str
    title: str = ""
    terms: list = field(default_factory=list)


@dataclass
class Layout:
    pitch: int = 20          # px between two terminals of a block (multiple of 10)
    lead: int = 10           # px from the box to the terminal (multiple of 10)
    name_pt: int = 6         # designation inside the box
    small_pt: int = 5        # terminal number and function
    title_pt: int = 8
    functions: bool = True   # write the function next to side terminals
    function_chars: int = 30  # longer functions are cut with "…" in the drawing (the table keeps them whole)


def _w(text: str, pt: float, bold=False) -> float:
    """Rough width of a text in px (Liberation Sans)."""
    return (0.68 if bold else 0.62) * pt * 1.33 * len(text)


def _up10(v: float) -> int:
    return int(math.ceil(v / 10 - 1e-9) * 10)


def _font(pt, bold=False):
    return f"Liberation Sans,{pt},-1,5,{75 if bold else 50},0,0,0,0,0,{'Bold' if bold else 'Regular'}"


# --------------------------------------------------------------------------
# Reading the table
# --------------------------------------------------------------------------

def _row_term(r: dict, block="") -> Term:
    return Term(block=str(r.get("block", block) or block).strip(),
                number=str(r.get("terminal", r.get("number", "")) or "").strip(),
                name=str(r.get("name", "") or "").strip(),
                function=str(r.get("function", "") or "").strip(),
                side=str(r.get("side", "") or "").strip().lower())


def read_spec(path: Path) -> dict:
    """JSON spec, or a CSV table (one row per terminal: block, terminal, name, side, function)."""
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return {"terminals": list(csv.DictReader(fh))}
    return json.loads(path.read_text(encoding="utf-8"))


def blocks_from_spec(spec: dict) -> list:
    """Blocks in table order. Accepts "blocks" (each with its terminals) or a flat "terminals" list."""
    blocks: list[Block] = []
    by_name: dict[str, Block] = {}

    def add(t: Term, side="", title=""):
        b = by_name.get(t.block)
        if b is None:
            b = Block(t.block, side, title)
            by_name[t.block] = b
            blocks.append(b)
        b.terms.append(t)
        if t.side and not b.side:
            b.side = t.side

    for b in spec.get("blocks", []):
        for r in b.get("terminals", []):
            if isinstance(r, (list, tuple)):
                r = dict(zip(("terminal", "name", "function"), r))
            add(_row_term(r, b.get("block", "")), str(b.get("side", "")).lower(), b.get("title", ""))
    for r in spec.get("terminals", []):
        add(_row_term(r))
    for b in blocks:
        if b.side not in SIDES:
            names = {t.name.upper() for t in b.terms}
            b.side = "top" if names & TOP_NAMES else "bottom" if names & BOTTOM_NAMES else "right"
        for t in b.terms:
            t.side = b.side
    # QElectroTech terminal name: X13:1 like the manual, X1:L1 when the terminal has no number
    seen: set = set()
    for b in blocks:
        for t in b.terms:
            if not t.name and not t.number:
                raise ValueError(f"block {b.name!r}: a terminal without name and number")
            base = f"{b.name}:{t.number or t.name}" if b.name else (t.name or t.number)
            n = base
            while n in seen:
                n += "'"
            seen.add(n)
            t.qet_name = n
    if not blocks:
        raise ValueError("no terminals in the table")
    return blocks


# --------------------------------------------------------------------------
# Drawing the symbol
# --------------------------------------------------------------------------

def _cut(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip(" ,;:-(") + "…"


def draw_symbol(blocks: list, title: list, lay: Layout) -> tuple:
    """Parts (QElectroTech description tags) and terminals of the symbol. The box starts at (0, 0)."""
    P, L = lay.pitch, lay.lead
    for b in blocks:
        for t in b.terms:
            t.function = _cut(t.function, lay.function_chars)
    side = {s: [b for b in blocks if b.side == s] for s in SIDES}

    # top / bottom: one column per terminal, an empty column between blocks
    def columns(bs):
        cols, spans, j = [], [], 0
        for b in bs:
            first = j
            for t in b.terms:
                cols.append((j, t)); j += 1
            spans.append((b, first, j - 1)); j += 1
        return cols, spans, max(j - 1, 0)

    # left / right: one row per terminal, a header row above each block
    def rows(bs):
        out, heads, i = [], [], 0
        for b in bs:
            heads.append((b, i)); i += 1
            for t in b.terms:
                out.append((i, t)); i += 1
        return out, heads, i

    tcols, tspans, nt = columns(side["top"])
    bcols, bspans, nb = columns(side["bottom"])
    lrows, lheads, nl = rows(side["left"])
    rrows, rheads, nr = rows(side["right"])

    band = lambda cols: (max(_w(t.name, lay.name_pt) for _j, t in cols) + 6) if cols else 0
    top_band, bot_band = band(tcols), band(bcols)
    head_h = 12
    top_h = _up10(top_band + head_h + 4) if tcols else 0
    bot_h = _up10(bot_band + head_h + 4) if bcols else 0

    def frame_w(bs):
        if not bs:
            return 0
        return _up10(max([_w(t.name, lay.name_pt) for b in bs for t in b.terms] +
                         [_w(b.name, lay.name_pt) for b in bs]) + 10)

    lf, rf = frame_w(side["left"]), frame_w(side["right"])
    func_w = lambda bs: (max([_w(t.function, lay.small_pt) for b in bs for t in b.terms] + [0]) + 8) \
        if lay.functions and bs else 0
    lfunc, rfunc = func_w(side["left"]), func_w(side["right"])
    title = [x for x in title if x]
    title_w = max([_w(x, lay.title_pt if i == 0 else lay.name_pt) for i, x in enumerate(title)] + [60]) + 16
    W = _up10(max(lf + lfunc + title_w + rfunc + rf, nt * P + 2 * P, nb * P + 2 * P, 80))

    y0 = _up10(top_h + P)                          # first side row
    side_rows = max(nl, nr)
    LABEL_H = 36                                   # label (9 pt) and comment (7 pt) above the title
    title_h = 14 * len(title) + 32 + LABEL_H
    body_mid = max(side_rows * P, title_h)
    H = _up10(y0 + body_mid + bot_h)
    H = max(H, _up10(top_h + LABEL_H + 14 * len(title) + 24 + bot_h))
    if side_rows:
        H = max(H, _up10(y0 + (side_rows - 1) * P + P / 2 + bot_h + 10))

    parts, qterms = [], []
    thin = D._style("thin")
    line = lambda x1, y1, x2, y2, st=None: parts.append(
        ("line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "end1": "none", "end2": "none",
                  "length1": "1.5", "length2": "1.5", "antialias": "false", "style": st or D._style()}))
    text = lambda x, y, s, pt, rot=0, bold=False: parts.append(
        ("text", {"x": x, "y": y, "text": s, "rotation": rot, "font": _font(pt, bold), "color": "#000000"}))
    rect = lambda x, y, w, h, st=None: parts.append(
        ("rect", {"x": x, "y": y, "width": w, "height": h, "antialias": "false", "style": st or thin}))

    rect(0, 0, W, H, D._style())

    # top and bottom
    for cols, spans, n, where in ((tcols, tspans, nt, "top"), (bcols, bspans, nb, "bottom")):
        if not cols:
            continue
        x0 = int(round(((W - (n - 1) * P) / 2) / 10) * 10)
        bnd = top_band if where == "top" else bot_band
        for j, t in cols:
            x = x0 + j * P
            if where == "top":
                line(x, 0, x, -L); qterms.append((x, -L, "n", t.qet_name))
                text(x + 3, bnd + 1, t.name, lay.name_pt, 270)
                if t.number:
                    text(x + 2, -2, t.number, lay.small_pt)
            else:
                line(x, H, x, H + L); qterms.append((x, H + L, "s", t.qet_name))
                text(x + 3, H - 3, t.name, lay.name_pt, 270)
                if t.number:
                    text(x + 2, H + 7, t.number, lay.small_pt)
        for b, a, z in spans:
            xa, xz = x0 + a * P - P / 2 + 2, x0 + z * P + P / 2 - 2
            if where == "top":
                rect(xa, 2, xz - xa, bnd + head_h)
                line(xa, bnd + 2, xz, bnd + 2, thin)
                for j in range(a, z):
                    line(x0 + j * P + P / 2, 2, x0 + j * P + P / 2, bnd + 2, thin)
                text((xa + xz) / 2 - _w(b.name, lay.name_pt) / 2, bnd + head_h - 1, b.name, lay.name_pt, bold=True)
            else:
                rect(xa, H - 2 - bnd - head_h, xz - xa, bnd + head_h)
                line(xa, H - 2 - bnd, xz, H - 2 - bnd, thin)
                for j in range(a, z):
                    line(x0 + j * P + P / 2, H - 2 - bnd, x0 + j * P + P / 2, H - 2, thin)
                text((xa + xz) / 2 - _w(b.name, lay.name_pt) / 2, H - 2 - bnd - 3, b.name, lay.name_pt, bold=True)

    # left and right
    for rws, heads, fw, where in ((lrows, lheads, lf, "left"), (rrows, rheads, rf, "right")):
        if not rws:
            continue
        fx = 2 if where == "left" else W - 2 - fw
        for b, i in heads:
            y = y0 + i * P
            last = y + len(b.terms) * P
            rect(fx, y - P / 2 + 2, fw, last - y + P - 4)
            line(fx, y + P / 2, fx + fw, y + P / 2, thin)
            text(fx + fw / 2 - _w(b.name, lay.name_pt) / 2, y + 3, b.name, lay.name_pt, bold=True)
            for k in range(1, len(b.terms)):
                line(fx, y + k * P + P / 2, fx + fw, y + k * P + P / 2, thin)
        for i, t in rws:
            y = y0 + i * P
            if where == "left":
                line(0, y, -L, y); qterms.append((-L, y, "w", t.qet_name))
                text(4, y + 3, t.name, lay.name_pt)
                if t.number:
                    text(-2 - max(_w(t.number, lay.small_pt), 5), y - 2, t.number, lay.small_pt)
                if lay.functions and t.function:
                    text(fx + fw + 4, y + 3, t.function, lay.small_pt)
            else:
                line(W, y, W + L, y); qterms.append((W + L, y, "e", t.qet_name))
                text(fx + 4, y + 3, t.name, lay.name_pt)
                if t.number:
                    text(W + 1, y - 2, t.number, lay.small_pt)
                if lay.functions and t.function:
                    text(fx - 6 - _w(t.function, lay.small_pt), y + 3, t.function, lay.small_pt)

    # title in the middle, label and comment above it
    left_end, right_start = lf + lfunc, W - rf - rfunc
    cx = (left_end + right_start) / 2
    ty = max(top_h + LABEL_H + 18, (H - 14 * len(title)) / 2 + 10)
    for k, s in enumerate(title):
        pt = lay.title_pt if k == 0 else lay.name_pt
        text(cx - _w(s, pt, k == 0) / 2, ty + 14 * k, s, pt, bold=(k == 0))
    parts.append(("dynamic_text", {"x": left_end + 6, "y": top_h + 4, "info": "label", "size": 9}))
    parts.append(("dynamic_text", {"x": left_end + 6, "y": top_h + 17, "info": "comment", "size": 7}))
    return parts, qterms, W, H


def _uuid(*key) -> str:
    """The same uuid for the same device and terminal, so a symbol drawn again keeps the wires of
    the projects that already use it."""
    return "{" + str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, "terminals2qet:" + ":".join(key))) + "}"


SIDE_WORDS = {"top": "top", "bottom": "bottom", "left": "left", "right": "right",
              "zgoraj": "top", "spodaj": "bottom", "levo": "left", "desno": "right"}


def draw_front_box(w_mm: float, h_mm: float, lines: list, side: str = "", ports: str = "",
                   px_per_mm: float = 2.0) -> tuple:
    """Cabinet front view from the outer dimensions alone: the outline at true scale, the name inside and a
    strip on the side the cables come in. Nothing is drawn where the manufacturer's drawing shows nothing."""
    W, H = round(w_mm * px_per_mm, 2), round(h_mm * px_per_mm, 2)
    thin = D._style("thin")
    r = round(min(6.0, W / 12, H / 12), 1)
    parts = [("rect", {"x": 0, "y": 0, "width": W, "height": H, "rx": r, "ry": r, "antialias": "false",
                       "style": D._style()})]
    if W >= 40 and H >= 40:
        parts.append(("rect", {"x": 3, "y": 3, "width": W - 6, "height": H - 6, "rx": max(r - 2, 0),
                               "ry": max(r - 2, 0), "antialias": "false", "style": thin}))
    side = SIDE_WORDS.get((side or "").strip().lower(), "")
    if side:
        t = round(max(4.0, min(14.0, (H if side in ("top", "bottom") else W) * 0.08)), 1)
        x, y, w, h = {"bottom": (3, H - 3 - t, W - 6, t), "top": (3, 3, W - 6, t),
                      "left": (3, 3, t, H - 6), "right": (W - 3 - t, 3, t, H - 6)}[side]
        parts.append(("rect", {"x": x, "y": y, "width": w, "height": h, "antialias": "false",
                               "style": "line-style:dashed;line-weight:thin;filling:none;color:black"}))
        if ports and side in ("top", "bottom"):
            pt = max(3, min(12, int((W - 10) / (0.62 * 1.33 * max(len(ports), 1))), int(t / 1.33)))
            if pt * 1.33 < t + 1:
                parts.append(("text", {"x": W / 2 - _w(ports, pt) / 2, "y": y + t / 2 + pt * 0.45, "text": ports,
                                       "rotation": 0, "font": _font(pt), "color": "#000000"}))
    lines = [x for x in lines if x]
    if lines:
        size = lambda txt, bold: max(3, min(24, int((W * 0.7) / ((0.68 if bold else 0.62) * 1.33 * max(len(txt), 1)))))
        sizes = [size(x, i == 0) for i, x in enumerate(lines)]
        step = [sz * 1.33 + 4 for sz in sizes]
        y = max(H * 0.35 - sum(step) / 2, 6) + step[0]
        for i, (txt, pt) in enumerate(zip(lines, sizes)):
            parts.append(("text", {"x": W / 2 - _w(txt, pt, i == 0) / 2, "y": y, "text": txt, "rotation": 0,
                                   "font": _font(pt, i == 0), "color": "#000000"}))
            y += step[i + 1] if i + 1 < len(step) else 0
    parts.append(("dynamic_text", {"x": 0, "y": -14, "info": "label", "size": 8}))
    return parts, W, H


def build_front_box(spec_dims: dict, names: dict, info: dict, lines: list, side: str, ports: str,
                    informations: str, key: str = "", px_per_mm: float = 2.0) -> ET.Element:
    w, h = float(spec_dims["width"]), float(spec_dims["height"])
    parts, W, H = draw_front_box(w, h, lines, side, ports, px_per_mm)
    x0, y0 = -10, -20
    x1, y1 = _up10(W + 10), _up10(H + 10)
    root = ET.Element("definition", {"version": "0.200", "type": "element", "link_type": "thumbnail",
                                     "width": str(int(x1 - x0)), "height": str(int(y1 - y0)),
                                     "hotspot_x": str(int(-x0)), "hotspot_y": str(int(-y0))})
    key = key or json.dumps(sorted(names.items()), ensure_ascii=False)   # no id: the names identify it
    ET.SubElement(root, "uuid", {"uuid": _uuid(key, "front")})
    nm = ET.SubElement(root, "names")
    for lang in sorted(names):
        ET.SubElement(nm, "name", {"lang": lang}).text = names[lang]
    ei = ET.SubElement(root, "elementInformations")
    for k, v in info.items():
        if v not in (None, ""):
            ET.SubElement(ei, "elementInformation", {"name": k, "show": "1"}).text = str(v)
    ET.SubElement(root, "informations").text = informations
    desc = ET.SubElement(root, "description")
    for kind, a in parts:
        if kind == "dynamic_text":
            d = ET.SubElement(desc, "dynamic_text", {
                "x": D._fmt(a["x"]), "y": D._fmt(a["y"]), "z": "10", "text_width": "-1",
                "Halignment": "AlignLeft", "Valignment": "AlignTop", "frame": "false", "rotation": "0",
                "keep_visual_rotation": "false", "text_from": "ElementInfo",
                "uuid": _uuid(key, "text", a["info"], str(len(desc))), "font": D.FONT.format(a["size"])})
            ET.SubElement(d, "text").text = ""
            ET.SubElement(d, "info_name").text = a["info"]
        else:
            ET.SubElement(desc, kind, {k: (D._fmt(v) if isinstance(v, (int, float)) else str(v)) for k, v in a.items()})
    return root


def build_element(parts, qterms, W, H, names: dict, info: dict, informations: str, lay: Layout,
                  link_type="simple", key="") -> ET.Element:
    # room for the terminal numbers outside the box, everything on the 10 px grid
    x0, y0, x1, y1 = -lay.lead - 10, -lay.lead - 10, W + lay.lead + 20, H + lay.lead + 10
    root = ET.Element("definition", {"version": "0.200", "type": "element", "link_type": link_type,
                                     "width": str(int(x1 - x0)), "height": str(int(y1 - y0)),
                                     "hotspot_x": str(int(-x0)), "hotspot_y": str(int(-y0))})
    key = key or json.dumps(sorted(names.items()), ensure_ascii=False)   # no id: the names identify it
    ET.SubElement(root, "uuid", {"uuid": _uuid(key)})
    nm = ET.SubElement(root, "names")
    for lang in sorted(names):
        ET.SubElement(nm, "name", {"lang": lang}).text = names[lang]
    if info:
        ei = ET.SubElement(root, "elementInformations")
        for k, v in info.items():
            if v not in (None, ""):
                ET.SubElement(ei, "elementInformation", {"name": k, "show": "1"}).text = str(v)
    ET.SubElement(root, "informations").text = informations
    desc = ET.SubElement(root, "description")
    for kind, a in parts:
        if kind == "dynamic_text":
            d = ET.SubElement(desc, "dynamic_text", {
                "x": D._fmt(a["x"]), "y": D._fmt(a["y"]), "z": "10", "text_width": "-1",
                "Halignment": "AlignLeft", "Valignment": "AlignTop", "frame": "false", "rotation": "0",
                "keep_visual_rotation": "false", "text_from": "ElementInfo",
                "uuid": _uuid(key, "text", a["info"], str(len(desc))), "font": D.FONT.format(a["size"])})
            ET.SubElement(d, "text").text = ""
            ET.SubElement(d, "info_name").text = a["info"]
        else:
            ET.SubElement(desc, kind, {k: (D._fmt(v) if isinstance(v, (int, float)) else str(v)) for k, v in a.items()})
    for x, y, o, n in qterms:
        ET.SubElement(desc, "terminal", {"uuid": _uuid(key, n),
                                         "x": D._fmt(x), "y": D._fmt(y),
                                         "orientation": o, "type": "Generic", "name": n})
    return root


# --------------------------------------------------------------------------
# A whole device: symbol + front view + 3D dimensions
# --------------------------------------------------------------------------

def make_device(spec: dict, base: Path, out: Path, front_out: Path | None = None, lay: Layout | None = None,
                preview=False) -> dict:
    lay = lay or Layout()
    blocks = blocks_from_spec(spec)
    names = dict(spec.get("names") or {"en": spec.get("id") or out.stem})
    info = dict(spec.get("info") or {})
    if spec.get("id"):
        info.setdefault("manufacturer_reference", spec["id"])
    title = spec.get("title") or [info.get("designation", ""), info.get("manufacturer_reference", "")]
    if isinstance(title, str):
        title = [title]
    report = {"symbol": str(out), "terminals": sum(len(b.terms) for b in blocks),
              "blocks": {b.name: {"side": b.side, "terminals": [t.qet_name for t in b.terms]} for b in blocks}}

    fv = spec.get("front_view")
    if isinstance(fv, str):
        fv = {"file": fv}
    if fv and fv.get("file") and front_out:
        src = (base / fv["file"]).expanduser()
        step = (base / fv["step"]).expanduser() if fv.get("step") else None
        for f in (src, step):
            if f and not f.exists():
                raise FileNotFoundError(f"{f} not found: get the manufacturer's CAD data "
                                        f"({fv.get('download', 'front view and STEP model')}) or leave out the front view")
        fnames = {k: f"{v} ({FRONT_VIEW.get(k, FRONT_VIEW['en'])})" for k, v in names.items()}
        finfo = {k: v for k, v in info.items() if k not in ("width", "height", "depth")}
        finfo["exclude_from_bom"] = "true"            # the schematic symbol carries the bill of materials
        o = D.Options(mode="thumbnail", px_per_mm=float(fv.get("px_per_mm", 2.0)), names=fnames, info=finfo,
                      step=str(step) if step else None,
                      ident="terminals2qet:" + str(spec.get("id") or "") + ":front:" + src.name)
        root, rep = D.convert(D.load(src), o, exclude=fv.get("exclude_layers"))
        D.write_elmt(root, front_out)
        report["front_view"] = {"file": str(front_out), "size_px": [rep["width"], rep["height"]]}
        if rep.get("step_mm"):
            report["front_view"]["step_mm"] = rep["step_mm"]
            for k in ("width", "height", "depth"):
                info.setdefault(k, o.info[k])
            info.setdefault("notes", o.info.get("notes", ""))
        if preview:
            D.preview_svg(front_out, front_out.with_suffix(".svg"))
    elif front_out and spec.get("dimensions_mm"):
        # no manufacturer drawing: the outline from the datasheet dimensions, at the same scale
        dims = {k: v for k, v in spec["dimensions_mm"].items() if v}
        fv = fv or {}
        for k, short in (("width", "width"), ("height", "height"), ("depth", "depth")):
            if dims.get(k):
                info.setdefault(short, f"{float(dims[k]):g}")
        fnames = {k: f"{v} ({FRONT_VIEW.get(k, FRONT_VIEW['en'])})" for k, v in names.items()}
        finfo = dict(info, exclude_from_bom="true")
        side = fv.get("side") or fv.get("stran_prikljuckov") or ""
        ports = fv.get("ports") or " · ".join(dict.fromkeys(b.name for b in blocks if b.side in ("top", "bottom", "left")))
        root = build_front_box(dims, fnames, finfo, title[:2], side, ports,
                               (spec.get("source", "") + "\n" if spec.get("source") else "") +
                               f"Outline from the datasheet dimensions, terminals2qet {__version__}",
                               key=str(spec.get("id") or ""), px_per_mm=float(fv.get("px_per_mm", 2.0)))
        D.write_elmt(root, front_out)
        report["front_view"] = {"file": str(front_out), "box_mm": [dims.get("width"), dims.get("height"), dims.get("depth")]}
        if preview:
            D.preview_svg(front_out, front_out.with_suffix(".svg"))

    parts, qterms, W, H = draw_symbol(blocks, title, lay)
    src_note = spec.get("source", "")
    informations = (src_note + "\n" if src_note else "") + f"Drawn with terminals2qet {__version__} from the terminal table"
    root = build_element(parts, qterms, W, H, names, info, informations, lay, spec.get("link_type", "simple"),
                         key=str(spec.get("id") or info.get("manufacturer_reference") or ""))
    D.write_elmt(root, out)
    report.update({"box_px": [W, H], "width": int(root.get("width")), "height": int(root.get("height"))})
    if preview:
        D.preview_svg(out, out.with_suffix(".svg"))
        report["preview_svg"] = str(out.with_suffix(".svg"))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="QElectroTech device symbol from a terminal table (JSON or CSV)")
    ap.add_argument("table", help="JSON spec or CSV table (block, terminal, name, side, function)")
    ap.add_argument("-o", "--output", required=True, help="schematic symbol .elmt")
    ap.add_argument("--front-out", help="cabinet front view .elmt (needs front_view in the JSON spec)")
    ap.add_argument("--name", action="append", help="lang=name (CSV tables)")
    ap.add_argument("--info", action="append", help="key=value element information (CSV tables)")
    ap.add_argument("--title", action="append", help="title line in the box, repeatable (CSV tables)")
    ap.add_argument("--pitch", type=int, default=20, help="px between terminals (multiple of 10)")
    ap.add_argument("--lead", type=int, default=10, help="px from the box to a terminal (multiple of 10)")
    ap.add_argument("--no-functions", action="store_true", help="do not write the functions next to side terminals")
    ap.add_argument("--preview", action="store_true", help="also write .svg previews")
    ap.add_argument("--check", action="store_true", help="run qelectrotech --check-elements on the result")
    ap.add_argument("--json", action="store_true", help="print the report")
    ap.add_argument("--version", action="version", version=__version__)
    ns = ap.parse_args(argv)
    if ns.pitch % 10 or ns.lead % 10 or ns.pitch <= 0 or ns.lead <= 0:
        raise SystemExit("--pitch and --lead must be multiples of 10 px")
    src = Path(ns.table)
    spec = read_spec(src)
    if ns.name:
        spec["names"] = D._parse_kv(ns.name)
    if ns.info:
        spec.setdefault("info", {}).update(D._parse_kv(ns.info))
    if ns.title:
        spec["title"] = ns.title
    rep = make_device(spec, src.parent, Path(ns.output), Path(ns.front_out) if ns.front_out else None,
                      Layout(pitch=ns.pitch, lead=ns.lead, functions=not ns.no_functions), ns.preview)
    if ns.check:
        for p in [ns.output] + ([ns.front_out] if ns.front_out else []):
            rep.setdefault("check", []).append(D.check_with_qelectrotech(Path(p)))
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"{ns.output}: {rep['terminals']} terminals in {len(rep['blocks'])} blocks, box {rep['box_px'][0]} x {rep['box_px'][1]} px")
        if "front_view" in rep:
            print(f"{ns.front_out}: front view {rep['front_view']}")
        for c in rep.get("check", []):
            print(c)


if __name__ == "__main__":
    main()
