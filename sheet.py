#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "trimesh", "pillow", "shapely", "networkx", "scipy", "rtree"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Contact sheet renderer for 3D-print design review.

Renders one or more mesh files (STL/OBJ/GLB/3MF) into a single labeled PNG:
standard orthographic views + isometrics, optional 2D cross-section slices
(with per-part colors and automatic part-vs-part interference highlighting),
and optional 3D cutaways. Every tile says what you're looking at.

Usage (uv resolves deps from the header):
  uv run sheet.py part1.stl part2.stl -o sheet.png
  uv run sheet.py body.stl lid.stl --slice z=50% --slice z=12 --slice x=50%
  uv run sheet.py body.stl lid.stl --cutaway "y>50%" --views iso,front,top
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- palette / style
PALETTE = [
    (90, 196, 184),   # teal
    (224, 158, 88),   # amber
    (168, 150, 222),  # violet
    (120, 205, 150),  # green
    (233, 110, 98),   # coral
    (238, 232, 205),  # cream
    (110, 160, 235),  # blue
    (222, 140, 190),  # pink
]
BG = (24, 26, 31)
TILE_BG = (30, 33, 39)
SLICE_BG = (36, 39, 46)
CAPTION_BG = (42, 46, 54)
TEXT = (214, 218, 224)
DIM_TEXT = (150, 156, 166)
GRID_COL = (52, 57, 66)
INTERFERE = (255, 60, 60)
LIGHT = np.array([-0.35, -0.5, 0.78])
LIGHT_DIR = LIGHT / np.linalg.norm(LIGHT)

TILE_W, TILE_H = 560, 420
CAPTION_H = 26
SS = 2  # supersampling factor for 3D tiles

VIEWS: dict[str, tuple[float, float, str]] = {
    # name: (az_deg, el_deg, "screen-axes hint")
    "front":  (0,    0,  "right=+X  up=+Z"),
    "back":   (180,  0,  "right=−X  up=+Z"),
    "left":   (90,   0,  "right=−Y  up=+Z"),
    "right":  (-90,  0,  "right=+Y  up=+Z"),
    "top":    (0,   90,  "right=+X  up=+Y"),
    "bottom": (0,  -90,  "right=+X  up=−Y"),
    "iso":    (-38, 30,  "+Z up"),
    "iso2":   (142, 30,  "+Z up"),
}
DEFAULT_VIEWS = ["iso", "iso2", "front", "back", "left", "right", "top", "bottom"]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- geometry
@dataclass
class Part:
    name: str
    mesh: trimesh.Trimesh
    color: tuple[int, int, int]
    tris: np.ndarray = field(init=False)  # (n, 3, 3)

    def __post_init__(self) -> None:
        self.tris = self.mesh.triangles.astype(np.float64)


def load_parts(paths: list[str]) -> list[Part]:
    parts: list[Part] = []
    for i, p in enumerate(paths):
        m = trimesh.load(p, force="mesh", process=False)
        parts.append(Part(Path(p).stem, m, PALETTE[i % len(PALETTE)]))
    return parts


def view_transform(az_deg: float, el_deg: float) -> np.ndarray:
    """3x3 world->(screen_x, screen_y, depth). Z-up world; camera looks along +depth."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    # yaw about Z, then tilt: screen = (x1, z2), depth = y2  (same math as render.py)
    return np.array([
        [ca, -sa, 0.0],            # screen x
        [sa * se, ca * se, ce],    # screen y
        [sa * ce, ca * ce, -se],   # depth
    ])


def camera_words(az_deg: float, el_deg: float) -> str:
    d = -view_transform(az_deg, el_deg)[2]  # camera sits opposite the depth axis
    words = []
    if d[1] < -0.25: words.append("front")
    if d[1] > 0.25: words.append("back")
    if d[0] < -0.25: words.append("left")
    if d[0] > 0.25: words.append("right")
    if d[2] > 0.25: words.append("above")
    if d[2] < -0.25: words.append("below")
    return "from " + "-".join(words) if words else ""


def clip_tris_halfspace(tris: np.ndarray, axis: int, val: float, keep_less: bool) -> np.ndarray:
    """Clip triangles to a half-space, re-triangulating straddlers so the cut edge
    lands exactly on the plane. Without this, whole-triangle culling leaves coarse
    facets jutting past the cut as shards. keep_less=True keeps coord<=val (cutaway
    op '>'); keep_less=False keeps coord>=val (op '<')."""
    c = tris[:, :, axis]
    s = (val - c) if keep_less else (c - val)     # signed keep-distance; >=0 is kept
    inside = s >= 0
    cnt = inside.sum(axis=1)
    kept = tris[cnt == 3]                          # fully inside -> unchanged
    new: list = []
    straddle = (cnt == 1) | (cnt == 2)
    for tri, sd, ins in zip(tris[straddle], s[straddle], inside[straddle]):
        poly: list = []                           # keep-side polygon (Sutherland-Hodgman)
        for i in range(3):
            j = (i + 1) % 3
            if ins[i]:
                poly.append(tri[i])
            if ins[i] != ins[j]:                  # edge crosses the plane -> split vertex on it
                t = sd[i] / (sd[i] - sd[j])
                poly.append(tri[i] + t * (tri[j] - tri[i]))
        for k in range(1, len(poly) - 1):         # fan-triangulate the 3- or 4-gon
            new.append([poly[0], poly[k], poly[k + 1]])
    if new:
        return np.concatenate([kept, np.asarray(new, dtype=tris.dtype)], axis=0)
    return kept


def render_view(parts: list[Part], az: float, el: float, w: int, h: int,
                scale: float, center: np.ndarray,
                cut: tuple[int, str, float] | None = None,
                box: tuple[np.ndarray, np.ndarray] | None = None) -> Image.Image:
    """Flat-shaded z-buffer render at a fixed mm->px scale (comparable tiles)."""
    M = view_transform(az, el)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = TILE_BG
    zbuf = np.full((h, w), np.inf, dtype=np.float64)

    for part in parts:
        tris = part.tris
        if box is not None:                      # ROI: keep tris whose bbox overlaps the region
            tmin, tmax = tris.min(axis=1), tris.max(axis=1)
            keep = np.all((tmax >= box[0] - 1e-9) & (tmin <= box[1] + 1e-9), axis=1)
            tris = tris[keep]
            if len(tris) == 0:
                continue
        if cut is not None:
            axis, op, val = cut
            tris = clip_tris_halfspace(tris, axis, val, keep_less=(op == ">"))
            if len(tris) == 0:
                continue
        v = (tris - center) @ M.T  # (n,3,3) -> screen coords
        sx = w / 2 + scale * v[:, :, 0]
        sy = h / 2 - scale * v[:, :, 1]
        sz = v[:, :, 2]

        # per-face shading from screen-space normal, flipped toward camera
        e1 = np.stack([sx[:, 1] - sx[:, 0], sy[:, 0] - sy[:, 1], sz[:, 1] - sz[:, 0]], axis=1)
        e2 = np.stack([sx[:, 2] - sx[:, 0], sy[:, 0] - sy[:, 2], sz[:, 2] - sz[:, 0]], axis=1)
        n = np.cross(e1, e2)
        nl = np.linalg.norm(n, axis=1)
        nl[nl == 0] = 1
        n /= nl[:, None]
        n[n[:, 2] > 0] *= -1  # camera looks along +depth; keep normals facing it
        diff = np.clip(-(n @ LIGHT_DIR.astype(np.float64)), 0, None)
        shade = 0.34 + 0.75 * diff
        cols = np.clip(np.array(part.color)[None, :] * shade[:, None], 0, 255).astype(np.uint8)

        order = np.argsort(-sz.mean(axis=1))  # far-to-near: fewer z-buffer writes win
        for i in order:
            ax_, ay, az_ = sx[i, 0], sy[i, 0], sz[i, 0]
            bx, by, bz = sx[i, 1], sy[i, 1], sz[i, 1]
            cx, cy, cz = sx[i, 2], sy[i, 2], sz[i, 2]
            x0 = max(0, int(min(ax_, bx, cx)));  x1 = min(w - 1, int(max(ax_, bx, cx)) + 1)
            y0 = max(0, int(min(ay, by, cy)));   y1 = min(h - 1, int(max(ay, by, cy)) + 1)
            if x0 > x1 or y0 > y1:
                continue
            d = (by - cy) * (ax_ - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-9:
                continue
            ys_, xs_ = np.mgrid[y0:y1 + 1, x0:x1 + 1]
            w0 = ((by - cy) * (xs_ - cx) + (cx - bx) * (ys_ - cy)) / d
            w1 = ((cy - ay) * (xs_ - cx) + (ax_ - cx) * (ys_ - cy)) / d
            w2 = 1 - w0 - w1
            mask = (w0 >= -1e-3) & (w1 >= -1e-3) & (w2 >= -1e-3)
            if not mask.any():
                continue
            z = w0 * az_ + w1 * bz + w2 * cz
            sub = zbuf[y0:y1 + 1, x0:x1 + 1]
            upd = mask & (z < sub)
            if not upd.any():
                continue
            sub[upd] = z[upd]
            img[y0:y1 + 1, x0:x1 + 1][upd] = cols[i]

    # crease/silhouette edges from z-buffer discontinuities
    zc = np.where(np.isinf(zbuf), np.nanmax(np.where(np.isinf(zbuf), np.nan, zbuf)) if np.isfinite(zbuf).any() else 0, zbuf)
    gx = np.abs(np.diff(zc, axis=1, prepend=zc[:, :1]))
    gy = np.abs(np.diff(zc, axis=0, prepend=zc[:1, :]))
    edges = ((gx + gy) > 2.5 / scale * SS) & np.isfinite(zbuf)
    img[edges] = (np.array(img[edges]) * 0.45).astype(np.uint8)
    return Image.fromarray(img)


# ---------------------------------------------------------------- slices
AXIS_IDX = {"x": 0, "y": 1, "z": 2}
# world->2D matrices per axis: consistent with front/left/top view orientations
def slice_frame(axis: int) -> tuple[list[int], list[int]]:
    """returns ([u_axis, v_axis], [u_sign, v_sign]) mapping world axes to slice 2D."""
    if axis == 2:   # Z slice, viewed from above: u=+X, v=+Y
        return [0, 1], [1, 1]
    if axis == 1:   # Y slice, viewed from front (-Y): u=+X, v=+Z
        return [0, 2], [1, 1]
    return [1, 2], [-1, 1]  # X slice, viewed from left (-X): u=-Y, v=+Z


def section_polys(part: Part, axis: int, val: float):
    normal = np.zeros(3); normal[axis] = 1.0
    origin = np.zeros(3); origin[axis] = val
    sec = part.mesh.section(plane_origin=origin, plane_normal=normal)
    if sec is None:
        return []
    (ua, va), (us, vs) = slice_frame(axis)
    M = np.zeros((4, 4)); M[3, 3] = 1.0
    M[0, ua] = us; M[1, va] = vs; M[2, axis] = 1.0; M[2, 3] = -val
    flat, _ = sec.to_2D(to_2D=M)
    try:
        return list(flat.polygons_full)
    except Exception:
        # even-odd assembly from closed rings (nested rings become holes)
        from functools import reduce
        rings = [p for p in flat.polygons_closed if p is not None and p.area > 1e-6]
        if not rings:
            return []
        merged = reduce(lambda a, b: a.symmetric_difference(b), rings)
        geoms = getattr(merged, "geoms", [merged])
        return [g for g in geoms if g.geom_type == "Polygon"]


def render_slice(parts: list[Part], axis: int, val: float, w: int, h: int,
                 bounds: np.ndarray) -> tuple[Image.Image, float]:
    from shapely.geometry import GeometryCollection
    from shapely.ops import unary_union

    img = Image.new("RGB", (w, h), SLICE_BG)
    dr = ImageDraw.Draw(img)
    (ua, va), (us, vs) = slice_frame(axis)
    umin, umax = sorted((bounds[0][ua] * us, bounds[1][ua] * us))
    vmin, vmax = sorted((bounds[0][va] * vs, bounds[1][va] * vs))
    span_u, span_v = max(umax - umin, 1e-6), max(vmax - vmin, 1e-6)
    s = min((w - 70) / span_u, (h - 70) / span_v)
    ox = w / 2 - s * (umin + umax) / 2
    oy = h / 2 + s * (vmin + vmax) / 2

    def px(u: float, v: float) -> tuple[float, float]:
        return ox + s * u, oy - s * v

    # 10mm grid
    def frange(a: float, b: float, step: float):
        x = math.floor(a / step) * step
        while x <= b:
            yield x
            x += step
    for u in frange(umin, umax, 10):
        dr.line([px(u, vmin), px(u, vmax)], fill=GRID_COL)
    for v in frange(vmin, vmax, 10):
        dr.line([px(umin, v), px(umax, v)], fill=GRID_COL)

    shapes = []
    draw_ops = []
    for part in parts:
        polys = section_polys(part, axis, val)
        shapes.append(unary_union(polys) if polys else None)
        draw_ops += [(part, poly) for poly in polys]
    # paint largest-first (by bbox extent) so a part nested in another's hole lands
    # on top: holes are filled with the background, so a later part would otherwise
    # be erased by an enclosing part's hole. Keeps sections order-independent.
    def _extent(poly):
        x0, y0, x1, y1 = poly.bounds
        return (x1 - x0) * (y1 - y0)
    for part, poly in sorted(draw_ops, key=lambda t: _extent(t[1]), reverse=True):
        ext = [px(u, v) for u, v in poly.exterior.coords]
        dr.polygon(ext, fill=part.color, outline=tuple(int(c * 0.55) for c in part.color))
        for hole in poly.interiors:
            dr.polygon([px(u, v) for u, v in hole.coords], fill=SLICE_BG,
                       outline=tuple(int(c * 0.55) for c in part.color))

    # interference: pairwise overlap between different parts' sections
    hit = 0.0
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if shapes[i] is None or shapes[j] is None:
                continue
            inter = shapes[i].intersection(shapes[j])
            if inter.is_empty or inter.area < 0.05:
                continue
            hit += inter.area
            geoms = inter.geoms if isinstance(inter, GeometryCollection) or hasattr(inter, "geoms") else [inter]
            for g in geoms:
                if g.geom_type == "Polygon":
                    dr.polygon([px(u, v) for u, v in g.exterior.coords], fill=INTERFERE)
    # scale bar (20mm)
    bx, by = 16, h - 18
    dr.line([(bx, by), (bx + s * 20, by)], fill=TEXT, width=2)
    dr.text((bx + s * 20 + 6, by - 8), "20 mm", fill=DIM_TEXT, font=font(13))
    return img, hit


# ---------------------------------------------------------------- sheet assembly
def caption(tile: Image.Image, text: str, warn: bool = False) -> Image.Image:
    out = Image.new("RGB", (tile.width, tile.height + CAPTION_H), CAPTION_BG)
    out.paste(tile, (0, 0))
    d = ImageDraw.Draw(out)
    d.text((10, tile.height + 5), text, fill=(255, 120, 110) if warn else TEXT, font=font(15))
    return out


def parse_pos(spec: str, lo: float, hi: float) -> float:
    if spec.endswith("%"):
        return lo + (hi - lo) * float(spec[:-1]) / 100
    return float(spec)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out", default="sheet.png")
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS),
                    help=f"comma list from {list(VIEWS)} (or 'none')")
    ap.add_argument("--view", action="append", default=[], metavar="AZ,EL",
                    help="custom camera angle in degrees; use = for negatives, e.g. --view=-25,15 (repeatable)")
    ap.add_argument("--roi", default=None, metavar="AXIS=LO:HI[,...]",
                    help="crop + zoom every tile to a region, e.g. 'z=40:62' or 'x=25%%:75%%,z=40:62'")
    ap.add_argument("--slice", action="append", default=[], metavar="AXIS=POS",
                    help="cross-section, e.g. z=50%% or y=12.5 (repeatable, comma lists ok)")
    ap.add_argument("--cutaway", action="append", default=[], metavar="AXIS>POS",
                    help="3D cutaway, e.g. 'y>50%%' removes y>mid (repeatable)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--cols", type=int, default=4)
    args = ap.parse_args()

    parts = load_parts(args.files)
    all_pts = np.vstack([p.tris.reshape(-1, 3) for p in parts])
    lo, hi = all_pts.min(axis=0), all_pts.max(axis=0)
    center = (lo + hi) / 2
    bounds = np.array([lo, hi])
    dims = hi - lo

    view_names = [] if args.views == "none" else [v.strip() for v in args.views.split(",") if v.strip()]
    for v in view_names:
        if v not in VIEWS:
            sys.exit(f"unknown view '{v}' (choose from {list(VIEWS)})")

    # optional ROI: crop geometry + zoom every tile (views, cutaways, slices) to it
    roi_lo, roi_hi = lo.copy(), hi.copy()
    if args.roi:
        for term in args.roi.split(","):
            axname, _, rng = term.partition("=")
            ax = AXIS_IDX[axname.strip().lower()]
            a, _, b = rng.partition(":")
            roi_lo[ax] = parse_pos(a.strip(), lo[ax], hi[ax])
            roi_hi[ax] = parse_pos(b.strip(), lo[ax], hi[ax])
        center = (roi_lo + roi_hi) / 2
        bounds = np.array([roi_lo, roi_hi])
    roi_box = (roi_lo, roi_hi) if args.roi else None
    roi_tag = f"  ·  roi {args.roi}" if args.roi else ""

    angle_tiles: list[tuple[str, float, float, str]] = \
        [(name, *VIEWS[name]) for name in view_names]
    for spec in args.view:
        a, e = (float(t) for t in spec.replace(" ", "").split(","))
        angle_tiles.append((f"custom az={a:g}° el={e:g}°", a, e, "+Z up"))

    # one shared scale so every 3D tile is comparable (fit the roi/full bbox corners)
    rw, rh = TILE_W * SS, TILE_H * SS
    corners = np.array([[x, y, z] for x in (roi_lo[0], roi_hi[0])
                        for y in (roi_lo[1], roi_hi[1]) for z in (roi_lo[2], roi_hi[2])])
    scale = None
    for _, az, el, _ in angle_tiles or [("iso", *VIEWS["iso"])]:
        v = (corners - center) @ view_transform(az, el).T
        ext = v.max(axis=0) - v.min(axis=0)
        s = min(rw * 0.86 / max(ext[0], 1e-6), rh * 0.86 / max(ext[1], 1e-6))
        scale = s if scale is None else min(scale, s)

    tiles: list[Image.Image] = []
    for name, az, el, hint in angle_tiles:
        im = render_view(parts, az, el, rw, rh, scale, center, box=roi_box)
        im = im.resize((TILE_W, TILE_H), Image.LANCZOS)
        cam = camera_words(az, el)
        tiles.append(caption(im, f"{name}  ·  {cam}  ·  {hint}{roi_tag}"))

    for spec in args.cutaway:
        s = spec.replace("<", ">@LT@")
        if ">" not in s:
            sys.exit(f"cutaway spec '{spec}' needs axis>pos or axis<pos")
        axname, pos = spec.replace("<", ">").split(">")
        op = "<" if "<" in spec else ">"
        ax = AXIS_IDX[axname.strip().lower()]
        val = parse_pos(pos.strip(), lo[ax], hi[ax])
        im = render_view(parts, *VIEWS["iso"][:2], rw, rh, scale, center,
                         cut=(ax, op, val), box=roi_box)
        im = im.resize((TILE_W, TILE_H), Image.LANCZOS)
        tiles.append(caption(im, f"cutaway {axname}{op}{val:.1f}mm  ·  iso  ·  uncapped{roi_tag}"))

    for spec in args.slice:
        axname, _, poslist = spec.partition("=")
        ax = AXIS_IDX[axname.strip().lower()]
        for pos in poslist.split(","):
            val = parse_pos(pos.strip(), lo[ax], hi[ax])
            pct = 100 * (val - lo[ax]) / max(hi[ax] - lo[ax], 1e-6)
            im, hit = render_slice(parts, ax, val, TILE_W, TILE_H, bounds)
            note = f"  ⚠ INTERFERENCE {hit:.1f}mm²" if hit else ""
            tiles.append(caption(im, f"slice {axname.upper()}={val:.1f}mm ({pct:.0f}%){note}", warn=bool(hit)))

    if not tiles:
        sys.exit("nothing to render (no views, slices, or cutaways)")

    # header: title, overall dims, legend
    cols = max(1, min(args.cols, len(tiles)))
    rows = math.ceil(len(tiles) / cols)
    pad = 12
    header_h = 66
    W = cols * TILE_W + (cols + 1) * pad
    H = header_h + rows * (TILE_H + CAPTION_H) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(sheet)
    title = args.title or " + ".join(p.name for p in parts)
    d.text((pad + 2, 10), title, fill=TEXT, font=font(22, bold=True))
    d.text((pad + 2, 40), f"overall {dims[0]:.1f} × {dims[1]:.1f} × {dims[2]:.1f} mm   (X×Y×Z)",
           fill=DIM_TEXT, font=font(15))
    x = pad + 460
    for p in parts:
        pd = p.tris.reshape(-1, 3)
        pdim = pd.max(axis=0) - pd.min(axis=0)
        d.rectangle([x, 42, x + 13, 55], fill=p.color)
        label = f"{p.name}  {pdim[0]:.0f}×{pdim[1]:.0f}×{pdim[2]:.0f}"
        d.text((x + 19, 41), label, fill=DIM_TEXT, font=font(14))
        x += 19 + int(d.textlength(label, font=font(14))) + 26

    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet.paste(t, (pad + c * (TILE_W + pad), header_h + pad + r * (TILE_H + CAPTION_H + pad)))

    sheet.save(args.out)
    ntris = sum(len(p.tris) for p in parts)
    print(f"wrote {args.out}  ({len(tiles)} tiles, {len(parts)} parts, {ntris} tris)")


if __name__ == "__main__":
    main()
