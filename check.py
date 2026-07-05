#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "trimesh", "scipy", "rtree", "manifold3d"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Deterministic geometry checks for 3D-print designs (Gen-2 verifier).

Machine-checkable diagnostics that a render can't guarantee: 3D interference
volume between parts, minimum clearance between mating parts, watertightness,
and an overhang report. Emits JSON + human summary; exits nonzero when an
assertion fails — wire it after STL export so violations kill the build.

Usage:
  uv run check.py body.stl lid.stl                                 # report only
  uv run check.py body.stl lid.stl --interference-max 0.01         # assert no overlap
  uv run check.py body.stl lid.stl --clearance body:lid:0.15       # assert min gap
  uv run check.py part.stl --min-wall 1.2 --overhang 45
Part names = file stems.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import trimesh

SAMPLES = 4000  # surface sample points for clearance / wall checks


def load(paths: list[str]) -> dict[str, trimesh.Trimesh]:
    out = {}
    for p in paths:
        m = trimesh.load(p, force="mesh", process=True)  # merge verts: STL soup -> volume
        if not m.is_volume:
            trimesh.repair.fix_winding(m)
            trimesh.repair.fix_normals(m)
        out[Path(p).stem] = m
    return out


def interference(a: trimesh.Trimesh, b: trimesh.Trimesh) -> float:
    """Overlap volume in mm³ (exact boolean via manifold, sampling fallback)."""
    try:
        inter = trimesh.boolean.intersection([a, b], engine="manifold")
        return float(inter.volume) if inter and not inter.is_empty else 0.0
    except Exception:
        pts = trimesh.sample.volume_mesh(b, 20000)
        if len(pts) == 0:
            return 0.0
        inside = a.contains(pts)
        return float(inside.mean() * b.volume)


AXIS_IDX = {"x": 0, "y": 1, "z": 2}


def clearance(a: trimesh.Trimesh, b: trimesh.Trimesh, region: str | None = None) -> dict:
    """Distance stats from A's surface to B's surface; negative = penetration.
    `region` limits A's sample points, e.g. "z=2:18" or "z=2:18,x=0:20"."""
    pts, _ = trimesh.sample.sample_surface(a, SAMPLES)
    if region:
        keep = np.ones(len(pts), dtype=bool)
        for term in region.split(","):
            axname, _, rng = term.partition("=")
            ax = AXIS_IDX[axname.strip().lower()]
            lo, _, hi = rng.partition(":")
            keep &= (pts[:, ax] >= float(lo)) & (pts[:, ax] <= float(hi))
        pts = pts[keep]
        if len(pts) == 0:
            raise SystemExit(f"clearance region '{region}' contains no surface points")
    _, dist, _ = trimesh.proximity.closest_point(b, pts)
    sign = np.where(b.contains(pts), -1.0, 1.0)
    d = dist * sign
    return {"min": float(d.min()), "p5": float(np.percentile(d, 5)),
            "median": float(np.median(d)), "samples": int(len(pts))}


def min_wall(m: trimesh.Trimesh) -> dict:
    """Local thickness by inward ray casting from surface samples."""
    pts, fidx = trimesh.sample.sample_surface(m, SAMPLES)
    normals = m.face_normals[fidx]
    origins = pts - normals * 1e-3
    hits = m.ray.intersects_first(origins, -normals)
    ok = hits >= 0
    d = np.linalg.norm(m.triangles_center[hits[ok]] - origins[ok], axis=1)
    d = d[d > 1e-2]
    if len(d) == 0:
        return {"min": None, "p5": None}
    return {"min": float(d.min()), "p5": float(np.percentile(d, 5))}


def overhangs(m: trimesh.Trimesh, deg: float) -> dict:
    """Downward-facing area steeper than `deg` from vertical, excluding the bed face."""
    thresh = -math.sin(math.radians(deg))
    down = m.face_normals[:, 2] < thresh
    zmin = m.bounds[0][2]
    on_bed = m.triangles_center[:, 2] < zmin + 0.4
    bad = down & ~on_bed
    return {"area_mm2": float(m.area_faces[bad].sum()), "faces": int(bad.sum())}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--interference-max", type=float, default=None, metavar="MM3",
                    help="assert pairwise overlap volume <= MM3 (checked pairwise always; this makes it fatal)")
    ap.add_argument("--clearance", action="append", default=[], metavar="A:B:MIN[@REGION]",
                    help="assert min surface gap A->B >= MIN mm; optional @z=2:18[,x=..] "
                         "limits which of A's surfaces count (repeatable)")
    ap.add_argument("--min-wall", type=float, default=None, metavar="MM",
                    help="assert per-part minimum wall thickness >= MM")
    ap.add_argument("--overhang", type=float, default=None, metavar="DEG",
                    help="report unsupported area steeper than DEG from vertical (informational)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    parts = load(args.files)
    report: dict = {"parts": {}, "interference": [], "clearance": [], "failures": []}

    for name, m in parts.items():
        info = {"watertight": bool(m.is_watertight),
                "volume_mm3": round(float(m.volume), 1) if m.is_watertight else None,
                "bbox": [round(float(x), 2) for x in (m.bounds[1] - m.bounds[0])]}
        if args.min_wall is not None:
            w = min_wall(m)
            info["wall"] = w
            if w["p5"] is not None and w["p5"] < args.min_wall:
                report["failures"].append(
                    f"{name}: wall p5 {w['p5']:.2f}mm < required {args.min_wall}mm")
        if args.overhang is not None:
            info["overhang"] = overhangs(m, args.overhang)
        if not m.is_watertight:
            report["failures"].append(f"{name}: NOT watertight")
        report["parts"][name] = info

    names = list(parts)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            v = interference(parts[names[i]], parts[names[j]])
            entry = {"pair": f"{names[i]}+{names[j]}", "volume_mm3": round(v, 3)}
            report["interference"].append(entry)
            if args.interference_max is not None and v > args.interference_max:
                report["failures"].append(
                    f"{entry['pair']}: interference {v:.3f}mm³ > {args.interference_max}mm³")

    for spec in args.clearance:
        spec, _, region = spec.partition("@")
        a, b, mn = spec.rsplit(":", 2)
        if a not in parts or b not in parts:
            sys.exit(f"--clearance {spec}: unknown part (have {names})")
        c = clearance(parts[a], parts[b], region or None)
        entry = {"pair": f"{a}->{b}" + (f" @{region}" if region else ""),
                 **{k: round(v, 3) for k, v in c.items()}, "required_min": float(mn)}
        report["clearance"].append(entry)
        if c["min"] < float(mn):
            report["failures"].append(
                f"{a}->{b}: min clearance {c['min']:.3f}mm < required {mn}mm"
                + (" (PENETRATION)" if c["min"] < 0 else ""))

    report["ok"] = not report["failures"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, p in report["parts"].items():
            line = f"{name}: {'watertight' if p['watertight'] else '⚠ NOT WATERTIGHT'}"
            if p.get("wall", {}).get("min") is not None:
                line += f" · wall min {p['wall']['min']:.2f} p5 {p['wall']['p5']:.2f}mm"
            if "overhang" in p:
                line += f" · overhang {p['overhang']['area_mm2']:.0f}mm²"
            print(line)
        for e in report["interference"]:
            print(f"{e['pair']}: overlap {e['volume_mm3']}mm³")
        for e in report["clearance"]:
            print(f"{e['pair']}: gap min {e['min']} p5 {e['p5']} median {e['median']}mm"
                  f" (need ≥{e['required_min']})")
        print("PASS" if report["ok"] else "FAIL:\n  " + "\n  ".join(report["failures"]))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
