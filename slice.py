#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Headless Bambu Studio slicing: errors, print time, filament use — no GUI.

Wraps the Bambu Studio CLI (P1S profiles by default), slices one plate from
one or more model files, and reports a clean summary: success/error, predicted
print time, filament grams/meters (computed from extruded length x profile
density — the CLI leaves used_g at 0), spool cost, feature-time breakdown,
and any slicer warnings. Keeps the .gcode.3mf next to the inputs.

Usage:
  uv run slice.py part1.stl part2.stl
  uv run slice.py part.stl --process "0.12mm Fine @BBL X1C" --filament "Bambu PETG Basic @BBL P1S 0.4 nozzle"
  uv run slice.py --list-processes | --list-filaments
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
PROFILES = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")
DEFAULTS = {
    "machine": "Bambu Lab P1S 0.4 nozzle",
    "process": "0.20mm Standard @BBL X1C",
    "filament": "Bambu PLA Basic @BBL P1S 0.4 nozzle",
}
FILAMENT_DIA = 1.75  # mm


def profile_path(kind: str, name: str) -> Path:
    p = PROFILES / kind / f"{name}.json"
    if not p.exists():
        sys.exit(f"no {kind} profile '{name}' — try --list-{kind}s")
    return p


def filament_density(name: str) -> float:
    """Follow the profile's `inherits` chain until filament_density appears."""
    seen = 0
    while name and seen < 6:
        p = PROFILES / "filament" / f"{name}.json"
        if not p.exists():
            break
        data = json.loads(p.read_text())
        dens = data.get("filament_density")
        if dens:
            return float(dens[0] if isinstance(dens, list) else dens)
        name = data.get("inherits", "")
        seen += 1
    return 1.24  # PLA-ish fallback


def fmt_time(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}h {s % 3600 // 60}m {s % 60}s" if s >= 3600 else f"{s // 60}m {s % 60}s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--machine", default=DEFAULTS["machine"])
    ap.add_argument("--process", default=DEFAULTS["process"])
    ap.add_argument("--filament", default=DEFAULTS["filament"])
    ap.add_argument("--outdir", default=None, help="output dir (default: alongside first input)")
    ap.add_argument("--spool-cost", type=float, default=20.0, help="$/kg for cost estimate")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON summary")
    ap.add_argument("--list-processes", action="store_true")
    ap.add_argument("--list-filaments", action="store_true")
    ap.add_argument("--list-machines", action="store_true")
    args = ap.parse_args()

    for kind, on in (("process", args.list_processes), ("filament", args.list_filaments),
                     ("machine", args.list_machines)):
        if on:
            for f in sorted((PROFILES / kind).glob("*.json")):
                if "template" not in f.stem:
                    print(f.stem)
            return
    if not args.files:
        sys.exit("no input files")
    if not APP.exists():
        sys.exit(f"Bambu Studio not found at {APP}")

    inputs = [Path(f).resolve() for f in args.files]
    for f in inputs:
        if not f.exists():
            sys.exit(f"missing input: {f}")
    outdir = Path(args.outdir).resolve() if args.outdir else inputs[0].parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = inputs[0].stem if len(inputs) == 1 else f"{inputs[0].stem}_plate"

    settings = f"{profile_path('machine', args.machine)};{profile_path('process', args.process)}"
    with tempfile.TemporaryDirectory() as td:
        cmd = [str(APP), "--debug", "0",
               "--load-settings", settings,
               "--load-filaments", str(profile_path("filament", args.filament)),
               "--load-defaultfila",
               "--arrange", "1", "--orient", "0",
               "--slice", "0",
               "--export-3mf", f"{stem}.gcode.3mf",
               "--outputdir", td,
               *map(str, inputs)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        td = Path(td)
        result_file = td / "result.json"
        result = json.loads(result_file.read_text()) if result_file.exists() else {}

        ok = result.get("return_code", proc.returncode) == 0 and "sliced_plates" in result
        summary: dict = {
            "ok": ok,
            "error": None if ok else result.get("error_string", proc.stderr.strip()[-400:] or "unknown"),
            "machine": args.machine, "process": args.process, "filament": args.filament,
        }

        if ok:
            plate = result["sliced_plates"][0]
            gcode = td / "plate_1.gcode"
            head = gcode.read_text(errors="ignore")[:20000] if gcode.exists() else ""
            m = re.search(r"total filament length \[mm\] : ([\d.]+)", head)
            length_mm = float(m.group(1)) if m else 0.0
            vol_mm3 = length_mm * math.pi * (FILAMENT_DIA / 2) ** 2
            dens = filament_density(args.filament)
            grams = vol_mm3 / 1000 * dens
            feats = plate.get("feature_type_times", {})
            top = sorted(feats.items(), key=lambda kv: -kv[1])[:5]
            summary.update({
                "time_s": plate["total_predication"],
                "time": fmt_time(plate["total_predication"]),
                "filament_g": round(grams, 1),
                "filament_m": round(length_mm / 1000, 2),
                "cost_usd": round(grams / 1000 * args.spool_cost, 2),
                "objects": [{"name": o["name"],
                             "bbox_mm": [round(o["bbox"]["width"], 1),
                                         round(o["bbox"]["depth"], 1),
                                         round(o["bbox"]["height"], 1)]}
                            for o in plate.get("objects", [])],
                "warnings": plate.get("warning_message", "") or None,
                "top_features": {k: fmt_time(v) for k, v in top},
            })
            keep = outdir / f"{stem}.gcode.3mf"
            src = td / f"{stem}.gcode.3mf"
            if src.exists():
                keep.write_bytes(src.read_bytes())
                summary["gcode_3mf"] = str(keep)

    if args.json:
        print(json.dumps(summary, indent=2))
    elif summary["ok"]:
        print(f"OK  {summary['time']}  ·  {summary['filament_g']}g "
              f"({summary['filament_m']}m, ~${summary['cost_usd']})  ·  "
              f"{args.process} / {args.filament.split('@')[0].strip()}")
        for o in summary["objects"]:
            print(f"  - {o['name']}  {o['bbox_mm'][0]}×{o['bbox_mm'][1]}×{o['bbox_mm'][2]}mm")
        if summary["warnings"]:
            print(f"  ⚠ {summary['warnings']}")
        print(f"  → {summary.get('gcode_3mf', '(3mf not kept)')}")
    else:
        print(f"SLICE FAILED: {summary['error']}")
    sys.exit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
