#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Deterministic PCB checks — headless KiCad DRC as a hard gate.

Runs kicad-cli DRC on a .kicad_pcb (design rules were baked in from the fab
profile by pcb.py, so this enforces the fab's real capabilities), plus the
netless-pad check from the build report. Emits JSON + human summary; exits
nonzero on any error-severity violation, unconnected item, or netless pad —
wire it after pcb.py save() so a broken board kills the build.

Usage:
  uv run pcbcheck.py out/blinky.kicad_pcb
  uv run pcbcheck.py out/blinky.kicad_pcb --strict     # warnings fatal too
  uv run pcbcheck.py out/blinky.kicad_pcb --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pcb import kicad_cli  # noqa: E402  (zero-dep sibling)


def run_drc(board: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        rpt = Path(td) / "drc.json"
        r = subprocess.run(
            [str(kicad_cli()), "pcb", "drc", str(board), "--format", "json",
             "--output", str(rpt), "--severity-all", "--units", "mm",
             "--all-track-errors"],
            capture_output=True, text=True)
        if not rpt.exists():
            sys.exit(f"kicad-cli drc failed:\n{r.stdout}{r.stderr}")
        return json.loads(rpt.read_text())


def fmt_violation(v: dict) -> str:
    loc = ""
    for it in v.get("items", []):
        p = it.get("pos")
        if p:
            loc = f" @({p['x']:.2f},{p['y']:.2f})"
            break
    return f"[{v['severity']}] {v['type']}: {v['description']}{loc}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--strict", action="store_true", help="warnings are fatal too")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    drc = run_drc(args.board)
    violations = drc.get("violations", [])
    unconnected = drc.get("unconnected_items", [])
    errors = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]

    # netless pads from the pcb.py build report, if present
    netless: list[str] = []
    br = args.board.with_name(args.board.stem + ".buildreport.json")
    if br.exists():
        netless = json.loads(br.read_text()).get("netless_pads", [])

    failures = [fmt_violation(v) for v in errors]
    failures += [f"[unrouted] {fmt_violation(u)}" for u in unconnected]
    failures += [f"[netless] pad {p} has no net (declare with net() or nc())" for p in netless]
    if args.strict:
        failures += [fmt_violation(v) for v in warnings]

    report = {
        "board": str(args.board),
        "errors": len(errors), "warnings": len(warnings),
        "unconnected": len(unconnected), "netless_pads": netless,
        "violations": violations, "unconnected_items": unconnected,
        "failures": failures, "ok": not failures,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{args.board.name}: {len(errors)} errors · {len(warnings)} warnings"
              f" · {len(unconnected)} unrouted · {len(netless)} netless pads")
        for v in warnings:
            print("  " + fmt_violation(v))
        if failures:
            print("FAIL:\n  " + "\n  ".join(failures))
        else:
            print("PASS")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
