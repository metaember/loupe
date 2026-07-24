#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "shapely"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""The draft spec: a dimension record you write *before* the CAD script exists.

A draft is a YAML file holding named dimensions — value, tolerance, and where the
number came from — plus simple 2D profiles that reference those dimensions by name.
It is the single source of truth: `draftsheet.py` draws it, `draftcheck.py` asserts
it (including back against an exported STL), and `--params` hands the exact same
numbers to the CAD script so the drawing and the model cannot drift apart.

Usage:
  uv run draft.py part.draft.yaml --info                 # resolved dim table
  uv run draft.py part.draft.yaml --params dims.json     # for the CAD script to read
  uv run draft.py part.draft.yaml --params dims.py       # ...or import as constants
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

# ---------------------------------------------------------------- safe expressions
# Dimensions may be written as expressions over other dimensions ("bore_d/2 + wall").
# Evaluated with ast, not eval(): names + arithmetic + a short function whitelist.
FUNCS = {"min": min, "max": max, "abs": abs, "sqrt": math.sqrt,
         "sin": lambda d: math.sin(math.radians(d)),
         "cos": lambda d: math.cos(math.radians(d)),
         "tan": lambda d: math.tan(math.radians(d)),
         "round": round, "floor": math.floor, "ceil": math.ceil}
BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
          ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
          ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b}



def _eps(a: float, b: float) -> float:
    """Comparisons are between physical millimetres, so they must not turn on the
    last bit of a float: `floor >= 3*0.4` is a true statement about a 1.2 mm floor,
    but 3*0.4 is 1.2000000000000002 in binary."""
    return 1e-9 * max(1.0, abs(a), abs(b))


CMPOPS = {ast.Lt: lambda a, b: a < b - _eps(a, b), ast.LtE: lambda a, b: a <= b + _eps(a, b),
          ast.Gt: lambda a, b: a > b + _eps(a, b), ast.GtE: lambda a, b: a >= b - _eps(a, b),
          ast.Eq: lambda a, b: abs(a - b) <= _eps(a, b),
          ast.NotEq: lambda a, b: abs(a - b) > _eps(a, b)}


class DraftError(Exception):
    """A spec-level defect: unresolvable expression, cycle, bad geometry."""


def _eval(node: ast.AST, lookup) -> float | bool:
    if isinstance(node, ast.Expression):
        return _eval(node.body, lookup)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise DraftError(f"only numbers allowed in expressions, got {node.value!r}")
    if isinstance(node, ast.Name):
        return lookup(node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
        return BINOPS[type(node.op)](_eval(node.left, lookup), _eval(node.right, lookup))
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, lookup)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        if isinstance(node.op, ast.Not):
            return not v
    if isinstance(node, ast.Compare):  # chained comparisons: 1 < x <= 5
        left = _eval(node.left, lookup)
        for op, right_node in zip(node.ops, node.comparators):
            if type(op) not in CMPOPS:
                raise DraftError(f"unsupported comparison {type(op).__name__}")
            right = _eval(right_node, lookup)
            if not CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, lookup) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in FUNCS:
            raise DraftError(f"unknown function {node.func.id}() "
                             f"(allowed: {', '.join(sorted(FUNCS))})")
        return FUNCS[node.func.id](*[_eval(a, lookup) for a in node.args])
    raise DraftError(f"unsupported expression element {type(node).__name__}")


def evaluate(expr: str, lookup) -> float | bool:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise DraftError(f"cannot parse expression {expr!r}: {e.msg}") from None
    return _eval(tree, lookup)


# ---------------------------------------------------------------- dimensions
@dataclass
class Dim:
    name: str
    expr: str | None = None          # set when the dim is derived from others
    raw: float | None = None         # set when the dim is a literal
    tol: tuple[float, float] | None = None   # (upper, lower) magnitudes, both positive
    source: str | None = None        # provenance: "calipers, n=5" / "datasheet p3"
    value: float = field(default=0.0, init=False)

    @property
    def derived(self) -> bool:
        return self.expr is not None

    def tol_text(self) -> str:
        if self.tol is None:
            return ""
        up, lo = self.tol
        return f"±{up:g}" if up == lo else f"+{up:g}/-{lo:g}"


def _parse_tol(v) -> tuple[float, float] | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return (abs(float(v)), abs(float(v)))
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return (abs(float(v[0])), abs(float(v[1])))
    raise DraftError(f"tol must be a number or [upper, lower], got {v!r}")


def _parse_dim(name: str, spec) -> Dim:
    if isinstance(spec, str):                      # neck_r: "bore_d/2 + wall"
        return Dim(name, expr=spec)
    if isinstance(spec, (int, float)):             # body_h: 34
        return Dim(name, raw=float(spec))
    if isinstance(spec, dict):
        if "v" in spec and "expr" in spec:
            raise DraftError(f"dim {name!r}: give either 'v' or 'expr', not both")
        d = Dim(name, tol=_parse_tol(spec.get("tol")),
                source=spec.get("from") or spec.get("source"))
        if "expr" in spec:
            d.expr = str(spec["expr"])
        elif "v" in spec:
            d.raw = float(spec["v"])
        else:
            raise DraftError(f"dim {name!r}: needs 'v' (a value) or 'expr'")
        return d
    raise DraftError(f"dim {name!r}: expected a number, an expression string, or a mapping")


# ---------------------------------------------------------------- geometry
def _rounded_rect(x: float, y: float, w: float, h: float, r: float) -> Polygon:
    box = Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
    if r <= 0:
        return box
    if r > min(w, h) / 2 + 1e-9:
        raise DraftError(f"corner radius {r:g} too large for a {w:g}×{h:g} rect")
    return box.buffer(-r, join_style=2).buffer(r, join_style=1, quad_segs=32)


@dataclass
class Feature:
    id: str
    kind: str                     # circle | rect | slot | poly
    geom: Polygon
    note: str | None = None
    # circle/slot only — carried so the drawing can emit a real ⌀/R callout
    center: tuple[float, float] | None = None
    radius: float | None = None
    ends: tuple[tuple[float, float], tuple[float, float]] | None = None
    tol_ref: str | None = None    # name of the dim whose tolerance annotates it


@dataclass
class View:
    name: str
    plane: str
    outline: Polygon
    features: list[Feature]
    dims: list[dict]              # drawing instructions, resolved at render time


@dataclass
class Draft:
    name: str
    units: str
    min_wall: float | None
    dims: dict[str, Dim]
    views: dict[str, View]
    checks: list[dict]
    fits: list[dict]
    asbuilt: dict[str, dict]
    path: Path

    def value(self, name: str) -> float:
        if name not in self.dims:
            raise DraftError(f"unknown dimension {name!r}")
        return self.dims[name].value

    def num(self, v) -> float:
        """A YAML scalar that may be a literal or an expression over the dims."""
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return float(evaluate(v, self.value))
        raise DraftError(f"expected a number or expression, got {v!r}")

    def params(self) -> dict[str, float]:
        return {n: d.value for n, d in self.dims.items()}


# ---------------------------------------------------------------- loading
def _resolve_dims(dims: dict[str, Dim]) -> None:
    """Depth-first resolution with an explicit stack, so cycles name themselves."""
    done: set[str] = set()
    stack: list[str] = []

    def get(name: str) -> float:
        if name not in dims:
            raise DraftError(f"unknown dimension {name!r}"
                             + (f" (referenced from {stack[-1]!r})" if stack else ""))
        if name in done:
            return dims[name].value
        if name in stack:
            loop = " -> ".join(stack[stack.index(name):] + [name])
            raise DraftError(f"circular dimension: {loop}")
        d = dims[name]
        stack.append(name)
        d.value = float(d.raw) if d.raw is not None else float(evaluate(d.expr, get))
        stack.pop()
        done.add(name)
        return d.value

    for n in list(dims):
        get(n)


def _build_shape(draft: Draft, spec: dict, what: str) -> tuple[str, Polygon, dict]:
    """One tagged-union geometry entry -> (kind, polygon, callout metadata)."""
    n = draft.num
    if "circle" in spec:
        cx, cy = (n(v) for v in spec["circle"])
        if "d" in spec:
            r = n(spec["d"]) / 2
        elif "r" in spec:
            r = n(spec["r"])
        else:
            raise DraftError(f"{what}: a circle needs 'd' or 'r'")
        return "circle", Point(cx, cy).buffer(r, quad_segs=64), {"center": (cx, cy), "radius": r}
    if "rect" in spec:
        x, y, w, h = (n(v) for v in spec["rect"])
        return "rect", _rounded_rect(x, y, w, h, n(spec.get("r", 0))), {}
    if "slot" in spec:
        x1, y1, x2, y2 = (n(v) for v in spec["slot"])
        if "w" not in spec:
            raise DraftError(f"{what}: a slot needs 'w' (its width)")
        w = n(spec["w"])
        geom = LineString([(x1, y1), (x2, y2)]).buffer(w / 2, quad_segs=64)
        return "slot", geom, {"radius": w / 2, "ends": ((x1, y1), (x2, y2))}
    if "poly" in spec:
        pts = [(n(p[0]), n(p[1])) for p in spec["poly"]]
        if len(pts) < 3:
            raise DraftError(f"{what}: a poly needs at least 3 points")
        return "poly", Polygon(pts), {}
    raise DraftError(f"{what}: expected one of circle / rect / slot / poly, "
                     f"got keys {sorted(spec)}")


def _build_view(draft: Draft, name: str, spec: dict) -> View:
    if "outline" not in spec:
        raise DraftError(f"view {name!r}: needs an 'outline'")
    outlines = spec["outline"]
    if isinstance(outlines, dict):
        outlines = [outlines]
    polys = [_build_shape(draft, o, f"view {name!r} outline")[1] for o in outlines]
    outline = unary_union(polys)
    if not isinstance(outline, Polygon):
        raise DraftError(f"view {name!r}: outline pieces are disjoint — "
                         "they must overlap or touch to form one profile")

    feats: list[Feature] = []
    for i, f in enumerate(spec.get("features") or []):
        fid = str(f.get("id") or f"f{i}")
        if any(x.id == fid for x in feats):
            raise DraftError(f"view {name!r}: duplicate feature id {fid!r}")
        kind, geom, meta = _build_shape(draft, f, f"view {name!r} feature {fid!r}")
        feats.append(Feature(id=fid, kind=kind, geom=geom, note=f.get("note"),
                             center=meta.get("center"), radius=meta.get("radius"),
                             ends=meta.get("ends"), tol_ref=f.get("tol_from")))
    return View(name=name, plane=str(spec.get("plane", "xy")), outline=outline,
                features=feats, dims=list(spec.get("dims") or []))


def load(path: str | Path) -> Draft:
    p = Path(path)
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise DraftError(f"{p}: invalid YAML — {e}") from None
    if not isinstance(doc, dict):
        raise DraftError(f"{p}: expected a mapping at the top level")

    dims = {str(k): _parse_dim(str(k), v) for k, v in (doc.get("dims") or {}).items()}
    _resolve_dims(dims)

    draft = Draft(
        name=str(doc.get("name", p.stem)),
        units=str(doc.get("units", "mm")),
        min_wall=doc.get("min_wall"),
        dims=dims, views={}, checks=list(doc.get("checks") or []),
        fits=list(doc.get("fits") or []), asbuilt=dict(doc.get("asbuilt") or {}),
        path=p,
    )
    if draft.units != "mm":
        raise DraftError(f"units: only mm is supported (got {draft.units!r}) — "
                         "the rest of the pipeline is mm throughout")
    if draft.min_wall is not None:
        draft.min_wall = draft.num(draft.min_wall)
    for vname, vspec in (doc.get("views") or {}).items():
        draft.views[str(vname)] = _build_view(draft, str(vname), vspec)
    return draft


# ---------------------------------------------------------------- params export
def params_json(draft: Draft) -> str:
    return json.dumps({
        "name": draft.name, "units": draft.units,
        "dims": {n: {"v": d.value, **({"tol": list(d.tol)} if d.tol else {}),
                     **({"from": d.source} if d.source else {}),
                     **({"expr": d.expr} if d.expr else {})}
                 for n, d in draft.dims.items()},
    }, indent=2) + "\n"


def params_py(draft: Draft) -> str:
    out = ["# Generated by draft.py from "
           f"{draft.path.name} — do not edit; edit the draft and regenerate.",
           f'"""Resolved dimensions for {draft.name} (all {draft.units})."""', ""]
    width = max((len(n) for n in draft.dims), default=1)
    for n, d in draft.dims.items():
        note = " | ".join(x for x in (d.tol_text(), d.expr, d.source) if x)
        out.append(f"{n:<{width}} = {d.value!r}" + (f"  # {note}" if note else ""))
    out.append("")
    out.append("DIMS = {" + ", ".join(f"{n!r}: {n}" for n in draft.dims) + "}")
    return "\n".join(out) + "\n"


def info(draft: Draft) -> str:
    rows = [("DIM", "VALUE", "TOL", "SOURCE")]
    for n, d in draft.dims.items():
        src = d.expr if d.derived else (d.source or "")
        rows.append((n, f"{d.value:.4g}", d.tol_text(), ("= " + src) if d.derived else src))
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = [f"{draft.name}  ({draft.units})", ""]
    for i, r in enumerate(rows):
        lines.append("  ".join(c.ljust(w[j]) for j, c in enumerate(r)).rstrip())
        if i == 0:
            lines.append("  ".join("-" * x for x in w))
    for vn, v in draft.views.items():
        xmin, ymin, xmax, ymax = v.outline.bounds
        lines.append(f"\nview {vn} ({v.plane}): extent "
                     f"{xmax - xmin:.4g} × {ymax - ymin:.4g}, "
                     f"{len(v.features)} feature(s), {len(v.dims)} dimension(s)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read a draft spec: resolve dims, export params.")
    ap.add_argument("spec")
    ap.add_argument("--params", metavar="OUT.json|OUT.py",
                    help="write the resolved dimension table for the CAD script to import")
    ap.add_argument("--info", action="store_true", help="print the resolved dim table")
    a = ap.parse_args()

    try:
        draft = load(a.spec)
    except DraftError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)

    if a.params:
        out = Path(a.params)
        text = params_py(draft) if out.suffix == ".py" else params_json(draft)
        out.write_text(text)
        print(f"wrote {out}  ({len(draft.dims)} dims)")
    if a.info or not a.params:
        print(info(draft))


if __name__ == "__main__":
    main()
