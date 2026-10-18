# Element collections made with dwg2qet / terminals2qet

Ready-to-use QElectroTech elements, generated (not drawn by hand) with the tools in this repository.

| folder | content |
|---|---|
| [`SOLION_PONUDBE`](SOLION_PONUDBE) | 288 PV and storage devices: inverters, MPPT chargers, batteries, gateways / backup units, meters, optimizers, EV chargers and PV modules (Sigenergy, Victron, SolarEdge, GoodWe, Dyness, Pylontech, FoxESS, Sungrow, AndSolar, Tigo, Teltonika, AIKO, LONGi, Trina, Jolywood, Luxor). For each device a schematic symbol with **all its terminals** (`shema/`) and a cabinet / wall front view at 2 px = 1 mm (`omara_2D/`, 286 views). |
| [`SEW`](SEW) | SEW-EURODRIVE MOVIDRIVE B MDX61B0015-5A3-4-00 and MOVITRAC B MC07B0015-5A3-4-00: schematic symbols with every terminal of the manual's terminal assignment tables (`shema/`). |

The device list follows the price list of the SOLION offer generator (hence the folder name, *ponudbe* = offers).
Deye is not included.

## What is in an element

* **Terminals** named as on the device (`PV1+`, `GRID:L1`, `COM:13`, `X13:DI00` …), grouped in blocks on the sides
  of the symbol (supply on top, outputs at the bottom, inputs left, communication right) and on the 10 px grid.
* **Element information**: manufacturer, manufacturer reference, designation, a short technical description
  (MPPT count and ranges, battery voltage, AC data, communication ports), mass and width / height / depth in mm,
  so the bill of materials and 3D exports of a cabinet fill in by themselves.
* **Sources** in the element's `notes` / informations field: the manufacturer document, edition and page the
  terminals and values were taken from.
* **Stable uuids** (uuid5, see the main README): generating the collection again gives the same element and
  terminal uuids, so projects that use an element keep their wires.

The cabinet front views are **outlines from the datasheet dimensions** (box with the connection side), not
copies of manufacturer CAD drawings. For SEW the front view comes from SEW's own CAD data, which is not
redistributed here: download the 2D front view (DXF) and 3D model (STEP) from SEW Online Support and run
`terminals2qet.py examples/terminals/sew_movidrive_b_MDX61B0015-5A3-4-00.json --front-out …` to make it.

## Use

Copy a folder into your QElectroTech user collection, for example on macOS
`~/Library/Application Support/QElectroTech/QElectroTech/elements/`. The elements then appear under
*User collection*. Tested with QElectroTech 0.200.1 (`qelectrotech --check-elements`: 0 failures; the front
views have no terminals by design).

**Check every element against the current datasheet of the exact model before use.** Manufacturers change
products and documents; the data here is as of October 2026.

## Licence

The elements are released under **CC-BY 3.0**, the licence of the QElectroTech element collection, by
David Podlesnik. Product and company names are trademarks of their owners; this collection is not affiliated
with or endorsed by any manufacturer.
