#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Native integration fixture: rotated front/back pads, offset custom pad, NPTH.

python3 examples/route_fixture.py /tmp/fixture --jar official.jar --jar-sha256 HASH
Writes only the requested fixture directory. No project design files are changed.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def driver(filename):
    import wx

    wx.DisableAsserts()
    import pcbnew as pcb

    board = pcb.LoadBoard(filename)
    custom = next(f for f in board.GetFootprints() if f.GetReference() == "R1")
    pad = next(p for p in custom.Pads() if p.GetNumber() == "1")
    pad.SetShape(pcb.PAD_SHAPE_CUSTOM)
    pad.SetSize(pcb.VECTOR2I(pcb.FromMM(0.6), pcb.FromMM(0.6)))
    points = pcb.VECTOR_VECTOR2I()
    for x, y in [(-1.2, -0.4), (0.1, -0.4), (0.1, 0.4), (-1.2, 0.4)]:
        points.push_back(pcb.VECTOR2I(pcb.FromMM(x), pcb.FromMM(y)))
    pad.AddPrimitivePoly(pcb.F_Cu, points, 0, True)
    custom.SetFPID(pcb.LIB_ID())
    pcb.SaveBoard(filename, board)


def main():
    from pcb import Board, kicad_python
    from routing import route_board
    from kicad_sexpr import parse, children, child

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    ap.add_argument("--jar", required=True)
    ap.add_argument("--jar-sha256", required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    b = Board("route-fixture", w=30, h=20)
    b.part("R1", "Resistor_SMD:R_0603_1608Metric", at=(8, 10), rot=90)
    b.part("R2", "Resistor_SMD:R_0603_1608Metric", at=(22, 10), rot=270, side="B")
    b.net("A", ("R1", 1), ("R2", 1))
    b.net("GND", ("R1", 2), ("R2", 2))
    b.hole(15, 10, 2.4)
    source = b.save(args.output)
    subprocess.run([str(kicad_python()), __file__, "--driver", str(source)], check=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = args.output / "routed.kicad_pcb"
    report = route_board(source, output, args.jar, args.jar_sha256)
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise RuntimeError("source changed")

    def placements(path):
        return sorted(
            (
                next(p[2] for p in children(f, "property") if p[1] == "Reference"),
                child(f, "at"),
                child(f, "layer"),
            )
            for f in children(parse(path.read_text()), "footprint")
            if any(p[1:2] == ["Reference"] and p[2] for p in children(f, "property"))
        )

    if placements(source) != placements(output):
        raise RuntimeError("placement changed")
    report["source_preserved"] = True
    report["placements_preserved"] = True
    (args.output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--driver"]:
        driver(sys.argv[2])
    else:
        main()
