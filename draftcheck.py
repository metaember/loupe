#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "shapely", "numpy", "trimesh", "scipy", "networkx", "rtree"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Deterministic checks for a draft spec — the gate for the planning stage.

Everything here is decided before a solid exists, which is exactly why it is cheap:
containment and wall thickness are exact 2D polygon distances, not mesh sampling.
With --stl it also closes the loop the other way, asserting an exported mesh still
matches the dimensions the draft recorded.

  uv run draftcheck.py part.draft.yaml
  uv run draftcheck.py part.draft.yaml --stl asm_case.stl --json

Exits nonzero when an assertion fails — wire it in before you write the CAD script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from draft import Draft, DraftError, evaluate, load  # noqa: E402

# Clearance per side, in mm, for FDM. These are process rules of thumb, not ISO 286
# fits — IT grades below ~IT12 are unreachable on a consumer FFF machine. Sources are
# carried into the report so a number never appears without saying where it came from.
FIT_CLASSES: dict[str, tuple[float, float, str]] = {
    "press": (0.00, 0.15, "press/interference: ~0.1 mm/side"),
    "slip":  (0.15, 0.35, "slip/sliding: 0.2-0.3 mm/side"),
    "free":  (0.35, 0.65, "free/loose: 0.4-0.5 mm/side"),
}
PLANE_AXIS = {"yz": 0, "xz": 1, "xy": 2}   # section normal for each view plane
EPS = 1e-7


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: str, **extra) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail, **extra})
        return ok

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]

    def text(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"  {'ok  ' if c['ok'] else 'FAIL'}  {c['check']}: {c['detail']}")
        return "\n".join(lines)


# ---------------------------------------------------------------- spec checks
def check_containment(draft: Draft, rep: Report) -> None:
    for vn, view in draft.views.items():
        for f in view.features:
            inside = view.outline.buffer(EPS).contains(f.geom)
            spill = f.geom.difference(view.outline).area
            rep.add(f"containment[{vn}.{f.id}]", inside,
                    "inside the outline" if inside else
                    f"extends {spill:.4g} mm² outside the {vn} outline")


def check_min_wall(draft: Draft, rep: Report) -> None:
    """Feature-to-feature and feature-to-edge distance. Features are voids: the
    material left between them is what the printer has to actually build."""
    if draft.min_wall is None:
        return
    mw = draft.min_wall
    for vn, view in draft.views.items():
        edge = view.outline.exterior
        for i, f in enumerate(view.features):
            d = edge.distance(f.geom)
            rep.add(f"min_wall[{vn}.{f.id}->edge]", d >= mw - EPS,
                    f"{d:.3f} mm to the outline edge (min {mw:g})", mm=round(d, 4))
            for g in view.features[i + 1:]:
                d = f.geom.distance(g.geom)
                ok = d >= mw - EPS
                rep.add(f"min_wall[{vn}.{f.id}->{g.id}]", ok,
                        f"{d:.3f} mm between features (min {mw:g})"
                        + ("" if ok else " — they nearly meet" if d > 0 else " — they overlap"),
                        mm=round(d, 4))


def check_expressions(draft: Draft, rep: Report) -> None:
    for i, c in enumerate(draft.checks):
        if "expr" not in c:
            raise DraftError(f"checks[{i}]: needs an 'expr'")
        expr = str(c["expr"])
        ok = bool(evaluate(expr, draft.value))
        rep.add(f"check[{c.get('msg', expr)}]", ok,
                expr if ok else f"{expr} is false"
                + "".join(f"  ({n}={draft.value(n):g})" for n in sorted(draft.dims)
                          if n in expr.split()) )


def check_fits(draft: Draft, rep: Report) -> None:
    for i, f in enumerate(draft.fits):
        name = f.get("name", f"fits[{i}]")
        for k in ("hole", "shaft", "class"):
            if k not in f:
                raise DraftError(f"{name}: a fit needs 'hole', 'shaft' and 'class'")
        cls = str(f["class"])
        if cls not in FIT_CLASSES:
            raise DraftError(f"{name}: unknown fit class {cls!r} "
                             f"(known: {', '.join(FIT_CLASSES)})")
        lo, hi, why = FIT_CLASSES[cls]
        per_side = (draft.num(f["hole"]) - draft.num(f["shaft"])) / 2
        ok = lo - EPS <= per_side <= hi + EPS
        rep.add(f"fit[{name}]", ok,
                f"{per_side:+.3f} mm/side, {cls} wants {lo:g}–{hi:g} ({why})",
                per_side=round(per_side, 4), fit_class=cls)


# ---------------------------------------------------------------- as-built checks
def _section_polys(mesh: trimesh.Trimesh, axis: int, val: float) -> list:
    """Cross-section as shapely polygons in the same 2D frame sheet.py uses."""
    normal = np.zeros(3); normal[axis] = 1.0
    origin = np.zeros(3); origin[axis] = val
    sec = mesh.section(plane_origin=origin, plane_normal=normal)
    if sec is None:
        return []
    (ua, va), (us, vs) = ({2: ([0, 1], [1, 1]), 1: ([0, 2], [1, 1])}
                          .get(axis, ([1, 2], [-1, 1])))
    M = np.zeros((4, 4)); M[3, 3] = 1.0
    M[0, ua] = us; M[1, va] = vs; M[2, axis] = 1.0; M[2, 3] = -val
    flat, _ = sec.to_2D(to_2D=M)
    try:
        return list(flat.polygons_full)      # needs rtree, or holes are dropped
    except ModuleNotFoundError:
        raise                                # a missing dep is not "no geometry"
    except ValueError:
        return []


def check_asbuilt(draft: Draft, stls: list[str], rep: Report) -> None:
    by_stem = {Path(s).stem: s for s in stls}
    for part, spec in draft.asbuilt.items():
        path = by_stem.get(part)
        if path is None:
            rep.add(f"asbuilt[{part}]", False,
                    f"no STL passed for {part!r} (got: {', '.join(by_stem) or 'none'})")
            continue
        mesh = trimesh.load(path, force="mesh", process=True)
        tol = float(draft.num(spec.get("tol", 0.2)))

        if "bbox" in spec:
            want = np.array([draft.num(v) for v in spec["bbox"]], dtype=float)
            got = np.asarray(mesh.bounding_box.extents, dtype=float)
            err = np.abs(got - want)
            rep.add(f"asbuilt[{part}].bbox", bool((err <= tol + EPS).all()),
                    f"built {got[0]:.3f}×{got[1]:.3f}×{got[2]:.3f}, "
                    f"drafted {want[0]:.3f}×{want[1]:.3f}×{want[2]:.3f}, "
                    f"worst Δ{err.max():.3f} (tol {tol:g})",
                    built=[round(v, 4) for v in got], drafted=[round(v, 4) for v in want])

        holes = spec.get("holes")
        if holes:
            _check_holes(draft, part, mesh, holes, tol, rep)


def _check_holes(draft: Draft, part: str, mesh, holes: dict, tol: float, rep: Report) -> None:
    """Every circular feature in a view must appear as a real hole in the mesh."""
    vname = str(holes.get("view", ""))
    if vname not in draft.views:
        raise DraftError(f"asbuilt[{part}].holes: unknown view {vname!r}")
    view = draft.views[vname]
    axis = PLANE_AXIS.get(view.plane)
    if axis is None:
        raise DraftError(f"view {vname!r}: plane {view.plane!r} must be xy, xz or yz")
    if "at" not in holes:
        raise DraftError(f"asbuilt[{part}].holes: needs 'at' (where to cut the section)")
    at = draft.num(holes["at"])

    polys = _section_polys(mesh, axis, at)
    if not polys:
        rep.add(f"asbuilt[{part}].holes", False,
                f"section of {view.plane} at {at:g} is empty — nothing to measure there")
        return
    rings = [(p.centroid, 2 * (p.area / np.pi) ** 0.5)
             for poly in polys for p in map(_ring_poly, poly.interiors)]

    for f in view.features:
        if f.kind != "circle":
            continue
        want_d = 2 * f.radius
        near = [(c, d) for c, d in rings
                if abs(c.x - f.center[0]) <= tol + want_d and abs(c.y - f.center[1]) <= tol + want_d]
        if not near:
            rep.add(f"asbuilt[{part}].hole[{f.id}]", False,
                    f"no hole near ({f.center[0]:g}, {f.center[1]:g}) in the "
                    f"{view.plane} section at {at:g} — drafted ⌀{want_d:.3f}")
            continue
        c, got_d = min(near, key=lambda t: (t[0].x - f.center[0]) ** 2 + (t[0].y - f.center[1]) ** 2)
        off = ((c.x - f.center[0]) ** 2 + (c.y - f.center[1]) ** 2) ** 0.5
        ok = abs(got_d - want_d) <= tol + EPS and off <= tol + EPS
        rep.add(f"asbuilt[{part}].hole[{f.id}]", ok,
                f"built ⌀{got_d:.3f} at ({c.x:.3f}, {c.y:.3f}), drafted ⌀{want_d:.3f} "
                f"at ({f.center[0]:g}, {f.center[1]:g}) — Δ⌀{got_d - want_d:+.3f}, "
                f"off-centre {off:.3f} (tol {tol:g})")


def _ring_poly(ring):
    from shapely.geometry import Polygon
    return Polygon(ring)


def main() -> None:
    ap = argparse.ArgumentParser(description="Assert a draft spec (and optionally the STL built from it).")
    ap.add_argument("spec")
    ap.add_argument("--stl", nargs="+", default=[], metavar="FILE",
                    help="exported meshes to check against the draft's asbuilt: block")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    a = ap.parse_args()

    rep = Report()
    try:
        draft = load(a.spec)
        rep.add("resolve", True, f"{len(draft.dims)} dims, {len(draft.views)} view(s)")
        check_containment(draft, rep)
        check_min_wall(draft, rep)
        check_expressions(draft, rep)
        check_fits(draft, rep)
        if a.stl:
            check_asbuilt(draft, a.stl, rep)
        elif draft.asbuilt:
            print(f"note: {len(draft.asbuilt)} asbuilt entr(y/ies) declared; "
                  "pass --stl to check them", file=sys.stderr)
    except DraftError as e:
        if a.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            print(f"{a.spec}\nFAIL: {e}")
        sys.exit(1)

    if a.json:
        print(json.dumps({"ok": not rep.failed, "spec": draft.name,
                          "checks": rep.checks}, indent=2))
    else:
        print(f"{draft.name}  ({a.spec})")
        print(rep.text())
        if rep.failed:
            print(f"\nFAIL: {len(rep.failed)} of {len(rep.checks)} checks — "
                  + "; ".join(c["check"] for c in rep.failed))
        else:
            print(f"\nPASS  ({len(rep.checks)} checks)")
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
