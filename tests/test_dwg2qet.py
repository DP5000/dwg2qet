"""Tests for dwg2qet on synthetic DXF drawings (no third-party CAD data needed).

Run:  python3 -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dwg2qet as D  # noqa: E402


def dxf(entities: str, blocks: str = "") -> str:
    return ("0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n"
            "0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n0\nLAYER\n2\nSYM\n70\n0\n62\n7\n"
            "0\nLAYER\n2\nHIDDEN\n70\n1\n62\n7\n0\nENDTAB\n0\nENDSEC\n"
            "0\nSECTION\n2\nBLOCKS\n" + blocks + "0\nENDSEC\n"
            "0\nSECTION\n2\nENTITIES\n" + entities + "0\nENDSEC\n0\nEOF\n")


def line(x1, y1, x2, y2, layer="SYM"):
    return f"0\nLINE\n8\n{layer}\n10\n{x1}\n20\n{y1}\n11\n{x2}\n21\n{y2}\n"


def text(x, y, s, h=1.0, layer="SYM"):
    return f"0\nTEXT\n8\n{layer}\n10\n{x}\n20\n{y}\n40\n{h}\n1\n{s}\n"


# a circuit breaker: lead in at the top, blade, lead out at the bottom, a cross on the fixed contact
BREAKER = (line(0, 10, 0, 7) + line(0, 3, -1.2, 6.8) + line(0, 3, 0, 0) +
           line(-0.3, 6.7, 0.3, 7.3) + line(-0.3, 7.3, 0.3, 6.7) +
           text(0.4, 9, "1", 0.8) + text(0.4, 0.4, "2", 0.8))

# a contactor: coil A1/A2 on the left (inside the drawing), one main contact 1-2 on the right
CONTACTOR = (line(-4, 8, -4, 6) + "0\nLWPOLYLINE\n8\nSYM\n90\n4\n70\n1\n10\n-5\n20\n6\n10\n-3\n20\n6\n10\n-3\n20\n4\n10\n-5\n20\n4\n" +
             line(-4, 4, -4, 2) + text(-3.8, 7.2, "A1", 0.6) + text(-3.8, 2.3, "A2", 0.6) +
             line(0, 10, 0, 7) + line(0, 3, -1, 6.8) + line(0, 3, 0, 0) +
             text(0.3, 9.2, "1", 0.6) + text(0.3, 0.3, "2", 0.6))


class Dwg2QetTest(unittest.TestCase):
    def convert(self, body, mode="symbol", blocks="", **kw):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(body, blocks))
            root, rep = D.convert(D.read_dxf(p), D.Options(mode=mode, names={"en": "t"}, **kw))
            out = Path(tmp) / "t.elmt"
            D.write_elmt(root, out)
            ET.parse(out)                      # well-formed
            D.preview_svg(out, Path(tmp) / "t.svg")
            return root, rep

    def test_breaker_terminals_on_grid(self):
        root, rep = self.convert(BREAKER)
        self.assertEqual(rep["terminals"], 2)
        terms = {t["name"]: t for t in rep["terminal_list"]}
        self.assertEqual(set(terms), {"1", "2"})
        self.assertEqual(terms["1"]["orientation"], "n")
        self.assertEqual(terms["2"]["orientation"], "s")
        for t in rep["terminal_list"]:
            self.assertEqual(t["x"] % 10, 0)
            self.assertEqual(t["y"] % 10, 0)
        self.assertEqual(root.get("link_type"), "simple")
        for k in ("width", "height", "hotspot_x", "hotspot_y"):
            self.assertTrue(root.get(k).lstrip("-").isdigit())

    def test_coil_terminals_inside_the_drawing(self):
        _, rep = self.convert(CONTACTOR)
        names = sorted(t["name"] for t in rep["terminal_list"])
        self.assertEqual(names, ["1", "2", "A1", "A2"])

    def test_hidden_layer_and_filters(self):
        _, rep = self.convert(BREAKER + line(5, 0, 5, 10, layer="HIDDEN"))
        self.assertEqual(rep["terminals"], 2)
        with self.assertRaises(ValueError):          # nothing visible
            D.convert(D.Drawing({}, {}, {}, []), D.Options())

    def test_thumbnail_scale_and_info(self):
        box = line(0, 0, 18, 0) + line(18, 0, 18, 80) + line(18, 80, 0, 80) + line(0, 80, 0, 0)
        root, rep = self.convert(box, mode="thumbnail", px_per_mm=2.0,
                                 info={"width": "18", "height": "80", "depth": "70"})
        self.assertEqual(root.get("link_type"), "thumbnail")
        self.assertEqual((rep["width"], rep["height"]), (40, 160))
        info = {e.get("name"): e.text for e in root.iter("elementInformation")}
        self.assertEqual(info["depth"], "70")

    def test_block_insert_and_bulge(self):
        blocks = ("0\nBLOCK\n8\n0\n2\nLEAD\n10\n0\n20\n0\n70\n0\n" + line(0, 0, 0, 3, layer="0") + "0\nENDBLK\n")
        body = ("0\nINSERT\n8\nSYM\n2\nLEAD\n10\n0\n20\n7\n" + "0\nINSERT\n8\nSYM\n2\nLEAD\n10\n0\n20\n-3\n" +
                "0\nLWPOLYLINE\n8\nSYM\n90\n2\n70\n0\n10\n0\n20\n7\n42\n1\n10\n0\n20\n0\n")
        _, rep = self.convert(body, blocks=blocks)
        self.assertGreaterEqual(rep["primitives"], 3)
        self.assertEqual(rep["terminals"], 2)

    def test_unique_terminal_names(self):
        body = (line(0, 10, 0, 6) + line(0, 4, 0, 0) + line(-1, 6, 1, 6) + line(-1, 4, 1, 4) +
                text(0.3, 9, "N", 0.8) + text(0.3, 0.5, "N", 0.8))
        _, rep = self.convert(body)
        names = [t["name"] for t in rep["terminal_list"]]
        self.assertEqual(len(names), len(set(names)))

    def test_cli_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.dxf").write_text(dxf(BREAKER))
            (tmp / "m.csv").write_text("source,output,name_en,info_manufacturer,category_en\n"
                                       f"{tmp / 'a.dxf'},lib/breakers/a.elmt,Breaker,ACME,Breakers\n")
            rc = D.main(["--batch", str(tmp / "m.csv"), "--out-dir", str(tmp)])
            self.assertEqual(rc, 0)
            self.assertTrue((tmp / "lib/breakers/a.elmt").exists())
            self.assertTrue((tmp / "lib/breakers/qet_directory").exists())


if __name__ == "__main__":
    unittest.main()
