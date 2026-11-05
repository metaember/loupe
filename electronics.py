"""Native KiCad verification and assembly checks, independent of project generators."""

import csv
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from kicad_sexpr import child, children, parse


def circuit_contract(board_path, netlist_path, design):
    """Compare explicit pin intent with separately exported schematic and native PCB.

    frame is the native KiCad position of the project's lower-left origin, in mm.
    Repeated physical pads for one pin must agree. Unnamed NPTH mounts are separate.
    """
    errors = []
    snets = {}
    tree = ET.parse(netlist_path)
    for net in tree.findall("./nets/net"):
        for node in net.findall("node"):
            key = (node.get("ref"), node.get("pin"))
            if key in snets and snets[key] != net.get("name"):
                errors.append(f"schematic {key}: conflicting duplicate pin")
            snets[key] = net.get("name")
    board = parse(Path(board_path).read_text())
    bnets, mounts, refs = {}, [], []
    intended = {p["ref"]: p for p in design["parts"]}
    frame = design.get("frame", {"x": 50, "y": 50 + design["board"]["h"]})
    net_names = {str(n[1]): str(n[2]) for n in children(board, "net") if len(n) > 2}
    for fp in children(board, "footprint"):
        props = {str(p[1]): p[2] for p in children(fp, "property")}
        ref = str(props.get("Reference", ""))
        if ref:
            refs.append(ref)
            part = intended.get(ref, {})
            if "value" in part and props.get("Value") != part["value"]:
                errors.append(f"PCB {ref}: value differs from design")
            if "footprint" in part and str(fp[1]).rpartition(":")[2] != part["footprint"].rpartition(":")[2]:
                errors.append(f"PCB {ref}: footprint differs from design")
        else:
            at = child(fp, "at")
            mounts.append(
                [
                    round(float(at[1]) - frame["x"], 4),
                    round(frame["y"] - float(at[2]), 4),
                ]
            )
        for pad in children(fp, "pad"):
            if not str(pad[1]):
                continue
            n = child(pad, "net")
            net = (
                (str(n[-1]) if len(n) > 2 else net_names.get(str(n[1]), str(n[1])))
                if n
                else None
            )
            key = (ref, str(pad[1]))
            if key in bnets and bnets[key] != net:
                errors.append(f"PCB {key}: duplicate pads have different nets")
            bnets[key] = net
    if len(refs) != len(set(refs)):
        errors.append("PCB has duplicate component references")
    expected_refs = {p["ref"] for p in design["parts"]}
    if len(expected_refs) != len(design["parts"]):
        errors.append("design has duplicate component references")
    if set(refs) != expected_refs:
        errors.append(
            f"PCB component membership differs: {sorted(set(refs) ^ expected_refs)}"
        )
    count = 0
    for part in design["parts"]:
        for pin, expected in part["pins"].items():
            count += 1
            key = (part["ref"], str(pin))
            for source, actual in [
                ("schematic", snets.get(key)),
                ("PCB", bnets.get(key)),
            ]:
                # NC/mechanical pads can be omitted by the schematic netlist, but
                # the physical pad must exist on the board.
                if key not in (snets if source == "schematic" else bnets) and (
                    source == "PCB" or expected is not None
                ):
                    errors.append(f"{source} {key}: missing pin")
                elif expected is None:
                    if actual and not actual.startswith("unconnected-"):
                        errors.append(f"{source} {key}: expected NC, got {actual}")
                elif actual != expected:
                    errors.append(f"{source} {key}: expected {expected}, got {actual}")
    expected_pins = {(p["ref"],str(pin)) for p in design["parts"] for pin in p["pins"]}
    unexpected = set(bnets)-expected_pins
    if unexpected:
        errors.append(f"PCB has undeclared numbered pads: {sorted(unexpected)}")
    expected_mounts = design["board"].get("holes")
    if expected_mounts is not None and sorted(mounts) != sorted(
        [list(p) for p in expected_mounts]
    ):
        errors.append(f"mount positions differ: {mounts}")
    return dict(
        ok=not errors,
        checked_pin_contracts=count,
        parts=len(refs),
        mounts=mounts,
        failures=errors,
    )


def assembly_contract(design, bom_path, cpl_path):
    """JLC-format membership/identity checks. No supplier rotation-offset assumption."""
    parts = {
        p["ref"]: p
        for p in design["parts"]
        if p.get("assembly", bool(p.get("lcsc"))) and not p.get("dnp")
    }
    with Path(bom_path).open(newline="") as f:
        bom = list(csv.DictReader(f))
    with Path(cpl_path).open(newline="") as f:
        cpl = list(csv.DictReader(f))
    errors, bom_refs = [], []
    for row in bom:
        for ref in [v.strip() for v in row["Designator"].split(",")]:
            bom_refs.append(ref)
            if ref in parts and row["LCSC Part #"] != parts[ref].get("lcsc"):
                errors.append(f"{ref}: BOM supplier ID mismatch")
            if ref in parts and "Comment" in row and row["Comment"] != parts[ref].get("value"):
                errors.append(f"{ref}: BOM value mismatch")
            if ref in parts and "Footprint" in row and row["Footprint"] != parts[ref].get("footprint", "").rpartition(":")[2]:
                errors.append(f"{ref}: BOM footprint mismatch")
    for label, refs in [("BOM", bom_refs), ("CPL", [r["Designator"] for r in cpl])]:
        duplicate = [ref for ref, n in Counter(refs).items() if n != 1]
        if duplicate or set(refs) != set(parts):
            errors.append(
                f"{label} membership differs: duplicates={duplicate}, missing={sorted(set(parts) - set(refs))}, extra={sorted(set(refs) - set(parts))}"
            )
    for row in cpl:
        ref = row["Designator"]
        try:
            xy = [float(row["Mid X"]), float(row["Mid Y"])]
            rot = float(row["Rotation"])
            if not all(math.isfinite(v) for v in [*xy, rot]) or not 0 <= rot < 360:
                errors.append(f"{ref}: invalid placement coordinate/rotation")
            part = parts.get(ref)
            if part:
                if any(
                    abs(a - b) > 0.0011
                    for a, b in zip(xy, part.get("at", part.get("xy", [])))
                ):
                    errors.append(f"{ref}: CPL coordinates differ from design")
                if abs((rot - float(part.get("rot", 0)) + 180) % 360 - 180) > 0.001:
                    errors.append(f"{ref}: CPL angle differs from design")
                side = "Bottom" if part.get("side", "F") == "B" else "Top"
                if row["Layer"] != side:
                    errors.append(f"{ref}: CPL side differs from design")
        except (ValueError, KeyError):
            errors.append(f"{ref}: invalid CPL row")
    return dict(ok=not errors, placements=len(cpl), bom_lines=len(bom), failures=errors)


def checked_command(command, report_path, policy):
    """Run a native checker and validate its report schema, not stdout text."""
    report_path = Path(report_path)
    report_path.unlink(missing_ok=True)
    result = subprocess.run([str(v) for v in command], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            f"checker exited {result.returncode}: {result.stderr or result.stdout}"
        )
    report = json.loads(report_path.read_text())
    if policy == "erc":
        failed = any(s["violations"] for s in report["sheets"])
    elif policy == "drc":
        failed = bool(report["violations"] or report["unconnected_items"])
    elif policy == "ok":
        failed = report["ok"] is not True
    else:
        raise ValueError(f"unknown report policy: {policy}")
    if failed:
        raise RuntimeError(f"failed {policy}: {report_path}")
    return report


def native_circuit_check(board, schematic, design, out):
    """Fresh native ERC/DRC/netlist, followed by pin parity. Warnings are fatal."""
    from pcb import kicad_cli

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cli = str(kicad_cli())
    checked_command(
        [
            cli,
            "sch",
            "erc",
            str(schematic),
            "--format",
            "json",
            "--exit-code-violations",
            "-o",
            str(out / "erc.json"),
        ],
        out / "erc.json",
        "erc",
    )
    netlist = out / "schematic.net.xml"
    netlist.unlink(missing_ok=True)
    subprocess.run(
        [
            cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            str(netlist),
            str(schematic),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    checked_command(
        [
            cli,
            "pcb",
            "drc",
            str(board),
            "--format",
            "json",
            "--exit-code-violations",
            "-o",
            str(out / "drc.json"),
        ],
        out / "drc.json",
        "drc",
    )
    return circuit_contract(board, netlist, design)
