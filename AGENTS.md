# AGENTS.md — driving loupe as an agent

loupe is a code-CAD **review** pipeline. You edit a parametric model (build123d / CadQuery),
then use these four `uv run` scripts to *prove* it before it prints. You have two things a
render alone can't give you: **machine-checkable geometry facts** and **an image you can read**.

## The loop

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

## MCP

`loupe_mcp.py` exposes `check`, `sheet`, and `slice` over MCP — and **`sheet` returns the contact sheet
inline as an image**, so you see the render in the same call instead of writing a file and reading it back.
Wire it up once:

```sh
claude mcp add loupe -- uv run /ABS/PATH/TO/loupe/loupe_mcp.py
```

Then call `loupe.sheet(files=[...], slices=["z=50%"])` and read the returned image directly. Paths are
resolved relative to the directory the server was launched in (usually your project root).
