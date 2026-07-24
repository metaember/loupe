#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "shapely", "ezdxf", "matplotlib", "pillow"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Dimensioned drawing sheet for a draft spec — the eyes for the planning stage.

Renders each view as a real technical drawing (extension lines, arrowheads, ⌀/R
callouts, stacked ± tolerances) and pins a dimension table beside it, so one PNG
carries both the picture and the numbers that produced it — including where each
number came from. Dimensions are measured off the *resolved geometry*, so an
expression that evaluates wrong shows up as a wrong number on the drawing.

  uv run draftsheet.py part.draft.yaml -o draft.png
  uv run draftsheet.py part.draft.yaml --views top --dxf top.dxf

Read the PNG.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                        # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext               # noqa: E402
from ezdxf.addons.drawing.config import ColorPolicy, Configuration     # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend          # noqa: E402
from PIL import Image, ImageDraw, ImageFont                            # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from draft import Draft, DraftError, View, load                        # noqa: E402

# chrome matched to sheet.py so a draft sheet and a contact sheet read as one family
BG = (24, 26, 31)
CAPTION_BG = (42, 46, 54)
TABLE_BG = (30, 33, 39)
TEXT = (214, 218, 224)
DIM_TEXT = (150, 156, 166)
ACCENT = (90, 196, 184)
PAPER = (255, 255, 255)

TILE_W, TILE_H = 720, 560
CAPTION_H = 26
PAD = 12


def font(size: int, bold: bool = False):
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except OSError:
            continue
    return ImageFont.load_default()


def mono(size: int):
    for p in ("/System/Library/Fonts/Menlo.ttc",
              "/System/Library/Fonts/Monaco.ttf",
              "/System/Library/Fonts/Courier.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- DXF construction
def _dimstyle(doc, scale: float) -> None:
    """Sized for the part, not for the page. ezdxf's stock EZDXF style annotates in
    centimetres (dimlfac=100) with architectural ticks — both wrong for mm mechanical."""
    ds = doc.dimstyles.duplicate_entry("EZDXF", "LOUPE")
    for k, v in dict(dimlfac=1.0, dimscale=1.0, dimtxt=2.2 * scale, dimasz=2.0 * scale,
                     dimexe=1.0 * scale, dimexo=0.8 * scale, dimgap=0.6 * scale,
                     dimdec=2, dimtdec=2, dimzin=0, dimblk="", dimblk1="", dimblk2="",
                     dimtsz=0.0, dimtad=1).items():
        ds.set_dxf_attrib(k, v)


def _poly(msp, geom, layer: str) -> None:
    msp.add_lwpolyline(list(geom.exterior.coords), close=True, dxfattribs={"layer": layer})
    for ring in geom.interiors:
        msp.add_lwpolyline(list(ring.coords), close=True, dxfattribs={"layer": layer})


def _add_dim(draft: Draft, msp, view: View, spec: dict, scale: float) -> None:
    feats = {f.id: f for f in view.features}
    n = draft.num

    def tolerance(d, ref):
        if ref and ref in draft.dims and draft.dims[ref].tol:
            up, lo = draft.dims[ref].tol
            d.set_tolerance(up, lo)

    if "linear" in spec:
        (x1, y1), (x2, y2) = [(n(p[0]), n(p[1])) for p in spec["linear"]]
        off = n(spec.get("offset", -8))
        horizontal = abs(y2 - y1) <= abs(x2 - x1)
        angle = 0.0 if horizontal else 90.0
        base = (x1, y1 + off) if horizontal else (x1 + off, y1)
        d = msp.add_linear_dim(base=base, p1=(x1, y1), p2=(x2, y2), angle=angle,
                               dimstyle="LOUPE",
                               text=spec.get("label") or "<>")
        tolerance(d, spec.get("tol_from"))
        d.render()
        return

    for kind, factory in (("diameter", "add_diameter_dim"), ("radius", "add_radius_dim")):
        if kind not in spec:
            continue
        fid = str(spec[kind])
        f = feats.get(fid)
        if f is None or f.radius is None:
            raise DraftError(f"view {view.name!r}: {kind} dim references {fid!r}, "
                             "which is not a circular feature")
        # dimtoh/dimtih keep the callout text horizontal instead of rotating it
        # along the leader — a ⌀ callout has to stay readable at tile scale.
        d = getattr(msp, factory)(center=f.center, radius=f.radius,
                                  angle=n(spec.get("angle", 135)), dimstyle="LOUPE",
                                  override={"dimtofl": 1, "dimtoh": 1, "dimtih": 1})
        tolerance(d, spec.get("tol_from"))
        d.render()
        return

    if "note" in spec:
        if "at" in spec:
            at = (n(spec["at"][0]), n(spec["at"][1]))
        elif "of" in spec and str(spec["of"]) in feats:
            f = feats[str(spec["of"])]
            at = (f.geom.centroid.x, f.geom.centroid.y)
        else:
            raise DraftError(f"view {view.name!r}: a note needs 'at' or 'of'")
        msp.add_text(str(spec["note"]), height=2.0 * scale,
                     dxfattribs={"insert": at, "layer": "NOTES"})
        return

    raise DraftError(f"view {view.name!r}: a dim needs one of "
                     f"linear / diameter / radius / note, got keys {sorted(spec)}")


def build_dxf(draft: Draft, view: View):
    xmin, ymin, xmax, ymax = view.outline.bounds
    # Annotation is sized as a fraction of the part, not in absolute mm: the tile is
    # always fitted to the extent, so this keeps text the same *visual* size whether
    # the part is a 20 mm coupon or a 200 mm panel.
    scale = max(xmax - xmin, ymax - ymin) / 60.0
    doc = ezdxf.new(setup=True)
    _dimstyle(doc, scale)
    for name in ("OUTLINE", "FEATURES", "NOTES"):
        if name not in doc.layers:
            doc.layers.add(name)
    msp = doc.modelspace()

    _poly(msp, view.outline, "OUTLINE")
    for f in view.features:
        if f.kind == "circle":
            msp.add_circle(f.center, radius=f.radius, dxfattribs={"layer": "FEATURES"})
        else:
            _poly(msp, f.geom, "FEATURES")
    for spec in view.dims:
        _add_dim(draft, msp, view, spec, scale)
    return doc


def render_dxf(doc, w: int, h: int) -> Image.Image:
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.96])
    try:
        Frontend(RenderContext(doc), MatplotlibBackend(ax),
                 config=Configuration(color_policy=ColorPolicy.BLACK)
                 ).draw_layout(doc.modelspace(), finalize=True)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor="white")
    finally:
        plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB").resize((w, h), Image.LANCZOS)


# ---------------------------------------------------------------- sheet assembly
def caption(tile: Image.Image, text: str) -> Image.Image:
    out = Image.new("RGB", (tile.width, tile.height + CAPTION_H), CAPTION_BG)
    out.paste(tile, (0, 0))
    ImageDraw.Draw(out).text((8, tile.height + CAPTION_H // 2), text,
                             font=font(13), fill=TEXT, anchor="lm")
    return out


def view_tile(draft: Draft, view: View) -> Image.Image:
    img = render_dxf(build_dxf(draft, view), TILE_W, TILE_H)
    xmin, ymin, xmax, ymax = view.outline.bounds
    return caption(img, f"{view.name}  ({view.plane})   extent "
                        f"{xmax - xmin:.4g} × {ymax - ymin:.4g} {draft.units}   "
                        f"{len(view.features)} feature(s)")


def _fit(dr: ImageDraw.ImageDraw, text: str, fnt, width: int) -> str:
    """Truncate to a pixel width, with an ellipsis — never mid-column clipping."""
    if dr.textlength(text, font=fnt) <= width:
        return text
    while text and dr.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text + "…"


def table_tile(draft: Draft, views: list[View], w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), TABLE_BG)
    dr = ImageDraw.Draw(img)
    fh, ft, fb = mono(13), font(13), font(13, bold=True)
    y = PAD
    dr.text((PAD, y), "DIMENSIONS", font=fb, fill=ACCENT); y += 22
    cols = (PAD, PAD + 130, PAD + 215, PAD + 288)
    src_w = w - PAD - cols[3]
    for label, x in zip(("name", "value", "tol", "source / expression"), cols):
        dr.text((x, y), label, font=ft, fill=DIM_TEXT)
    y += 18
    dr.line((PAD, y, w - PAD, y), fill=(60, 65, 75)); y += 8

    for name, d in draft.dims.items():
        if y > h - 24:
            dr.text((PAD, y), "…", font=fh, fill=DIM_TEXT)
            break
        src = ("= " + d.expr) if d.derived else (d.source or "")
        for text, x, col in ((name, cols[0], TEXT),
                             (f"{d.value:.4g}", cols[1], TEXT),
                             (d.tol_text(), cols[2], DIM_TEXT),
                             (_fit(dr, src, fh, src_w), cols[3], ACCENT if d.derived else DIM_TEXT)):
            if text:
                dr.text((x, y), text, font=fh, fill=col)
        y += 18

    feats = [(v.name, f) for v in views for f in v.features]
    if feats and y < h - 60:
        y += 12
        dr.text((PAD, y), "FEATURES", font=fb, fill=ACCENT); y += 22
        for vn, f in feats:
            if y > h - 24:
                dr.text((PAD, y), "…", font=fh, fill=DIM_TEXT)
                break
            size = f"⌀{2 * f.radius:.4g}" if f.kind == "circle" and f.radius else f.kind
            dr.text((PAD, y), f"{vn}.{f.id}", font=fh, fill=TEXT)
            dr.text((cols[1], y), size, font=fh, fill=TEXT)
            if f.note:
                dr.text((cols[2], y), _fit(dr, f.note, fh, w - PAD - cols[2]),
                        font=fh, fill=DIM_TEXT)
            y += 18

    if y < h - 40:
        y += 12
        if draft.min_wall is not None:
            dr.text((PAD, y), f"min_wall  {draft.min_wall:g} {draft.units}",
                    font=fh, fill=DIM_TEXT)
            y += 18
        for f in draft.fits:
            if y > h - 24:
                break
            dr.text((PAD, y), f"fit  {f.get('name', '?')}  ({f.get('class', '?')})",
                    font=fh, fill=DIM_TEXT)
            y += 18
    return img


def build_sheet(draft: Draft, views: list[View]) -> Image.Image:
    tiles = [view_tile(draft, v) for v in views]
    tw, th = tiles[0].width, tiles[0].height
    cols = min(2, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    table_w = 560
    head_h = 46

    W = PAD + cols * (tw + PAD) + table_w + PAD
    H = head_h + rows * (th + PAD) + PAD
    sheet = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(sheet)
    dr.text((PAD, head_h // 2), f"{draft.name}", font=font(20, bold=True),
            fill=TEXT, anchor="lm")
    dr.text((W - PAD, head_h // 2),
            f"draft · {draft.path.name} · all dimensions {draft.units}",
            font=font(13), fill=DIM_TEXT, anchor="rm")

    for i, t in enumerate(tiles):
        x = PAD + (i % cols) * (tw + PAD)
        y = head_h + (i // cols) * (th + PAD)
        sheet.paste(t, (x, y))
    sheet.paste(table_tile(draft, views, table_w, H - head_h - PAD),
                (PAD + cols * (tw + PAD), head_h))
    return sheet


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a dimensioned drawing sheet from a draft spec.")
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="draft.png")
    ap.add_argument("--views", help="comma list of views to draw (default: all)")
    ap.add_argument("--dxf", metavar="OUT.dxf",
                    help="also write the drawing as DXF (one view; opens in any CAD tool)")
    a = ap.parse_args()

    try:
        draft = load(a.spec)
        if not draft.views:
            raise DraftError("this draft has no views: — nothing to draw")
        names = [s.strip() for s in a.views.split(",")] if a.views else list(draft.views)
        for nm in names:
            if nm not in draft.views:
                raise DraftError(f"unknown view {nm!r} (have: {', '.join(draft.views)})")
        views = [draft.views[nm] for nm in names]
        sheet = build_sheet(draft, views)
        if a.dxf:
            build_dxf(draft, views[0]).saveas(a.dxf)
    except DraftError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)

    sheet.save(a.out)
    print(f"wrote {a.out}  ({sheet.width}×{sheet.height}, "
          f"{len(views)} view(s), {len(draft.dims)} dims)"
          + (f" and {a.dxf}" if a.dxf else ""))


if __name__ == "__main__":
    main()
