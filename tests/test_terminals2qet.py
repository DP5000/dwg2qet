"""Tests for terminals2qet on a small made-up drive (no manufacturer data needed).

Run:  python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terminals2qet as T  # noqa: E402

SPEC = {
    "id": "DRIVE-1",
    "names": {"en": "Test drive"},
    "title": ["TEST DRIVE", "DRIVE-1"],
    "info": {"manufacturer": "ACME"},
    "blocks": [
        {"block": "X1", "terminals": [["1", "L1"], ["2", "L2"], ["3", "L3"], ["", "PE"]]},
        {"block": "X2", "terminals": [["4", "U"], ["5", "V"], ["6", "W"], ["", "PE"]]},
        {"block": "X13", "side": "left", "terminals": [["1", "DI00", "Enable"], ["2", "DI01", "CW"], ["3", "GND"]]},
        {"block": "X10", "side": "right", "terminals": [["1", "DO01-C"], ["2", "DO01-NO"], ["3", "GND"]]},
    ],
}


def terminals(root):
    return [t.attrib for t in root.iter("terminal")]


class Terminals2QetTest(unittest.TestCase):
    def build(self, spec=SPEC):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "drive.elmt"
            rep = T.make_device(json.loads(json.dumps(spec)), Path(tmp), out)
            return ET.parse(out).getroot(), rep

    def test_sides_names_and_grid(self):
        root, rep = self.build()
        ts = terminals(root)
        self.assertEqual(rep["terminals"], 14)
        by = {t["name"]: t for t in ts}
        # power blocks go top and bottom by their designations, the others where the table says
        self.assertEqual(by["X1:1"]["orientation"], "n")
        self.assertEqual(by["X2:4"]["orientation"], "s")
        self.assertEqual(by["X13:1"]["orientation"], "w")
        self.assertEqual(by["X10:2"]["orientation"], "e")
        # a terminal without a number is named after its designation; GND appears twice, in two blocks
        self.assertIn("X1:PE", by)
        self.assertIn("X2:PE", by)
        self.assertEqual(len(by), len(ts))
        for t in ts:
            self.assertEqual(float(t["x"]) % 10, 0, t)
            self.assertEqual(float(t["y"]) % 10, 0, t)
        self.assertEqual(root.get("link_type"), "simple")
        info = {e.get("name"): e.text for e in root.iter("elementInformation")}
        self.assertEqual(info["manufacturer_reference"], "DRIVE-1")

    def test_same_uuids_when_drawn_again(self):
        a, _ = self.build()
        b, _ = self.build()
        self.assertEqual([t["uuid"] for t in terminals(a)], [t["uuid"] for t in terminals(b)])
        self.assertEqual(a.find("uuid").get("uuid"), b.find("uuid").get("uuid"))

    def test_csv_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "t.csv"
            csv.write_text("block,terminal,name,side,function\nX1,,L,,\nX1,,N,,\nX5,1,IN1,left,Start\nX5,2,IN1,left,Stop\n")
            spec = T.read_spec(csv)
            names = [t.qet_name for b in T.blocks_from_spec(spec) for t in b.terms]
        self.assertEqual(names, ["X1:L", "X1:N", "X5:1", "X5:2"])


if __name__ == "__main__":
    unittest.main()
