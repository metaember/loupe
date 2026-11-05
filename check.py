#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "trimesh", "scipy", "rtree", "manifold3d>=3.5.2"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Deterministic geometry checks for 3D-print designs (Gen-2 verifier).

Machine-checkable diagnostics that a render can't guarantee: 3D interference
volume between parts, minimum clearance between mating parts, watertightness,
sustained thin walls, and an overhang report. Emits JSON + human summary; exits
nonzero when an assertion fails — wire it after STL export so violations kill the build.

Usage:
  uv run tools/check.py body.stl lid.stl                                 # default 0.4mm wall gate
  uv run tools/check.py body.stl lid.stl --interference-max 0.01         # assert no overlap
  uv run tools/check.py body.stl lid.stl --clearance body:lid:0.15       # assert min gap
  uv run tools/check.py part.stl --min-wall 1.2 --overhang 45
Part names = file stems.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import manifold3d
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
import trimesh

SAMPLES = 4000  # surface sample points for clearance checks
DEFAULT_MIN_WALL = 0.4
WALL_SAMPLE_AREA = 4.0
MAX_WALL_SAMPLES = 1_000_000
WALL_OPPOSITION = math.cos(math.radians(30))


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


def _wall_samples(m: trimesh.Trimesh, max_area: float) -> tuple:
    """Centroid quadrature on every face, subdividing large faces into equal areas."""
    triangles = np.asarray(m.triangles)
    face_ids = np.arange(len(triangles))
    areas = np.asarray(m.area_faces)
    points, faces, weights = [], [], []
    count = 0
    while len(triangles):
        small = areas <= max_area
        count += int(small.sum())
        if count + int((~small).sum()) * 4 > MAX_WALL_SAMPLES:
            raise ValueError(f"wall sampling exceeds {MAX_WALL_SAMPLES} points")
        points.append(triangles[small].mean(axis=1))
        faces.append(face_ids[small])
        weights.append(areas[small])
        large = triangles[~small]
        a, b, c = large[:, 0], large[:, 1], large[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        triangles = np.concatenate([
            np.stack([a, ab, ca], axis=1), np.stack([ab, b, bc], axis=1),
            np.stack([ca, bc, c], axis=1), np.stack([ab, bc, ca], axis=1)])
        face_ids = np.tile(face_ids[~small], 4)
        areas = np.tile(areas[~small] / 4, 4)
    return np.concatenate(points), np.concatenate(faces), np.concatenate(weights)


def _wall_rays(solid, points, normals, epsilon, length) -> tuple:
    """Use actual hit positions, not triangle centers (RayHit.distance is fractional)."""
    distances = np.full(len(points), np.nan)
    opposition = np.full(len(points), np.nan)
    for i, (point, normal) in enumerate(zip(points, normals)):
        hits = solid.ray_cast(point - normal * epsilon, point - normal * length)
        if hits:
            hit = hits[0]
            distance = float(np.dot(np.asarray(hit.position) - point, -normal))
            dot = float(np.dot(hit.normal, normal))
            if np.isfinite(distance) and distance > epsilon and dot < 0:
                distances[i], opposition[i] = distance, dot
    return distances, opposition


def min_wall(m: trimesh.Trimesh, required: float = DEFAULT_MIN_WALL,
             sample_area: float = WALL_SAMPLE_AREA) -> dict:
    """Deterministic sustained thin-patch check; not a proof of global thickness.

    Short chords exiting an adjacent chamfer/fillet are reported as edge effects.
    Opposing surfaces (within 30 degrees of antiparallel) remain eligible even at
    an edge. Connected thin patches at least (required / 2)**2 mm2 fail the gate.
    Area is quadrature-estimated; sub-face defects can fall between sample points.
    """
    if not np.isfinite(required) or required <= 0:
        raise ValueError("minimum wall must be a positive finite number")
    if not np.isfinite(sample_area) or sample_area <= 0:
        raise ValueError("wall sample area must be a positive finite number")
    patch_area = (required / 2) ** 2
    report = {"status": "indeterminate", "min": None, "p5": None,
              "required_min_mm": required, "sample_max_area_mm2": sample_area,
              "sustained_area_min_mm2": patch_area, "opposition_angle_deg": 30,
              "samples": 0, "unresolved_samples": 0, "unresolved_area_mm2": 0.0,
              "edge_short_area_mm2": 0.0, "subresolution_thin_area_mm2": 0.0,
              "thin_patches": [], "errors": []}
    if not m.is_volume or not np.isfinite(m.vertices).all():
        report["errors"].append("wall check requires a finite, consistently wound positive volume")
        return report
    try:
        points, face_ids, weights = _wall_samples(m, sample_area)
        solid = manifold3d.Manifold(manifold3d.Mesh64(
            np.asarray(m.vertices), np.asarray(m.faces, dtype=np.uint64)))
        if solid.status() != manifold3d.Error.NoError or solid.is_empty():
            raise ValueError(f"wall ray mesh rejected: {solid.status()}")
        length = float(np.linalg.norm(m.extents)) * 1.01
        epsilon = min(required * 1e-6, length * 1e-9)
        distances, opposition = _wall_rays(solid, points, m.face_normals[face_ids],
                                           epsilon, length)
    except Exception as exc:
        report["errors"].append(f"wall check could not complete: {exc}")
        return report
    report["samples"] = len(points)
    resolved = np.isfinite(distances) & np.isfinite(opposition)
    if not resolved.all():
        missing = ~resolved
        report["unresolved_samples"] = int(missing.sum())
        report["unresolved_area_mm2"] = float(weights[missing].sum())
        report["unresolved_location_mm"] = points[np.flatnonzero(missing)[0]].tolist()
        report["errors"].append("inward rays without a reliable exit; thickness is indeterminate")
    if resolved.any():
        d, w = distances[resolved], weights[resolved]
        order = np.argsort(d, kind="stable")
        p5 = np.searchsorted(np.cumsum(w[order]), w.sum() * 0.05)
        report["min"] = float(d.min())
        report["p5"] = float(d[order[min(p5, len(order) - 1)]])
    short = resolved & (distances < required - epsilon)
    thin = short & (opposition <= -WALL_OPPOSITION)
    report["edge_short_area_mm2"] = float(weights[short & ~thin].sum())
    thin_area = np.bincount(face_ids[thin], weights=weights[thin], minlength=len(m.faces))
    active = thin_area > 0
    adjacency = m.face_adjacency
    adjacency = adjacency[active[adjacency].all(axis=1)]
    graph = coo_matrix((np.ones(len(adjacency)), (adjacency[:, 0], adjacency[:, 1])),
                       shape=(len(m.faces), len(m.faces)))
    _, labels = connected_components(graph, directed=False)
    for label in np.unique(labels[active]):
        selected_faces = active & (labels == label)
        area = float(thin_area[selected_faces].sum())
        if area < patch_area:
            report["subresolution_thin_area_mm2"] += area
            continue
        selected = thin & selected_faces[face_ids]
        indexes = np.flatnonzero(selected)
        worst = indexes[np.argmin(distances[selected])]
        bounds = m.triangles[selected_faces].reshape(-1, 3)
        report["thin_patches"].append({
            "area_mm2": area, "min_mm": float(distances[worst]),
            "max_mm": float(distances[selected].max()),
            "location_mm": points[worst].tolist(),
            "bounds_mm": [bounds.min(axis=0).tolist(), bounds.max(axis=0).tolist()],
            "faces": int(selected_faces.sum()), "samples": int(selected.sum())})
    report["thin_patches"].sort(key=lambda p: (-p["area_mm2"], p["location_mm"]))
    if not report["errors"]:
        report["status"] = "thin" if report["thin_patches"] else "ok"
    return report


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
    walls = ap.add_mutually_exclusive_group()
    walls.add_argument("--min-wall", type=float, default=DEFAULT_MIN_WALL, metavar="MM",
                       help="assert sustained thin patches >= MM (default: 0.4 for FDM)")
    walls.add_argument("--no-wall-check", action="store_true",
                       help="explicitly skip wall preflight for intentional thin/non-FDM parts")
    ap.add_argument("--wall-sample-area", type=float, default=WALL_SAMPLE_AREA, metavar="MM2",
                    help="maximum area per deterministic wall sample (default: 4 mm2)")
    ap.add_argument("--overhang", type=float, default=None, metavar="DEG",
                    help="report unsupported area steeper than DEG from vertical (informational)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not np.isfinite(args.min_wall) or args.min_wall <= 0:
        ap.error("--min-wall must be positive and finite; use --no-wall-check to opt out")
    if not np.isfinite(args.wall_sample_area) or args.wall_sample_area <= 0:
        ap.error("--wall-sample-area must be positive and finite")

    parts = load(args.files)
    report: dict = {"parts": {}, "interference": [], "clearance": [], "failures": []}

    for name, m in parts.items():
        info = {"watertight": bool(m.is_watertight),
                "is_volume": bool(m.is_volume),
                "volume_mm3": round(float(m.volume), 1) if m.is_watertight else None,
                "bbox": [round(float(x), 2) for x in (m.bounds[1] - m.bounds[0])]}
        if not args.no_wall_check:
            w = min_wall(m, args.min_wall, args.wall_sample_area)
            info["wall"] = w
            for error in w["errors"]:
                report["failures"].append(f"{name}: {error}")
            for patch in w["thin_patches"]:
                report["failures"].append(
                    f"{name}: thin patch {patch['min_mm']:.6g}mm < {args.min_wall}mm, "
                    f"area ~{patch['area_mm2']:.3g}mm2 at "
                    f"{[round(x, 4) for x in patch['location_mm']]}")
        else:
            info["wall"] = {"status": "skipped", "reason": "--no-wall-check"}
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
                line += (f" · ray min {p['wall']['min']:.6g} p5 {p['wall']['p5']:.2f}mm"
                         f" · wall {p['wall']['status']}")
            elif "wall" in p:
                line += f" · wall {p['wall']['status']}"
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
