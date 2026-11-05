#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local electronics/CAD project workflow. Run `uv run loupe.py --help`."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from workflow import Project, WorkflowError, atomic_json, tool_versions


def doctor():
    from pcb import footprint_dir, kicad_python

    tools = tool_versions()
    code = (
        "import wx; wx.DisableAsserts(); import pcbnew; print(pcbnew.GetBuildVersion())"
    )
    result = subprocess.run(
        [str(kicad_python()), "-c", code], capture_output=True, text=True, timeout=30
    )
    if result.returncode or not footprint_dir().is_dir():
        raise WorkflowError(f"KiCad Python/libraries unavailable: {result.stderr}")
    tools["pcbnew"] = result.stdout.strip()
    if not tools["kicad"]["version"].startswith("10."):
        raise WorkflowError(
            "PCB backend qualified on KiCad 10 only; other majors require qualification"
        )
    tools["java"] = shutil.which("java")
    tools["routing"] = "optional: provide an official Freerouting 2.5.0 JAR to route"
    return dict(ok=True, tools=tools)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=".", help="project directory or loupe.json")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    for action in ["build", "check", "review"]:
        sub.add_parser(action).add_argument("--force", action="store_true")
    sub.add_parser("release").add_argument("--target", default="jlcpcb")
    circuit = sub.add_parser("circuit-check")
    for opt in ["board", "design", "output"]:
        circuit.add_argument("--" + opt, required=True)
    source = circuit.add_mutually_exclusive_group(required=True)
    source.add_argument("--netlist")
    source.add_argument("--schematic")
    assembly = sub.add_parser("assembly-check")
    for opt in ["bom", "cpl", "design", "output"]:
        assembly.add_argument("--" + opt, required=True)
    route = sub.add_parser(
        "route", help="route into a separate new board; source remains untouched"
    )
    route.add_argument("board")
    route.add_argument("--output", required=True)
    route.add_argument("--jar", required=True)
    route.add_argument("--jar-sha256", required=True)
    args = ap.parse_args()
    try:
        if args.action == "doctor":
            report = doctor()
        elif args.action == "route":
            from routing import route_board

            report = route_board(args.board, args.output, args.jar, args.jar_sha256)
        elif args.action in ["circuit-check", "assembly-check"]:
            from electronics import (
                assembly_contract,
                circuit_contract,
                native_circuit_check,
            )

            design = json.loads(Path(args.design).read_text())
            if args.action == "circuit-check":
                report = (
                    native_circuit_check(
                        args.board, args.schematic, design, Path(args.output).parent
                    )
                    if args.schematic
                    else circuit_contract(args.board, args.netlist, design)
                )
            else:
                report = assembly_contract(design, args.bom, args.cpl)
            atomic_json(args.output, report)
            if not report["ok"]:
                print(json.dumps(report, indent=2))
                return 1
        else:
            project = Project(args.project)
            if args.action == "status":
                report = project.status()
            elif args.action == "release":
                report = project.release(args.target)
            else:
                project.run(args.action, args.force)
                report = {"ok": True, "action": args.action}
                if args.action == "review":
                    report["open"] = str(project.path(project.config["review"]))
        print(json.dumps(report, indent=2))
        return 0
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"loupe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
