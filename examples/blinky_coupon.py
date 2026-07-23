#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Example board: a 20×15mm blinky coupon.

Power in on a 2-pin header, through a 1k resistor into an LED, ground
returned through a via into a B.Cu pour. Exercises every pcb.py feature:
SMD + THT parts, pad-referenced routing, a via, a zone, an NPTH mounting
hole, silkscreen. LCSC numbers flow to the JLCPCB BOM.

  uv run examples/blinky_coupon.py           # -> examples/out/blinky.kicad_pcb
  uv run pcbcheck.py examples/out/blinky.kicad_pcb
  uv run pcbsheet.py examples/out/blinky.kicad_pcb
  uv run pcbfab.py  examples/out/blinky.kicad_pcb --mesh
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb import Board  # noqa: E402

b = Board("blinky", w=20, h=15, corner_r=2)

# parts — origin lower-left, +Y up, same frame a build123d case would use
b.part("D1", "LED_SMD:LED_0603_1608Metric", at=(5, 7.5), value="LED red",
       lcsc="C2286")
b.part("R1", "Resistor_SMD:R_0603_1608Metric", at=(10.5, 7.5), value="1k",
       lcsc="C21190")
b.part("J1", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
       at=(16.5, 8.8), value="PWR")

# nets — the board script IS the netlist
b.net("VCC", ("J1", 1), ("R1", 2))
b.net("LED_A", ("R1", 1), ("D1", 2))
b.net("GND", ("J1", 2), ("D1", 1))

# routing — explicit polylines, points are pads or absolute mm
b.route("VCC", [("J1", 1), (13, 7.5), ("R1", 2)], width=0.3)
b.route("LED_A", [("R1", 1), ("D1", 2)])
b.route("GND", [("D1", 1), (3, 7.5)])
b.via((3, 7.5), net="GND")            # down into the pour
b.zone("GND", layer="B.Cu")           # J1.2 (THT) lands in it directly

b.hole(2.5, 12.5, d=2.2)              # M2 mounting hole
b.silk("loupe blinky", 9.5, 12.5, size=0.9)

b.save(Path(__file__).parent / "out")
