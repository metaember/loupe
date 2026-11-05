"""Regression gates for evidence freshness, release boundaries and pin/placement parity."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from electronics import assembly_contract, circuit_contract
from workflow import Project, WorkflowError


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "input.txt").write_text("source")
        config = dict(
            version=1,
            name="fixture",
            review="report.json",
            stages=[
                dict(
                    name="gate",
                    kind="check",
                    inputs=["input.txt"],
                    outputs=["report.json"],
                    reports=["report.json"],
                    command=[
                        "{python}",
                        "-c",
                        'from pathlib import Path; Path("report.json").write_text(\'{"ok":true}\')',
                    ],
                )
            ],
            release=dict(
                targets=["jlcpcb"],
                manifest="delivery/manifest.json",
                bundles=[
                    dict(
                        output="delivery/fixture.zip",
                        files=["input.txt", "report.json"],
                    )
                ],
            ),
        )
        (self.root / "loupe.json").write_text(json.dumps(config))
        self.patch = patch("workflow.tool_versions", return_value={"test-tool": "1"})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.project = Project(self.root)

    def check(self):
        self.project.run("check")
        self.assertTrue(self.project.fresh("gate")[0])

    def test_release_requires_check_then_detects_source_change(self):
        with self.assertRaises(WorkflowError):
            self.project.release()
        self.check()
        manifest = self.project.release()
        with zipfile.ZipFile(self.root / manifest["packages"][0]["file"]) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("SHA256SUMS.txt", z.namelist())
        (self.root / "input.txt").write_text("manual edit")
        self.assertFalse(self.project.fresh("gate")[0])
        with self.assertRaises(WorkflowError):
            self.project.release()

    def test_output_and_manifest_changes_invalidate_evidence(self):
        self.check()
        (self.root / "report.json").write_text('{"ok":true,"edited":true}')
        self.assertFalse(self.project.fresh("gate")[0])
        self.check()
        with patch("workflow.tool_versions", return_value={"test-tool": "2"}):
            newer = Project(self.root)
            self.assertFalse(newer.fresh("gate")[0])
        data = json.loads((self.root / "loupe.json").read_text())
        data["name"] = "changed"
        (self.root / "loupe.json").write_text(json.dumps(data))
        self.assertFalse(self.project.fresh("gate")[0])

    def test_failed_rerun_removes_previous_pass(self):
        self.check()
        with patch("workflow.subprocess.Popen", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.project.run_stage("gate", force=True)
        self.assertFalse(self.project.fresh("gate")[0])

    def test_report_must_explicitly_pass(self):
        self.project.stages["gate"]["command"] = [
            "{python}",
            "-c",
            'from pathlib import Path; Path("report.json").write_text(\'{"ok":false}\')',
        ]
        with self.assertRaises(WorkflowError):
            self.project.run("check")
        self.assertFalse(self.project.record_path("gate").exists())

    def test_missing_input_and_glob_membership_change(self):
        self.project.stages["gate"]["inputs"] = ["*.txt"]
        self.check()
        (self.root / "added.txt").write_text("new")
        self.assertFalse(self.project.fresh("gate")[0])
        (self.root / "added.txt").unlink()
        (self.root / "input.txt").unlink()
        self.assertFalse(self.project.fresh("gate")[0])

    def test_release_rejects_secrets_and_paths_outside_project(self):
        for name in ["secrets.yaml", ".env.local", "../outside", "/tmp/outside"]:
            with self.assertRaises(WorkflowError):
                self.project.path(name)
        with tempfile.NamedTemporaryFile() as f:
            (self.root / "linked").symlink_to(f.name)
            with self.assertRaises(WorkflowError):
                self.project.path("linked")

    def test_release_rejects_unchecked_extra_files(self):
        self.check()
        (self.root / "extra.txt").write_text("unverified")
        self.project.config["release"]["bundles"][0]["files"].append("extra.txt")
        with self.assertRaises(WorkflowError):
            self.project.release()

    def test_failed_second_bundle_keeps_previous_release(self):
        self.check()
        result = self.project.release()
        output = self.root / result["packages"][0]["file"]
        original = output.read_bytes()
        (self.root / "extra.txt").write_text("untracked")
        self.project.config["release"]["bundles"].append(
            dict(output="delivery/bad.zip", files=["extra.txt"])
        )
        with self.assertRaises(WorkflowError):
            self.project.release()
        self.assertEqual(output.read_bytes(), original)
        self.assertFalse((self.root / "delivery/bad.zip").exists())

    def test_added_rule_sidecar_invalidates_previous_pass(self):
        self.project.stages["gate"]["optional_inputs"] = ["*.kicad_dru"]
        self.check()
        (self.root/"board.kicad_dru").write_text("new rule")
        self.assertFalse(self.project.fresh("gate")[0])

    def test_release_cannot_replace_native_source(self):
        self.check()
        self.project.config["release"]["bundles"][0]["output"] = "input.txt"
        with self.assertRaises(WorkflowError):
            self.project.release()
        self.assertEqual((self.root / "input.txt").read_text(), "source")

    def test_dependency_output_edit_blocks_downstream(self):
        self.check()
        stage = dict(
            name="next",
            kind="review",
            requires=["gate"],
            inputs=["report.json"],
            outputs=["preview.txt"],
            command=[
                "{python}",
                "-c",
                'from pathlib import Path; Path("preview.txt").write_text("view")',
            ],
        )
        self.project.stages["next"] = stage
        self.project.run_stage("next")
        (self.root / "report.json").write_text('{"ok":true,"edit":1}')
        self.assertFalse(self.project.fresh("next")[0])
        with self.assertRaises(WorkflowError):
            self.project.run_stage("next")


class ElectronicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.design = {
            "board": {"h": 10, "holes": [[1, 1]]},
            "frame": {"x": 0, "y": 10},
            "parts": [
                {
                    "ref": "U1",
                    "pins": {"1": "GND", "2": None},
                    "at": [2, 3],
                    "rot": 270,
                    "side": "B",
                    "lcsc": "C1",
                }
            ],
        }
        self.board = self.root / "test.kicad_pcb"
        self.board.write_text("""(kicad_pcb
          (footprint "fixture" (property "Reference" "U1") (at 2 7 270) (layer "B.Cu")
            (pad "1" smd rect (net "GND")) (pad "1" smd rect (net "GND"))
            (pad "2" smd rect))
          (footprint "mount" (property "Reference" "") (at 1 9)))""")
        self.net = self.root / "test.xml"
        self.net.write_text(
            '<export><nets><net name="GND"><node ref="U1" pin="1"/></net></nets></export>'
        )
        self.bom = self.root / "bom.csv"
        self.cpl = self.root / "cpl.csv"
        self.bom.write_text("Designator,LCSC Part #\nU1,C1\n")
        self.cpl.write_text(
            "Designator,Mid X,Mid Y,Layer,Rotation\nU1,2,3,Bottom,270\n"
        )

    def test_rotated_bottom_and_duplicate_same_net_pads_pass(self):
        self.assertTrue(circuit_contract(self.board, self.net, self.design)["ok"])
        self.assertTrue(assembly_contract(self.design, self.bom, self.cpl)["ok"])

    def test_swapped_pin_or_conflicting_duplicate_pad_fails(self):
        self.board.write_text(
            self.board.read_text().replace('(net "GND")', '(net "5V")', 1)
        )
        report = circuit_contract(self.board, self.net, self.design)
        self.assertFalse(report["ok"])
        self.assertTrue(any("duplicate pads" in x for x in report["failures"]))

    def test_missing_physical_nc_pad_fails(self):
        self.board.write_text(self.board.read_text().replace('(pad "2" smd rect)', ""))
        self.assertFalse(circuit_contract(self.board, self.net, self.design)["ok"])

    def test_changed_value_or_undeclared_pad_fails(self):
        self.design["parts"][0]["value"] = "changed"
        self.assertFalse(circuit_contract(self.board,self.net,self.design)["ok"])
        del self.design["parts"][0]["value"]
        self.board.write_text(self.board.read_text().replace('(pad "2" smd rect)', '(pad "2" smd rect) (pad "3" smd rect)'))
        self.assertFalse(circuit_contract(self.board,self.net,self.design)["ok"])

    def test_bad_supplier_rotation_position_side_and_duplicate_fail(self):
        for row in [
            "U1,2,3,Top,270",
            "U1,2,3,Bottom,-90",
            "U1,3,3,Bottom,270",
            "U1,2,3,Bottom,0",
            "U1,nan,3,Bottom,270",
            "U1,2,3,Bottom,270\nU1,2,3,Bottom,270",
        ]:
            self.cpl.write_text("Designator,Mid X,Mid Y,Layer,Rotation\n" + row + "\n")
            self.assertFalse(
                assembly_contract(self.design, self.bom, self.cpl)["ok"], row
            )
        self.bom.write_text("Designator,LCSC Part #\nU1,C2\n")
        self.assertFalse(assembly_contract(self.design, self.bom, self.cpl)["ok"])


class SchematicTests(unittest.TestCase):
    def test_inheritance_and_cycle(self):
        from schematic import load_symbol
        from kicad_sexpr import children

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.kicad_sym"
            path.write_text(
                '(kicad_symbol_lib (symbol "Base" (property "Value" "base") (symbol "Base_1_1" (pin input line))) (symbol "Derived" (extends "Base") (property "Value" "derived")))'
            )
            sym = load_symbol(path, "Derived", "Test:Derived")
            self.assertEqual(str(sym[1]), "Test:Derived")
            self.assertEqual(children(sym, "property")[0][2], "derived")
            self.assertEqual(children(sym, "symbol")[0][1], "Derived_1_1")
            path.write_text(
                '(kicad_symbol_lib (symbol "A" (extends "B")) (symbol "B" (extends "A")))'
            )
            with self.assertRaises(ValueError):
                load_symbol(path, "A", "Test:A")


if __name__ == "__main__":
    unittest.main()
