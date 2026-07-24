#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["build123d>=0.11", "pyyaml", "shapely"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""The case tray drafted in coupon_case.draft.yaml, built from that same file.

Note what this script does *not* contain: numbers. Every dimension is read from the
draft, so the drawing and the model cannot disagree — and `draftcheck.py --stl` can
assert the exported mesh still matches what was drafted.

Built in the draft's frame — outer corner at (0,0), +Y up — so a hole at (4.8, 14.8)
in the draft is at (4.8, 14.8) here. The as-built gate compares coordinates directly.

  uv run examples/coupon_case.py
  uv run draftcheck.py examples/coupon_case.draft.yaml --stl asm_case.stl
"""
import sys
from pathlib import Path

from build123d import (Align, BuildPart, BuildSketch, Circle, Location, Locations,
                       Mode, Plane, Rectangle, RectangleRounded, SlotCenterToCenter,
                       export_stl, extrude)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from draft import load  # noqa: E402

D = load(Path(__file__).parent / "coupon_case.draft.yaml").params()
MIN = (Align.MIN, Align.MIN)

with BuildPart() as case:
    # outer body, corner at the origin to match the draft's frame
    with BuildSketch() as body:
        RectangleRounded(D["case_w"], D["case_d"], 3, align=MIN)
    extrude(amount=D["case_h"])

    # hollow it out from the top, leaving the floor
    with BuildSketch(Plane.XY.offset(D["floor_t"])) as cavity:
        with Locations((D["wall"], D["wall"])):
            RectangleRounded(D["case_w"] - 2 * D["wall"],
                             D["case_d"] - 2 * D["wall"], 1.0, align=MIN)
    extrude(amount=D["case_h"] - D["floor_t"], mode=Mode.SUBTRACT)

    # openings through the floor — the features the draft dimensions
    with BuildSketch() as floor_holes:
        with Locations((D["m2_x"], D["m2_y"])):
            Circle(D["m2_clear_d"] / 2)
        with Locations((D["led_x"], D["led_y"])):
            Circle(D["led_win_d"] / 2)
        with Locations(Location((16.5, 4))):          # vent, drafted as a slot
            SlotCenterToCenter(7, 2.0)
    extrude(amount=D["floor_t"], mode=Mode.SUBTRACT)

out = Path("asm_case.stl")
export_stl(case.part, str(out), tolerance=0.001, angular_tolerance=0.05)
bb = case.part.bounding_box()
print(f"wrote {out}  bbox {bb.size.X:.3f} × {bb.size.Y:.3f} × {bb.size.Z:.3f}"
      f"  (drafted {D['case_w']:.3f} × {D['case_d']:.3f} × {D['case_h']:.3f})")
