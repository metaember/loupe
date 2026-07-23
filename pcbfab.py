#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["build123d"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Fab outputs — gerbers/drill zip, JLCPCB BOM + CPL, STEP, and viewer meshes.

Turns a checked .kicad_pcb into everything a fab run needs:
  <name>_gerbers.zip   gerber + excellon drill, JLCPCB-ready
  <name>_bom.csv       Comment,Designator,Footprint,LCSC Part # (grouped)
  <name>_cpl.csv       Designator,Mid X,Mid Y,Layer,Rotation (JLC pick&place)
  <name>.step          full 3D board incl. component models
  <name>.stl           (--mesh) tessellated board for viewer.py / sheet.py /
                       check.py — the PCB becomes one more part in the case
                       assembly, same coordinate frame as your build123d model.

BOM/CPL need the .pcbspec.json written by pcb.py next to the board (that's
where values + LCSC part numbers live); gerbers/STEP work on any .kicad_pcb.

Usage:
  uv run pcbfab.py out/blinky.kicad_pcb                # gerbers + bom + cpl + step
  uv run pcbfab.py out/blinky.kicad_pcb --mesh         # + STL for the 3D pipeline
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pcb import kicad_cli  # noqa: E402  (zero-dep sibling)

GERBER_LAYERS = "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"


def run(cmd: list) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(str(c) for c in cmd)}\n{r.stdout}{r.stderr}")
    return r


def gerbers(board: Path, out_zip: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        gd = Path(td) / "gerbers"
        gd.mkdir()
        run([kicad_cli(), "pcb", "export", "gerbers", board, "-o", f"{gd}/",
             "--layers", GERBER_LAYERS])
        run([kicad_cli(), "pcb", "export", "drill", board, "-o", f"{gd}/",
             "--format", "excellon", "--excellon-units", "mm"])
        shutil.make_archive(str(out_zip.with_suffix("")), "zip", gd)


def bom_cpl(board: Path, spec: dict, out_bom: Path, out_cpl: Path) -> tuple[int, int]:
    # positions from kicad-cli (authoritative, drill-origin = math origin)
    with tempfile.TemporaryDirectory() as td:
        pos = Path(td) / "pos.csv"
        run([kicad_cli(), "pcb", "export", "pos", board, "-o", pos,
             "--format", "csv", "--units", "mm", "--side", "both",
             "--use-drill-file-origin"])
        rows = list(csv.DictReader(pos.open()))
    parts = {p["ref"]: p for p in spec["parts"]}

    placed = 0
    with out_cpl.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for r in rows:
            ref = r.get("Ref") or r.get("ref")
            if ref not in parts or parts[ref].get("dnp"):
                continue
            w.writerow([ref, f"{float(r['PosX']):.3f}", f"{float(r['PosY']):.3f}",
                        "Top" if r["Side"] == "top" else "Bottom",
                        f"{float(r['Rot']):.1f}"])
            placed += 1

    groups: dict[tuple, list[str]] = {}
    for p in spec["parts"]:
        if p.get("dnp"):
            continue
        fp = p["footprint"].rpartition(":")[2]
        groups.setdefault((p["value"] or fp, fp, p.get("lcsc", "")), []).append(p["ref"])
    with out_bom.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for (val, fp, lcsc), refs in sorted(groups.items()):
            w.writerow([val, ",".join(sorted(refs)), fp, lcsc])
    return placed, len(groups)


def step(board: Path, out: Path) -> None:
    run([kicad_cli(), "pcb", "export", "step", board, "-o", out,
         "--subst-models", "--drill-origin"])


def mesh(step_path: Path, out_stl: Path) -> None:
    from build123d import export_stl, import_step  # heavy; only on --mesh
    shape = import_step(str(step_path))
    export_stl(shape, str(out_stl))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--mesh", action="store_true",
                    help="also tessellate STEP -> STL for viewer.py/sheet.py/check.py")
    ap.add_argument("--no-step", action="store_true")
    args = ap.parse_args()
    stem, outd = args.board.stem, args.board.parent

    gz = outd / f"{stem}_gerbers.zip"
    gerbers(args.board, gz)
    print(f"{gz}")

    specp = args.board.with_name(stem + ".pcbspec.json")
    if specp.exists():
        spec = json.loads(specp.read_text())
        n, g = bom_cpl(args.board, spec, outd / f"{stem}_bom.csv", outd / f"{stem}_cpl.csv")
        print(f"{outd / f'{stem}_bom.csv'}  ({g} line items)")
        print(f"{outd / f'{stem}_cpl.csv'}  ({n} placements)")
    else:
        print(f"note: {specp.name} not found — skipping BOM/CPL")

    if not args.no_step:
        sp = outd / f"{stem}.step"
        step(args.board, sp)
        print(f"{sp}")
        if args.mesh:
            st = outd / f"{stem}.stl"
            mesh(sp, st)
            print(f"{st}  (feed to viewer.py / sheet.py / check.py)")


if __name__ == "__main__":
    main()
