#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "pypdfium2"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Contact sheet for PCB review — raytraced 3D views + true layer plots.

Renders a .kicad_pcb into one labeled PNG: 3D top/bottom/iso (kicad-cli's
raytracer, real soldermask + silkscreen + component models) and 2D layer
plots (copper front/back, silk+mask) rasterized from KiCad's own PDF plotter,
so what you see is what the fab gets. Every tile is captioned. Read the PNG.

Usage:
  uv run pcbsheet.py out/blinky.kicad_pcb -o out/blinky_sheet.png
  uv run pcbsheet.py out/blinky.kicad_pcb --no-3d          # layer plots only (faster)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

from pcb import kicad_cli  # noqa: E402  (zero-dep sibling)

# ---- style, matched to sheet.py
BG = (24, 26, 31)
TILE_BG = (30, 33, 39)
CAPTION_BG = (42, 46, 54)
TEXT = (214, 218, 224)
DIM_TEXT = (150, 156, 166)
TILE_W, TILE_H = 560, 460
CAPTION_H = 26
HEADER_H = 34


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in ["/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True)


def render3d(board: Path, out: Path, side: str, rotate: str | None,
             zoom: float) -> bool:
    base = [kicad_cli(), "pcb", "render", board, "-o", out,
            "--width", "1120", "--height", "880", "--quality", "high",
            "--background", "opaque", "--side", side, "--zoom", str(zoom)]
    for cmd in ([base + ["--rotate", rotate]] if rotate else []) + [base]:
        r = run(cmd)
        if r.returncode == 0 and out.exists():
            return True
    sys.stderr.write(f"3D render failed ({side}): {r.stdout}{r.stderr}\n")
    return False


def plot2d(board: Path, layers: str, out_png: Path, mirror: bool = False) -> bool:
    """kicad-cli PDF plot of `layers` -> autocropped PNG (fab ground truth)."""
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "plot.pdf"
        cmd = [kicad_cli(), "pcb", "export", "pdf", board, "-o", pdf,
               "--layers", layers]
        if mirror:
            cmd.append("--mirror")
        r = run(cmd)
        if r.returncode != 0 or not pdf.exists():
            sys.stderr.write(f"pdf plot failed ({layers}): {r.stdout}{r.stderr}\n")
            return False
        page = pdfium.PdfDocument(str(pdf))[0]
        img = page.render(scale=8.0).to_pil().convert("RGB")
        # autocrop to drawn content (page is mostly white)
        bbox = ImageOps.invert(img.convert("L")).getbbox()
        if bbox:
            pad = 40
            bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                    min(img.width, bbox[2] + pad), min(img.height, bbox[3] + pad))
            img = img.crop(bbox)
        img.save(out_png)
        return True


def tile(img_path: Path | None, caption: str, hint: str) -> Image.Image:
    t = Image.new("RGB", (TILE_W, TILE_H + CAPTION_H), TILE_BG)
    d = ImageDraw.Draw(t)
    if img_path and img_path.exists():
        img = Image.open(img_path).convert("RGB")
        img.thumbnail((TILE_W - 8, TILE_H - 8), Image.LANCZOS)
        t.paste(img, ((TILE_W - img.width) // 2, (TILE_H - img.height) // 2))
    else:
        d.text((TILE_W // 2, TILE_H // 2), "render failed", fill=DIM_TEXT,
               font=font(16), anchor="mm")
    d.rectangle([0, TILE_H, TILE_W, TILE_H + CAPTION_H], fill=CAPTION_BG)
    d.text((8, TILE_H + CAPTION_H // 2), caption, fill=TEXT, font=font(13), anchor="lm")
    d.text((TILE_W - 8, TILE_H + CAPTION_H // 2), hint, fill=DIM_TEXT,
           font=font(12), anchor="rm")
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--no-3d", action="store_true", help="skip raytraced views")
    ap.add_argument("--zoom", type=float, default=1.0, help="3D zoom factor")
    args = ap.parse_args()
    out_png = args.output or args.board.with_name(args.board.stem + "_sheet.png")

    # board stats from the pcb.py spec, if it lives next door
    stats = ""
    specp = args.board.with_name(args.board.stem + ".pcbspec.json")
    if specp.exists():
        s = json.loads(specp.read_text())
        stats = (f"{s['w']}×{s['h']}mm · {s['layers']}L · "
                 f"{len(s['parts'])} parts · {len(s['nets'])} nets")

    tiles: list[Image.Image] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if not args.no_3d:
            jobs3d = [("top", None, "3D · top", "raytraced"),
                      ("bottom", None, "3D · bottom", "raytraced"),
                      ("top", "-35,0,25", "3D · iso", "raytraced")]
            for i, (side, rot, cap, hint) in enumerate(jobs3d):
                p = tmp / f"r{i}.png"
                render3d(args.board, p, side, rot, args.zoom)
                tiles.append(tile(p if p.exists() else None, cap, hint))
        jobs2d = [("F.Cu,Edge.Cuts", False, "F.Cu + edge", "plot · from front"),
                  ("B.Cu,Edge.Cuts", False, "B.Cu + edge", "plot · from front"),
                  ("F.SilkS,F.Mask,Edge.Cuts", False, "F.Silk + F.Mask", "plot · from front")]
        for i, (layers, mirror, cap, hint) in enumerate(jobs2d):
            p = tmp / f"p{i}.png"
            plot2d(args.board, layers, p, mirror)
            tiles.append(tile(p if p.exists() else None, cap, hint))

        cols = 3
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * TILE_W + (cols + 1) * 8,
                                  HEADER_H + rows * (TILE_H + CAPTION_H + 8) + 8), BG)
        d = ImageDraw.Draw(sheet)
        d.text((10, HEADER_H // 2), args.board.stem, fill=TEXT, font=font(18), anchor="lm")
        if stats:
            d.text((sheet.width - 10, HEADER_H // 2), stats, fill=DIM_TEXT,
                   font=font(14), anchor="rm")
        for i, t in enumerate(tiles):
            x = 8 + (i % cols) * (TILE_W + 8)
            y = HEADER_H + (i // cols) * (TILE_H + CAPTION_H + 8)
            sheet.paste(t, (x, y))
        sheet.save(out_png)
    print(f"{out_png}  ({sheet.width}×{sheet.height}, {len(tiles)} tiles)")


if __name__ == "__main__":
    main()
