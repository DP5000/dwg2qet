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

    def test_arc_extent_is_the_arc_not_the_circle(self):
        # a quarter arc of radius 10 from 0 to 90 degrees covers x 0..10, y 0..10, not -10..10
        arc = D.Prim("arc", cx=0, cy=0, r=10, a0=0, a1=90)
        self.assertEqual([round(v, 6) for v in D.bbox([arc])], [0, 0, 10, 10])

    def test_depth_from_step(self):
        # a 3D model 80 wide, 160 deep, 287 high; the front view is 80 x 287
        step = ("ISO-10303-21;\nDATA;\n"
                + "".join(f"#{i}=CARTESIAN_POINT('',({x},{y},{z}));\n#{i + 100}=VERTEX_POINT('',#{i});\n"
                          for i, (x, y, z) in enumerate([(0, 0, 0), (80, 160, 287), (40, 10, 100)], 1))
                + "ENDSEC;\nEND-ISO-10303-21;\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "box.stp"; path.write_text(step)
            w, h, d = D.depth_from_step(D.step_dimensions(path), 80, 287)
        self.assertEqual((round(w), round(h), round(d)), (80, 287, 160))

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


    # --- review of Laurent Trinques (discussion #1452) ---------------------------------------------

    def uuids(self, root):
        return [e.get("uuid") for e in root.iter() if e.tag in ("uuid", "terminal", "dynamic_text")]

    def test_uuids_are_deterministic(self):
        r1, _ = self.convert(BREAKER, ident=D.source_ident("breaker.dxf"))
        r2, _ = self.convert(BREAKER, ident=D.source_ident("breaker.dxf"))
        r3, _ = self.convert(BREAKER, ident=D.source_ident("other.dxf"))
        u1, u2, u3 = self.uuids(r1), self.uuids(r2), self.uuids(r3)
        self.assertEqual(u1, u2)                         # regenerated: same element, same terminals
        self.assertEqual(len(set(u1)), len(u1))          # all different inside one element
        self.assertTrue(set(u1).isdisjoint(u3))          # another drawing: other uuids
        thumb, _ = self.convert(BREAKER, mode="thumbnail", ident=D.source_ident("breaker.dxf", "thumbnail"))
        self.assertNotEqual(self.uuids(thumb)[0], u1[0])  # symbol and cabinet view are two elements

    def test_cli_keeps_uuids_when_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.dxf").write_text(dxf(BREAKER))
            got = []
            for _ in range(2):
                self.assertEqual(D.main([str(tmp / "a.dxf"), "-o", str(tmp / "a.elmt")]), 0)
                got.append(self.uuids(ET.parse(tmp / "a.elmt").getroot()))
            self.assertEqual(got[0], got[1])

    def test_split_on_line_feeds_only(self):
        # \x0b and \u2028 inside a text value must not shift the following code/value pairs
        body = BREAKER + text(5, 5, "A\x0bB\u2028C", 0.8)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(body), encoding="utf-8")
            pairs = D.read_pairs(p)
            self.assertIn((1, "A\x0bB\u2028C"), pairs)
            self.assertTrue(all(isinstance(c, int) for c, _ in pairs))
        root, rep = self.convert(body)                   # the element is still well-formed XML
        self.assertEqual(rep["terminals"], 2)
        self.assertIn("A B\u2028C", [e.get("text") for e in root.iter("text")])

    def test_arc_in_non_uniformly_scaled_block_is_elliptical(self):
        blocks = ("0\nBLOCK\n8\n0\n2\nRING\n10\n0\n20\n0\n70\n0\n"
                  "0\nCIRCLE\n8\n0\n10\n0\n20\n0\n40\n1\n0\nENDBLK\n")
        ins = "0\nINSERT\n8\nSYM\n2\nRING\n10\n0\n20\n0\n41\n3\n42\n1\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(ins, blocks))
            prims = D.flatten(D.read_dxf(p))
        self.assertEqual([q.kind for q in prims], ["polyline"])          # not a circle any more
        xs = [x for x, _ in prims[0].pts]; ys = [y for _, y in prims[0].pts]
        self.assertAlmostEqual(max(xs) - min(xs), 6.0, places=2)           # 2 r · sx
        self.assertAlmostEqual(max(ys) - min(ys), 2.0, places=2)           # 2 r · sy
        uniform = ins.replace("41\n3\n42\n1", "41\n2\n42\n2")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(uniform, blocks))
            prims = D.flatten(D.read_dxf(p))
        self.assertEqual([q.kind for q in prims], ["circle"])              # uniform scale keeps the circle

    def test_insert_on_frozen_layer_is_hidden(self):
        blocks = ("0\nBLOCK\n8\n0\n2\nLEAD\n10\n0\n20\n0\n70\n0\n" + line(0, 0, 0, 3, layer="SYM") + "0\nENDBLK\n")
        body = "0\nINSERT\n8\nSYM\n2\nLEAD\n10\n0\n20\n0\n" + "0\nINSERT\n8\nHIDDEN\n2\nLEAD\n10\n5\n20\n0\n"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(body, blocks))
            prims = D.flatten(D.read_dxf(p))
        self.assertEqual(len(prims), 1)                  # the copy on the frozen layer HIDDEN is gone
        self.assertEqual(prims[0].pts[0][0], 0.0)

    def test_polyface_mesh_draws_its_edges(self):
        v = lambda x, y, f: f"0\nVERTEX\n8\nSYM\n10\n{x}\n20\n{y}\n70\n{f}\n"
        face = lambda a, b, c, d: f"0\nVERTEX\n8\nSYM\n10\n0\n20\n0\n70\n128\n71\n{a}\n72\n{b}\n73\n{c}\n74\n{d}\n"
        body = ("0\nPOLYLINE\n8\nSYM\n66\n1\n70\n64\n71\n4\n72\n2\n" + v(0, 0, 192) + v(4, 0, 192) + v(4, 4, 192) +
                v(0, 4, 192) + face(1, 2, 3, 0) + face(1, 3, -4, 0) + "0\nSEQEND\n")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(body))
            prims = D.flatten(D.read_dxf(p))
        edges = {tuple(sorted((q.pts[0], q.pts[1]))) for q in prims}
        self.assertTrue(all(len(q.pts) == 2 for q in prims))           # edges, not one polyline
        self.assertIn(((0.0, 0.0), (4.0, 4.0)), edges)                   # the shared diagonal, once
        self.assertNotIn(((0.0, 0.0), (0.0, 4.0)), edges)                # 4 -> 1 marked invisible (-4)
        self.assertEqual(len(prims), 4)

    def test_polygon_mesh_draws_its_grid(self):
        v = lambda x, y: f"0\nVERTEX\n8\nSYM\n10\n{x}\n20\n{y}\n70\n64\n"
        body = ("0\nPOLYLINE\n8\nSYM\n66\n1\n70\n16\n71\n2\n72\n3\n" +
                "".join(v(x, y) for y in (0, 2) for x in (0, 1, 2)) + "0\nSEQEND\n")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.dxf"
            p.write_text(dxf(body))
            prims = D.flatten(D.read_dxf(p))
        self.assertEqual(len(prims), 2 + 3)              # M rows + N columns

    def test_binary_dxf_gives_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "b.dxf"
            p.write_bytes(D.BINARY_DXF + b"\r\n\x1a\x00" + bytes(64))
            with self.assertRaises(ValueError) as cm:
                D.read_pairs(p)
            self.assertIn("binary DXF", str(cm.exception))
            self.assertEqual(D.main([str(p), "-o", str(Path(tmp) / "b.elmt")]), 2)

    def test_element_version(self):
        root, _ = self.convert(BREAKER)
        self.assertEqual(root.get("version"), "0.200")


if __name__ == "__main__":
    unittest.main()
