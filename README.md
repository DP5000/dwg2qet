# dwg2qet

Convert **DWG and DXF** drawings into **QElectroTech** elements (`.elmt`) that are ready to use:
symbols you can wire straight away, cabinet front views at a true scale, and a whole
manufacturer library in one batch run.

Python 3.9+, standard library only. DWG input uses [LibreDWG](https://www.gnu.org/software/libredwg/)'s `dwg2dxf` when it is installed.

## Why another converter

[dxf2elmt](https://github.com/Vadoola/dxf2elmt), the converter QElectroTech's element editor uses,
turns DXF geometry into an element. dwg2qet adds what a manufacturer CAD library needs before it is usable in a schematic:

| | dxf2elmt | dwg2qet |
|---|---|---|
| DXF geometry (lines, polylines with bulges, arcs, circles, ellipses, splines, texts, blocks, hatches) | ✓ | ✓ |
| DWG input | – | ✓ (LibreDWG) |
| **Terminals**: found on the drawing, named from the drawing's own labels (1, 2, A1, A2, 13, 14, L, N …) | – | ✓ |
| **10 px grid**: scaled and snapped so wires come out straight | – | ✓ |
| Label and comment fields (`%{label}`, `%{comment}`) | – | ✓ |
| **Element information** for the bill of materials: manufacturer, article number, EAN, current rating … | – | ✓ |
| **Width / height / depth in mm** (QElectroTech's own `width`, `height`, `depth` fields) for 2D panel layouts and 3D, also read from the manufacturer's STEP model | – | ✓ |
| Cabinet front view (`thumbnail`) at a fixed px per mm | – | ✓ |
| Batch: a CSV manifest into a categorised collection with translated folder names | – | ✓ |
| SVG preview of each element, `qelectrotech --check-elements` validation | – | ✓ |

## Install

```bash
git clone https://github.com/DP5000/dwg2qet && cd dwg2qet
python3 dwg2qet.py --help
# DWG files: brew install libredwg   (Debian/Ubuntu: apt install libredwg-tools)
```

## Use

```bash
# schematic symbol: terminals on the 10 px grid, label + comment fields, SVG preview
python3 dwg2qet.py breaker.dwg -o breaker.elmt --preview \
    --name en="Circuit breaker C16 1P" --name sl="Odklopnik C16 1p" \
    --info manufacturer=ACME --info manufacturer_reference=MCB-C16-1P --info current_rating="16 A"

# cabinet front view, 2 px = 1 mm, dimensions for 3D
python3 dwg2qet.py breaker_front.dxf --mode thumbnail --px-per-mm 2 \
    --info width=17.7 --info height=80 --info depth=74.9 --exclude-layer "dim.*"

# front view of a drive, width / height / depth taken from its STEP 3D model
python3 dwg2qet.py MDX61B0015_front.dxf --mode thumbnail --step MDX61B0015.stp \
    --info manufacturer=SEW-EURODRIVE --info manufacturer_reference=MDX61B0015-5A3-4-00

# a whole library from a manifest, then let QElectroTech check every file
python3 dwg2qet.py --batch manifest.csv --out-dir ~/qet/elements/ACME --check
```

### Inside QElectroTech's element editor

The element editor's **File > Import DXF** runs `<data folder>/binary/dxf2elmt <file> -v` and opens the element it prints.
dwg2qet speaks the same interface (`-v` / `--stdout`), so a two-line wrapper makes the editor import symbols with terminals:

```sh
# macOS: ~/Library/Application Support/QElectroTech/QElectroTech/binary/dxf2elmt   (chmod +x)
# Linux: ~/.local/share/QElectroTech/QElectroTech/binary/dxf2elmt
#!/bin/sh
exec python3 /path/to/dwg2qet.py "$@"
```

### Manifest (CSV)

One row per element. Columns: `source`, `output` (relative to `--out-dir`), optional `mode`
(`symbol` / `thumbnail`), `layer`, `exclude_layer` (regex, `|`-separated), `link_type`, `step` (STEP model),
`name_<lang>`, `info_<key>`, `kind_<key>`, `category_<lang>` (name of the output folder).
See [`examples/manifest.csv`](examples/manifest.csv).

### Options that matter

| option | |
|---|---|
| `--mode symbol` | find terminals, snap to the grid, add label/comment (default) |
| `--mode thumbnail` | front view at `--px-per-mm` (default 2), no terminals |
| `--layer`, `--exclude-layer` | regex layer filters (dimension and attribute layers are usually excluded) |
| `--terminal x,y` | force terminals (drawing units) when the drawing has no leads |
| `--pitch` | px between poles (default 20, or 10 when the symbol would get too big) |
| `--target` | rough symbol height in px for single-pole symbols (default 60) |
| `--thin-layer` | draw these layers with a thin pen (details of a front view) |
| `--step` | STEP (AP203/AP214) 3D model of the part: width, height and depth in mm come from it, the file name goes to `notes` |
| `--json` | print the conversion report (terminals, size, notes) |

## How terminals are found

1. A **lead** is a straight line end that touches nothing else.
2. It is a terminal when it sits on the **outline** of the drawing and points outwards. That covers a
   breaker fed from the top and the bottom, and the poles of a 4-pole switch.
3. A lead **inside** the drawing also counts when nothing lies beyond its end and it carries a
   **label of its own** that no outline terminal already uses. That covers the A1/A2 coil next to the
   main contacts.
4. Short ticks and marks that sit next to the main leads (trip mechanisms, actuators) are left out.
5. Symbols drawn without a lead (lamps, sockets) get one supply terminal on top. Long strips stay
   drawings (`thumbnail`).
6. Unnamed terminals are numbered, and repeated names get a prime (`N`, `N'`), because QElectroTech
   rejects duplicates.

On a manufacturer library of 1,333 schematic symbols and 8,675 front views, every element loads in
QElectroTech 0.200 (`--check-elements`: 0 failures). The terminals of contactors, auxiliary blocks,
RCDs and breakers match the drawings. Vendor CAD data is not part of this repository. Convert your
own, within its licence.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Licence

MIT. See [LICENSE](LICENSE). QElectroTech is © The QElectroTech Team (GPL-2.0+). dwg2qet only writes files
in its format. Thanks to the QElectroTech, LibreDWG and dxf2elmt projects.

---

### Slovensko

Pretvornik risb DWG in DXF v elemente QElectroTech. Simbol dobi priključke na mreži 10 px s
pravimi imeni (1, 2, A1, A2 …), polja za oznako in podatke ter podatke za kosovnico in mere za
2D in 3D. Pogled od spredaj je v merilu (2 px = 1 mm). Iz manifesta CSV pretvori celotno
knjižnico proizvajalca. Avtor: David Podlesnik (Elektro David Podlesnik s.p.). Licenca MIT.
