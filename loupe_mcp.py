# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""loupe MCP server — the review pipeline, exposed to any MCP client.

  draft_check -> assert a dimension spec before any CAD exists (and, with stl, after)
  draft_sheet -> dimensioned drawing + dimension table, returned INLINE as a PNG
  check       -> deterministic geometry report (watertight / interference / walls / overhang)
  sheet       -> labeled contact sheet, returned INLINE as a PNG you can read in the same turn
  slice       -> headless Bambu estimate (time / filament / cost)

Each tool shells out to the matching `uv run <tool>.py`, so the heavy per-tool deps
(trimesh, shapely, Bambu Studio) stay isolated in their own environments and this
server stays light. Wire it into a client with, e.g.:

    claude mcp add loupe -- uv run /ABS/PATH/TO/loupe/loupe_mcp.py

File paths are resolved relative to the server's launch directory (your project root)
unless absolute.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

ROOT = Path(__file__).resolve().parent          # where the tool scripts live
mcp = FastMCP("loupe")


def _run(script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a loupe tool via uv; cwd stays the server's launch dir so relative
    STL paths resolve against the caller's project."""
    return subprocess.run(
        ["uv", "run", str(ROOT / script), *args],
        capture_output=True, text=True,
    )


@mcp.tool()
def check(
    files: list[str],
    interference_max: float | None = None,
    min_wall: float | None = None,
    overhang: float | None = None,
    clearance: str | None = None,
) -> str:
    """Deterministic geometry check — your specification, not a vibe.

    Watertightness is always checked. Add interference_max (mm3 boolean overlap
    between every pair of parts), min_wall (mm), overhang (deg, unsupported area),
    or clearance ("colPart:borePart:gap@z=lo:hi") as needed. The report is always
    returned; the final line is `PASS` or `FAIL:` — gate your build on it.
    """
    args = list(files)
    if interference_max is not None:
        args += ["--interference-max", str(interference_max)]
    if min_wall is not None:
        args += ["--min-wall", str(min_wall)]
    if overhang is not None:
        args += ["--overhang", str(overhang)]
    if clearance:
        args += ["--clearance", clearance]
    r = _run("check.py", args)
    return (r.stdout + r.stderr).strip() or "(no output)"


@mcp.tool()
def sheet(
    files: list[str],
    views: str | None = None,
    slices: list[str] | None = None,
    cutaways: list[str] | None = None,
    view: str | None = None,
    roi: str | None = None,
) -> Image:
    """Render a labeled contact sheet and return it INLINE as a PNG to read now.

    views: comma list ("iso,front,top") or "none". slices: e.g. ["z=50%", "x=12"].
    cutaways: e.g. ["y<50%"] (removes y<mid; opens toward a front-right-above camera).
    view: "AZ,EL" custom camera (negatives fine here — passed as one token).
    roi: "z=lo:hi[,x=..]" zooms every tile onto a region.
    Interference between parts is painted red with the area in the tile caption.
    """
    out = Path(tempfile.mkstemp(suffix=".png")[1])
    try:
        args = list(files) + ["-o", str(out)]
        if views is not None:
            args += ["--views", views]
        for s in slices or []:
            args += ["--slice", s]
        for c in cutaways or []:
            args += ["--cutaway", c]
        if view:
            args += [f"--view={view}"]          # = form so a leading '-' isn't eaten
        if roi:
            args += ["--roi", roi]
        r = _run("sheet.py", args)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"sheet.py produced no image:\n{r.stdout}\n{r.stderr}".strip())
        return Image(data=out.read_bytes(), format="png")
    finally:
        out.unlink(missing_ok=True)


@mcp.tool()
def draft_check(spec: str, stl: list[str] | None = None) -> str:
    """Assert a draft spec — the planning-stage gate, before any CAD exists.

    Checks that every dimension expression resolves, that features sit inside their
    outline and hold the declared min_wall, that the `checks:` expressions hold, and
    that declared fits land in their clearance class. Pass `stl` to also assert an
    exported mesh still matches the draft (bbox, and drafted holes present at the
    right diameter and position). Final line is `PASS` or `FAIL:` — gate on it.
    """
    args = [spec]
    if stl:
        args += ["--stl", *stl]
    r = _run("draftcheck.py", args)
    return (r.stdout + r.stderr).strip() or "(no output)"


@mcp.tool()
def draft_sheet(spec: str, views: str | None = None) -> Image:
    """Render a draft's dimensioned drawing and return it INLINE as a PNG to read now.

    Each view is drawn with real extension lines, arrowheads, ⌀/R callouts and ±
    tolerances, beside a table of every dimension with its value, tolerance and
    provenance. Dimensions are measured off the resolved geometry, so an expression
    that evaluates wrong shows a wrong number on the drawing.
    """
    out = Path(tempfile.mkstemp(suffix=".png")[1])
    try:
        args = [spec, "-o", str(out)]
        if views:
            args += ["--views", views]
        r = _run("draftsheet.py", args)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"draftsheet.py produced no image:\n{r.stdout}\n{r.stderr}".strip())
        return Image(data=out.read_bytes(), format="png")
    finally:
        out.unlink(missing_ok=True)


@mcp.tool(name="slice")
def slice_(
    files: list[str],
    process: str | None = None,
    filament: str | None = None,
) -> str:
    """Headless Bambu Studio estimate: success/error, print time, filament
    grams/meters, spool cost, per-object boxes, slicer warnings. Requires a
    local Bambu Studio install. Writes the .gcode.3mf next to the inputs.
    """
    args = list(files)
    if process:
        args += ["--process", process]
    if filament:
        args += ["--filament", filament]
    r = _run("slice.py", args)
    return (r.stdout + r.stderr).strip() or "(no output)"


if __name__ == "__main__":
    mcp.run()
