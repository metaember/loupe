#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Parametric PCB builder — define a board in Python, get a real .kicad_pcb.

The board script IS the netlist: declare parts (KiCad footprints), connect
pads into nets, place everything with computed coordinates, route with
explicit polylines, pour zones — then `save()` materializes a .kicad_pcb by
driving KiCad's bundled Python (pcbnew) under the hood. Downstream, the
kicad-cli-based siblings take over: pcbcheck.py (DRC gate), pcbsheet.py
(renders), pcbfab.py (gerbers/BOM/CPL/STEP).

Coordinates are math-style: origin at the board's lower-left, +Y UP — the
same frame as a build123d case model. The driver flips into KiCad's y-down
world; you never think about it.

Usage (as a library, from a board script):
  import sys; sys.path.insert(0, "/path/to/loupe")
  from pcb import Board
  b = Board("blinky", w=20, h=15, corner_r=2)
  b.part("D1", "LED_SMD:LED_0603_1608Metric", at=(6, 5), value="LED")
  b.net("GND", ("J1", 2), ("D1", 1))
  b.route("GND", [("J1", 2), ("D1", 1)])         # pad refs or (x, y) tuples
  b.zone("GND", layer="B.Cu")
  b.save("out")                                   # -> out/blinky.kicad_pcb

Internal driver mode (run automatically under KiCad's Python):
  <kicad-python> pcb.py --driver spec.json out.kicad_pcb
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ KiCad discovery

KICAD_APP_CANDIDATES = [
    "/Applications/KiCad/KiCad.app",
    os.path.expanduser("~/Applications/KiCad/KiCad.app"),
]


def kicad_app() -> Path:
    env = os.environ.get("LOUPE_KICAD")
    for c in ([env] if env else []) + KICAD_APP_CANDIDATES:
        if c and Path(c).exists():
            return Path(c)
    sys.exit("KiCad not found (looked in /Applications/KiCad). Install it or set LOUPE_KICAD=/path/to/KiCad.app")


def kicad_python() -> Path:
    fw = kicad_app() / "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
    if fw.exists():
        return fw
    sys.exit(f"KiCad bundled python not found at {fw}")


def kicad_cli() -> Path:
    cli = kicad_app() / "Contents/MacOS/kicad-cli"
    if cli.exists():
        return cli
    sys.exit(f"kicad-cli not found at {cli}")


def footprint_dir() -> Path:
    return kicad_app() / "Contents/SharedSupport/footprints"


# ------------------------------------------------------------------ fab profiles
# DRC minimums are set INTO the board file so kicad-cli drc enforces the fab's
# real capabilities, not KiCad defaults.  Units: mm.

FAB_PROFILES = {
    "jlcpcb": {  # 2-layer, 1oz, standard service
        "clearance": 0.15,        # default netclass clearance (JLC min 0.127)
        "track_width": 0.25,      # default track width
        "min_track": 0.127,       # 5 mil
        "min_clearance": 0.127,
        "via_dia": 0.6, "via_drill": 0.3,
        "min_via_dia": 0.45, "min_through_drill": 0.3,
        "copper_edge_clearance": 0.3,
        "min_annular": 0.13,
    },
}


# ------------------------------------------------------------------ the model

PadRef = tuple  # ("REF", pad_number)  |  (x, y) floats


@dataclass
class _Part:
    ref: str
    footprint: str          # "LibName:FootprintName" or "/abs/path.pretty:Name"
    at: tuple[float, float]
    rot: float = 0.0
    side: str = "F"         # F | B
    value: str = ""
    lcsc: str = ""          # JLC assembly part number (optional, flows to BOM)
    dnp: bool = False


class Board:
    """Parametric PCB. All dims mm, origin lower-left, +Y up, rotations CCW."""

    def __init__(self, name: str, w: float, h: float, corner_r: float = 0.0,
                 layers: int = 2, fab: str = "jlcpcb"):
        if layers != 2:
            raise ValueError("v1 supports 2-layer boards")
        self.name, self.w, self.h, self.corner_r = name, w, h, corner_r
        self.layers, self.fab = layers, fab
        self.parts: list[_Part] = []
        self.nets: dict[str, list[list]] = {}       # name -> [[ref, pad], ...]
        self.tracks: list[dict] = []
        self.vias: list[dict] = []
        self.zones: list[dict] = []
        self.holes: list[dict] = []                 # NPTH mounting holes
        self.texts: list[dict] = []
        self.nc_pads: list[list] = []               # explicitly not-connected

    # -------------------------------------------------- construction API

    def part(self, ref: str, footprint: str, at: tuple[float, float],
             rot: float = 0.0, side: str = "F", value: str = "",
             lcsc: str = "", dnp: bool = False) -> None:
        if any(p.ref == ref for p in self.parts):
            raise ValueError(f"duplicate ref {ref}")
        self.parts.append(_Part(ref, footprint, at, rot, side, value, lcsc, dnp))

    def net(self, name: str, *pads: PadRef) -> None:
        self.nets.setdefault(name, []).extend([list(p) for p in pads])

    def nc(self, ref: str, *pad_numbers) -> None:
        """Mark pads as intentionally unconnected (silences the netless-pad check)."""
        self.nc_pads.extend([[ref, str(p)] for p in pad_numbers])

    def route(self, net: str, points: list[PadRef], layer: str = "F.Cu",
              width: float | None = None) -> None:
        """Polyline of track segments. Points are (x,y) mm or ("REF", pad)."""
        if net not in self.nets:
            raise ValueError(f"route on undeclared net {net!r}")
        self.tracks.append({"net": net, "points": [list(p) for p in points],
                            "layer": layer, "width": width})

    def via(self, at: PadRef, net: str, dia: float | None = None,
            drill: float | None = None) -> None:
        self.vias.append({"at": list(at), "net": net, "dia": dia, "drill": drill})

    def zone(self, net: str, layer: str, margin: float = 0.5,
             outline: list[tuple[float, float]] | None = None) -> None:
        """Copper pour. Default outline = board rect inset by `margin`."""
        if outline is None:
            m = margin
            outline = [(m, m), (self.w - m, m), (self.w - m, self.h - m), (m, self.h - m)]
        self.zones.append({"net": net, "layer": layer,
                           "outline": [list(p) for p in outline]})

    def hole(self, x: float, y: float, d: float) -> None:
        """Plain NPTH mounting hole, diameter d."""
        self.holes.append({"at": [x, y], "d": d})

    def silk(self, text: str, x: float, y: float, layer: str = "F.SilkS",
             size: float = 1.0, rot: float = 0.0) -> None:
        self.texts.append({"text": text, "at": [x, y], "layer": layer,
                           "size": size, "rot": rot})

    # -------------------------------------------------- serialization + build

    def spec(self) -> dict:
        return {
            "name": self.name, "w": self.w, "h": self.h,
            "corner_r": self.corner_r, "layers": self.layers,
            "fab": FAB_PROFILES[self.fab],
            "footprint_dir": str(footprint_dir()),
            "parts": [vars(p) | {"at": list(p.at)} for p in self.parts],
            "nets": self.nets, "tracks": self.tracks, "vias": self.vias,
            "zones": self.zones, "holes": self.holes, "texts": self.texts,
            "nc_pads": self.nc_pads,
        }

    def save(self, out_dir: str | Path) -> Path:
        out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
        spec_path = out / f"{self.name}.pcbspec.json"
        board_path = out / f"{self.name}.kicad_pcb"
        spec_path.write_text(json.dumps(self.spec(), indent=1))
        r = subprocess.run(
            [str(kicad_python()), str(Path(__file__).resolve()), "--driver",
             str(spec_path), str(board_path)],
            capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            sys.exit(f"pcb driver FAILED for {self.name} (see above)")
        report = json.loads((out / f"{self.name}.buildreport.json").read_text())
        if report["netless_pads"]:
            print(f"⚠ netless pads (declare with net() or nc()): {report['netless_pads']}")
        print(f"{board_path}  ·  {len(self.parts)} parts · {len(self.nets)} nets"
              f" · {report['pad_count']} pads")
        return board_path


# ================================================================== DRIVER
# Everything below runs under KiCad's bundled Python (pcbnew importable).
# stdlib + pcbnew ONLY.  Math frame -> KiCad frame:  x_k = X0 + x,
# y_k = Y0 - y  (origin lower-left, +Y up -> KiCad y-down), rotation passes
# through unchanged (double flip: y-axis + screen sense).

X0, Y0_MARGIN = 50.0, 50.0  # board placed at 50mm from page corner


def _driver_main(spec_path: str, out_path: str) -> None:
    # pcbnew calls into wxWidgets internals; without this, asserts abort the
    # process. (No wx.App — that bounces a dock icon and isn't needed.)
    try:
        import wx
        wx.DisableAsserts()
    except Exception:
        pass
    import pcbnew  # noqa — only importable under KiCad python

    spec = json.loads(Path(spec_path).read_text())
    W, H = spec["w"], spec["h"]
    Y0 = Y0_MARGIN + H
    fab = spec["fab"]
    fpdir = Path(spec["footprint_dir"])

    def P(x, y):  # math mm -> KiCad VECTOR2I
        return pcbnew.VECTOR2I(pcbnew.FromMM(X0 + x), pcbnew.FromMM(Y0 - y))

    def IU(v):
        return pcbnew.FromMM(v)

    board = pcbnew.CreateEmptyBoard()

    # ---- design rules from fab profile
    ds = board.GetDesignSettings()
    ds.m_TrackMinWidth = IU(fab["min_track"])
    ds.m_MinClearance = IU(fab["min_clearance"])
    ds.m_ViasMinSize = IU(fab["min_via_dia"])
    ds.m_MinThroughDrill = IU(fab["min_through_drill"])
    ds.m_CopperEdgeClearance = IU(fab["copper_edge_clearance"])
    ds.m_ViasMinAnnularWidth = IU(fab["min_annular"])
    try:  # default netclass (API shape varies by version; best-effort)
        nc = ds.m_NetSettings.GetDefaultNetclass()
        nc.SetClearance(IU(fab["clearance"]))
        nc.SetTrackWidth(IU(fab["track_width"]))
        nc.SetViaDiameter(IU(fab["via_dia"]))
        nc.SetViaDrill(IU(fab["via_drill"]))
    except Exception as e:
        print(f"note: netclass defaults not set ({e}); DRC minimums still active")
    ds.SetAuxOrigin(P(0, 0))
    ds.SetGridOrigin(P(0, 0))

    # ---- board outline (rounded rect on Edge.Cuts)
    def edge_seg(a, b):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(P(*a)); s.SetEnd(P(*b))
        s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(IU(0.1))
        board.Add(s)

    def edge_arc(start, mid, end):
        # three-point arc — immune to the angle-sign conventions that produced
        # both scooped corners (270° long way) and unclosed outlines.
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_ARC)
        s.SetArcGeometry(P(*start), P(*mid), P(*end))
        s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(IU(0.1))
        board.Add(s)

    r = spec["corner_r"]
    if r <= 0:
        edge_seg((0, 0), (W, 0)); edge_seg((W, 0), (W, H))
        edge_seg((W, H), (0, H)); edge_seg((0, H), (0, 0))
    else:
        edge_seg((r, 0), (W - r, 0)); edge_seg((W, r), (W, H - r))
        edge_seg((W - r, H), (r, H)); edge_seg((0, H - r), (0, r))
        # corner arcs, each start/mid/end; k = r·(1 − 1/√2) for the midpoints
        k = r * (1 - 2 ** -0.5)
        edge_arc((W - r, 0), (W - k, k), (W, r))            # bottom-right
        edge_arc((W, H - r), (W - k, H - k), (W - r, H))    # top-right
        edge_arc((r, H), (k, H - k), (0, H - r))            # top-left
        edge_arc((0, r), (k, k), (r, 0))                    # bottom-left

    # ---- nets
    nets = {}
    for name in spec["nets"]:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        nets[name] = ni
    pad_net = {}  # (ref, pad#) -> net name
    for name, pads in spec["nets"].items():
        for ref, num in pads:
            pad_net[(ref, str(num))] = name

    # ---- footprints
    footprints = {}
    for part in spec["parts"]:
        libname, _, fpname = part["footprint"].rpartition(":")
        libpath = libname if libname.endswith(".pretty") else str(fpdir / f"{libname}.pretty")
        fp = pcbnew.FootprintLoad(libpath, fpname)
        if fp is None:
            sys.exit(f"footprint not found: {part['footprint']} (looked in {libpath})")
        fp.SetReference(part["ref"])
        fp.SetValue(part["value"] or fpname)
        board.Add(fp)
        if part["side"] == "B":
            fp.Flip(P(*part["at"]), False)
        fp.SetPosition(P(*part["at"]))
        fp.SetOrientationDegrees(part["rot"])
        if part.get("lcsc"):
            fp.SetProperty("LCSC", part["lcsc"]) if hasattr(fp, "SetProperty") else None
        if part.get("dnp"):
            fp.SetDNP(True)
        footprints[part["ref"]] = fp

    # assign nets to pads; find netless along the way
    nc_set = {(r, str(n)) for r, n in spec["nc_pads"]}
    netless, pad_count = [], 0
    pad_pos = {}  # (ref, pad#) -> math (x, y) for track endpoints
    for ref, fp in footprints.items():
        for pad in fp.Pads():
            num = pad.GetNumber()
            if not num:  # e.g. NPTH pads in mounting-hole footprints
                continue
            pad_count += 1
            c = pad.GetPosition()
            pad_pos[(ref, num)] = (pcbnew.ToMM(c.x) - X0, Y0 - pcbnew.ToMM(c.y))
            key = (ref, num)
            if key in pad_net:
                pad.SetNet(nets[pad_net[key]])
            elif key not in nc_set:
                netless.append(f"{ref}.{num}")

    def resolve(pt):
        """(x,y) floats -> as-is;  ["REF", pad] -> pad center."""
        if isinstance(pt[0], str):
            key = (pt[0], str(pt[1]))
            if key not in pad_pos:
                sys.exit(f"route references unknown pad {pt[0]}.{pt[1]}")
            return pad_pos[key]
        return (float(pt[0]), float(pt[1]))

    # ---- tracks
    ALIASES = {"F.SilkS": "F.Silkscreen", "B.SilkS": "B.Silkscreen",
               "F.CrtYd": "F.Courtyard", "B.CrtYd": "B.Courtyard",
               "F.Adhes": "F.Adhesive", "B.Adhes": "B.Adhesive"}

    def layer_id(name: str) -> int:
        # KiCad 10 GetLayerID returns -1 (UNDEFINED) for legacy short names
        # like F.SilkS — and would silently write an unparseable board.
        for cand in (name, ALIASES.get(name, name)):
            lid = board.GetLayerID(cand)
            if lid >= 0:
                return lid
        sys.exit(f"unknown layer {name!r}")
    for t in spec["tracks"]:
        pts = [resolve(p) for p in t["points"]]
        w = IU(t["width"] or fab["track_width"])
        for a, b in zip(pts, pts[1:]):
            tr = pcbnew.PCB_TRACK(board)
            tr.SetStart(P(*a)); tr.SetEnd(P(*b))
            tr.SetWidth(w); tr.SetLayer(layer_id(t["layer"]))
            tr.SetNet(nets[t["net"]])
            board.Add(tr)

    # ---- vias
    for v in spec["vias"]:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(P(*resolve(v["at"])))
        via.SetDrill(IU(v["drill"] or fab["via_drill"]))
        via.SetWidth(IU(v["dia"] or fab["via_dia"]))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(nets[v["net"]])
        board.Add(via)

    # ---- NPTH mounting holes (raw circular NPTH pad in a bare footprint)
    for h in spec["holes"]:
        fp = pcbnew.FOOTPRINT(board)
        fp.SetFPID(pcbnew.LIB_ID("", f"NPTH_{h['d']}mm"))
        fp.SetReference("")
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(IU(h["d"]), IU(h["d"])))
        pad.SetDrillSize(pcbnew.VECTOR2I(IU(h["d"]), IU(h["d"])))
        # an empty (layers) list is a parse error on reload — use the
        # canonical NPTH mask (F&B.Cu + both mask layers)
        pad.SetLayerSet(pcbnew.PAD.UnplatedHoleMask())
        fp.Add(pad)
        fp.SetPosition(P(*h["at"]))

    # ---- silkscreen text
    for tx in spec["texts"]:
        t = pcbnew.PCB_TEXT(board)
        t.SetText(tx["text"])
        t.SetLayer(layer_id(tx["layer"]))
        t.SetTextSize(pcbnew.VECTOR2I(IU(tx["size"]), IU(tx["size"])))
        t.SetTextThickness(IU(max(0.15, tx["size"] * 0.15)))
        t.SetPosition(P(*tx["at"]))
        t.SetTextAngleDegrees(tx["rot"])
        if tx["layer"].startswith("B."):
            t.SetMirrored(True)
        board.Add(t)

    # ---- zones (pour + fill)
    for z in spec["zones"]:
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer_id(z["layer"]))
        zone.SetNetCode(nets[z["net"]].GetNetCode())
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        zone.SetLocalClearance(IU(fab["clearance"]))
        zone.SetMinThickness(IU(fab["min_track"]))
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in z["outline"]:
            outline.Append(int(IU(X0 + x)), int(IU(Y0 - y)))
        board.Add(zone)

    pcbnew.SaveBoard(out_path, board)
    if spec["zones"]:
        # ZONE_FILLER segfaults on a CreateEmptyBoard() board (no project
        # context, KiCad 10.0.4) — filling a saved-then-reloaded board works.
        # (NB: don't rebind `board` — dropping the last ref to the original
        # BOARD runs its SWIG destructor and segfaults the filler.)
        board2 = pcbnew.LoadBoard(out_path)
        if board2 is None:
            sys.exit(f"reload of {out_path} failed — the written board does not parse")
        filler = pcbnew.ZONE_FILLER(board2)
        filler.Fill(board2.Zones())
        pcbnew.SaveBoard(out_path, board2)

    report = {"pad_count": pad_count, "netless_pads": netless,
              "pad_positions": {f"{r}.{n}": list(xy) for (r, n), xy in pad_pos.items()},
              "kicad_version": pcbnew.GetBuildVersion()}
    Path(out_path).with_name(Path(spec_path).name.replace(".pcbspec.json",
                                                          ".buildreport.json")) \
        .write_text(json.dumps(report, indent=1))
    print(f"driver: wrote {out_path} (KiCad {pcbnew.GetBuildVersion()})")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--driver":
        _driver_main(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--info":
        print(f"KiCad app:    {kicad_app()}")
        print(f"python:       {kicad_python()}")
        print(f"kicad-cli:    {kicad_cli()}")
        print(f"footprints:   {footprint_dir()}")
    else:
        print(__doc__)
