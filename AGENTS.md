# AGENTS.md — driving loupe as an agent

loupe is a code-CAD **review** pipeline. You edit a parametric model (build123d / CadQuery),
then use these `uv run` scripts to *prove* it before it prints. You have two things a
render alone can't give you: **machine-checkable geometry facts** and **an image you can read**.

## The planning loop (before you write any CAD)

```
1. write a draft: a YAML dimension record — named dims with tolerance and provenance,
   plus simple 2D profiles that reference them
2. GATE:  uv run draftcheck.py part.draft.yaml
          -> nonzero = unresolvable expression, feature outside the outline, wall under
             min_wall, a false check, a fit outside its class. Fix it before modeling.
3. LOOK:  uv run draftsheet.py part.draft.yaml -o draft.png   then Read draft.png
4. the CAD script reads the draft (`from draft import load`) or a params export
   (`uv run draft.py part.draft.yaml --params dims.json`) — never retype the numbers
5. after export, close the loop:
   uv run draftcheck.py part.draft.yaml --stl asm_part.stl
```

Why this exists: `check.py` proves the model you built, but it cannot tell you that you built
the *wrong* model. A wall that's 1.8 mm when you meant 2.4 mm is watertight, collision-free,
and wrong. The draft is the record that makes that a failing assertion instead of a surprise.

**The draft is the single source of truth.** If a number appears in both the draft and the CAD
script, you have already lost — import it.

## The 3D loop

```
1. edit the CAD script, export STLs (individual parts + an assembled `asm_*` set)
2. GATE:   uv run check.py asm_*.stl --interference-max 0.05 --min-wall 1.2
           -> nonzero exit = a real defect. Fix it before you render. Do not skip.
3. LOOK:   uv run sheet.py asm_*.stl --slice z=50% -o sheet.png   then Read sheet.png
4. iterate until check passes and the sheet looks right
5. (optional) uv run slice.py part_a.stl part_b.stl   -> time / filament / cost
```

Rule of thumb: **`check.py` is your specification, `sheet.py` is your eyes.** "Looks right" is
not a spec — gate on the exit code, use the image to understand *why* it failed.

## The tools

- **`draft.py <spec.yaml> [--info] [--params dims.json|dims.py]`**
  Loads and resolves a draft. `--params` writes the resolved dim table for the CAD script to
  import; `--info` prints it with tolerances and provenance.
- **`draftcheck.py <spec.yaml> [--stl FILE...] [--json]`**
  The planning gate: expression resolution, containment, 2D min-wall, `checks:`, fit classes,
  and — with `--stl` — bbox and hole diameter/position against the exported mesh.
  **Exits nonzero on a failed assertion.**
- **`draftsheet.py <spec.yaml> [-o out.png] [--views a,b] [--dxf out.dxf]`**
  Dimensioned drawing (extension lines, arrowheads, ⌀/R callouts, ± tolerances) plus a table
  of every dim with its value, tolerance and provenance. **Read the PNG.**
- **`check.py <stls> [--interference-max MM3] [--min-wall MM] [--overhang DEG] [--clearance "a:b:gap@z=lo:hi"]`**
  Watertightness always; interference (exact boolean volume), thin-wall, unsupported-overhang,
  region-scoped clearance on request. **Exits nonzero on a failed assertion** — wire it into your gate.
- **`sheet.py <stls> [-o out.png] [--views ...] [--slice z=50%] [--cutaway "y>50%"] [--roi "z=a:b"] [--view=AZ,EL]`**
  One labeled PNG: 8 named views + slices (part-vs-part interference painted red, mm² in caption)
  + 3D cutaways + region zoom. Every tile is captioned with camera + screen axes. **Read the PNG.**
- **`viewer.py <stls> -o preview.html`** — self-contained browser viewer. This is for a **human**;
  don't render it expecting to read it yourself.
- **`slice.py <stls> [--process ...] [--filament ...]`** — headless Bambu Studio: time, grams, cost.
  Needs a local Bambu Studio install (macOS paths by default).

## Draft gotchas

- **Model in the draft's frame.** The as-built gate compares hole coordinates literally, so a
  part whose corner is at the origin in the draft must be built that way too (`align=Align.MIN`,
  not build123d's centred default). Same rule the PCB half already states.
- **A dim is a literal, an expression string, or a mapping**: `body_h: 34`, `neck_r: "bore_d/2 + wall"`,
  or `{v: 2.4, tol: 0.1, from: "calipers, n=5"}`. Use `{expr: ..., tol: ...}` to put a tolerance on
  a derived dim. `from:` is where the number came from — fill it in; it's the whole point.
- **Features are voids.** `min_wall` measures the material *between* them and to the outline edge,
  which is what the printer has to build. Two features 0.4 mm apart is a failing wall, not a gap.
- **Comparisons are epsilon-tolerant** (1e-9 relative). `floor >= 3*0.4` is true for a 1.2 mm floor
  even though `3*0.4` is 1.2000000000000002. Don't write defensive slop into your checks.
- **Only circular features are checked as-built.** Slots and polys are drawn and wall-checked but
  not measured back out of the mesh; `holes:` cuts one section, so features must lie in that plane.
- **`--stl` matches on file stem**: an `asbuilt:` key of `asm_case` needs `asm_case.stl`.

## Gotchas that will waste your tokens

- **Model in assembled coordinates**, export both individual parts *and* an `asm_*` set. `check.py`
  and `sheet.py` do interference across whatever files you hand them — they can't align parts for you.
- **Negative azimuth needs the `=` form:** `--view=-25,12`, never `--view -25,12` (argparse eats the dash).
- **Interference vs. a contact sliver:** surfaces that touch by design flag a few mm²/mm³. Judge by the
  reported magnitude — real collisions are 10–100× bigger. Scope clearance with `@z=lo:hi` when parts
  legitimately touch elsewhere.
- **A multi-start thread's axial shift *is* a rotation.** Moving a helical part `dz` along its axis
  without also rotating it by `360·dz/lead` produces phantom interference. Applies to through-bores too.
- **Cutaways** cut toward whatever you keep: `z>50%` removes the top. To reveal *toward the camera* in the
  default iso (front-right-above), remove the near side — `y<50%` (front) / `z>50%` (top) / `x>50%` (right).
- **Loading STLs elsewhere?** trimesh needs `process=True` or CAD-exported STLs aren't `is_volume`
  (booleans refuse triangle soup); `polygons_full` drops holes unless **rtree** is installed.

## The PCB loop

```
1. edit the board script (imports pcb.py: parts, nets, route polylines, zones)
2. BUILD:  uv run my_board.py            -> .kicad_pcb via KiCad's bundled Python
3. GATE:   uv run pcbcheck.py out/my_board.kicad_pcb
           -> nonzero = DRC error / unrouted net / netless pad. Fix before rendering.
4. LOOK:   uv run pcbsheet.py out/my_board.kicad_pcb   then Read the PNG
5. FAB:    uv run pcbfab.py out/my_board.kicad_pcb --mesh
           -> gerbers.zip + JLC BOM/CPL + STEP + STL (the STL joins the 3D loop above)
```

- **`pcbcheck.py` is your specification, `pcbsheet.py` is your eyes** — same doctrine as 3D.
  The sheet catches what DRC can't: a corner arc sweeping the wrong way, a part on the
  wrong side, silk crowding a connector. Actually read it.
- Coordinates: **origin lower-left, +Y up, mm** — build123d's frame, not KiCad's y-down.
  Rotations CCW. The driver handles the flip; never pre-flip anything yourself.
- Route points are pads `("R1", 2)` or absolute `(x, y)`; pad refs resolve to true pad
  centers from the loaded footprint, so route pad-to-pad and only add elbows as bare tuples.
- Every pad must be in a `net()` or declared `nc()` — netless pads fail the gate (they'd
  silently skip connectivity checking otherwise).
- Footprint names are `LibName:FootprintName` from KiCad's bundled libs
  (`ls "$(uv run pcb.py --info | grep footprints | cut -d: -f2- | xargs)"` to browse; e.g.
  `LED_SMD:LED_0603_1608Metric`). A missing footprint is a build error, not a warning.
- Pad-1 conventions matter: chip R/C/LED pad 1 is the LEFT pad at rot=0; LED pad 1 = cathode.
- Silk sits ~0.15mm proud in DRC's eyes — keep text ≥1mm from the outline or eat warnings.

### pcbnew driver gotchas (already handled inside pcb.py — don't re-fight these)

- ZONE_FILLER on a `CreateEmptyBoard()` board **segfaults** (KiCad 10.0.4): the driver
  saves, reloads (attaches a project), then fills. Also: never drop the last Python ref
  to a BOARD mid-build — its SWIG destructor runs immediately and corrupts the heap.
- `board.GetLayerID("F.SilkS")` returns −1 in KiCad 10 (wants `F.Silkscreen`) and pcbnew
  will happily write `(layer "UNDEFINED")` → an **unparseable board file**. The driver
  aliases legacy names and hard-fails unknown ones.
- An NPTH pad with an empty `(layers)` list is also a parse error on reload — the driver
  uses `PAD.UnplatedHoleMask()`.
- Corner arcs are built from **three points** (`SetArcGeometry`) — every angle-sign
  convention was tried and every one was wrong in some view.
- No `wx.App` in the driver — `wx.DisableAsserts()` alone is enough, and an App bounces
  a dock icon (and crash dialogs at the user) on every build.

## MCP

`loupe_mcp.py` exposes `draft_check`, `draft_sheet`, `check`, `sheet`, and `slice` over MCP — and
**`sheet` and `draft_sheet` return their image inline**, so you see the render in the same call
instead of writing a file and reading it back.
Wire it up once:

```sh
claude mcp add loupe -- uv run /ABS/PATH/TO/loupe/loupe_mcp.py
```

Then call `loupe.sheet(files=[...], slices=["z=50%"])` and read the returned image directly. Paths are
resolved relative to the directory the server was launched in (usually your project root).
