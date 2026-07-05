# loupe

**A code-CAD review pipeline for 3D printing: render it, prove it, preview it, slice it.**

Four single-file [`uv`](https://docs.astral.sh/uv/) scripts — zero setup, inline dependencies — that turn a parametric CAD model into something you (or an LLM agent) can actually *trust* before committing filament to it:

```
CAD script (build123d / CadQuery)
   → check.py    assert geometry facts — FAILS LOUDLY if a part floats, collides, or has a thin wall
   → sheet.py    a labeled contact sheet: 8 named views + slices + interference painted red
   → viewer.py   a self-contained browser viewer: orbit, clip planes, explode, mm grid
   → slice.py    headless Bambu Studio: errors, print time, filament grams, cost — then print
```

A loupe is the glass a jeweler holds to a gem, a printer to a proof sheet — the tool for catching the flaw under magnification before it costs you. These scripts are that glass for parametric CAD: they put the part under scrutiny before the printer ever does.

---

## Why this exists

The reliable way to do CAD with code in 2026 isn't GUI-automation-over-a-protocol, and it isn't text-to-CAD. It's three things working together:

1. **Code-CAD** — parametric Python (build123d / CadQuery, both on the OpenCascade kernel). The model is a program: diffable, versionable, reproducible.
2. **Deterministic geometric verification** — machine-checkable assertions a render can *never* guarantee (does part A actually collide with part B? is any wall under 1.2 mm? is the mesh watertight?). Exits nonzero when a fact is false.
3. **Rendered eyes** — a labeled contact sheet you look at, and a browser viewer a human opines on.

This pipeline was built during a real reverse-engineering job — modeling a part to interoperate with a commercial product. On its first day it caught **four** genuine assembly bugs (a body floating 53 mm off the floor, a 1 mm bore offset masquerading as thread interference, a hardcoded floor constant, a phantom interference from an un-rotated helical part) *before a single render existed*, and the printed test parts fit the commercial reference on the first try.

It's written to be driven by a human at a terminal **or** by an AI coding agent — the contact sheet exists precisely because an agent can `Read` a PNG, and the deterministic checks exist because "looks right" is not a specification.

---

## Requirements

- **[uv](https://docs.astral.sh/uv/)** — that's it for the first three tools. Each script declares its own dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)); `uv run` resolves them into a throwaway environment on first invocation. No `pip install`, no virtualenv to manage.
- **Python ≥ 3.11** (uv will fetch one if you don't have it).
- **`slice.py` only:** a local **Bambu Studio** install. Paths default to macOS (`/Applications/BambuStudio.app`); adjust the two constants at the top for Linux/Windows. The other three tools are cross-platform.

```sh
git clone https://github.com/<you>/loupe.git
cd loupe
uv run check.py your_part.stl        # first run resolves deps; subsequent runs are instant
```

---

## The tools

### `check.py` — deterministic geometry verifier

Machine-checkable facts renders can't guarantee. Reports always; exits nonzero on a failed assertion, so you can wire it straight into a build.

```sh
uv run check.py part.stl                                          # watertightness + report only
uv run check.py asm_body.stl asm_lid.stl --interference-max 0.05  # exact 3D boolean overlap volume, mm³
uv run check.py col.stl bore.stl --clearance "col:bore:0.15@z=2:18"  # min gap, region-scoped
uv run check.py part.stl --min-wall 1.2 --overhang 45 --json      # thin-wall + unsupported-area report
```

- **Interference** = exact manifold boolean volume between every pair of parts, always reported.
- **Clearance** samples part A's surface and measures signed distance to part B (negative = penetration). Scope it with `@z=lo:hi` — parts that legitimately touch elsewhere (a flange resting on a rim) otherwise read 0.
- **Watertightness** is always checked. `--min-wall` is an inward ray-cast; `--overhang DEG` reports unsupported area (the bed face is excluded).

### `sheet.py` — labeled contact sheet (the reviewer's eyes)

Renders one or more meshes into a single PNG: 8 named orthographic/iso views + custom camera angles, 2D cross-section slices with **automatic part-vs-part interference detection** (overlap painted red, mm² in the caption), 3D cutaways, and region zoom. Every tile is captioned with its camera position and screen axes — trust the captions, not your assumptions.

```sh
uv run sheet.py body.stl lid.stl -o sheet.png
uv run sheet.py a.stl b.stl --slice z=50% --slice z=12,15 --slice x=50%
uv run sheet.py a.stl b.stl --cutaway "y>50%" --views iso,front,top
uv run sheet.py a.stl b.stl --view=-25,12 --roi "z=45:62" --slice z=52   # close-up on a region
uv run sheet.py a.stl --views none --slice z=10,20,30                    # slices only
```

- `--view AZ,EL` sets a custom camera. Use the `=` form for **negative** azimuths (`--view=-25,12`) — argparse eats a bare leading dash otherwise.
- `--roi "z=45:62[,x=..]"` zooms *every* tile onto a region, keeping any triangle that overlaps it.
- Surfaces that touch by design flag a few mm² of contact sliver; judge by the reported area — real collisions are 10–100× bigger.

### `viewer.py` — self-contained browser viewer (the human's eyes)

One HTML file (GLB embedded as base64, three.js from CDN): orbit controls, an adaptive mm grid with tick numbers in **world mm on all three axes**, per-part legend, explode toggle, auto-rotate (remembered in localStorage), and **double-ended clip sliders per axis** (keep the slab between two handles).

```sh
uv run viewer.py body.stl lid.stl -o preview.html --title "My part"
uv run viewer.py case.stl panel.stl pcb.stl glass.stl io.stl \
    --group module=pcb,glass,io -o preview.html   # module stays rigid on explode
open preview.html
```

The grid coordinates match `sheet.py`'s slice coordinates exactly — "clip Z 50:55" in the viewer and `--slice z=52` in the sheet are the same place. `--group NAME=a,b,c` (repeatable) makes those parts explode as one rigid body, so an assembly (PCB + glass + connectors) stays together while the shells fly off.

### `slice.py` — headless Bambu Studio

Wraps the Bambu Studio CLI (P1S profiles by default): slices one arranged plate and reports success/error, predicted print time, filament grams/meters, spool cost, per-object bounding boxes, and slicer warnings. Writes the ready-to-print `.gcode.3mf` next to the inputs.

```sh
uv run slice.py part1.stl part2.stl                         # one plate, P1S defaults
uv run slice.py part.stl --process "0.12mm Fine @BBL X1C" \
    --filament "Bambu PETG Basic @BBL P1S 0.4 nozzle"
uv run slice.py --list-processes | --list-filaments | --list-machines
```

Defaults: P1S, 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic. Filament weight is computed from the gcode's extruded length × profile density (the CLI leaves `used_g` at 0). **macOS/Bambu-specific** — edit the `APP` and `PROFILES` paths at the top for other platforms.

---

## A typical loop

```sh
# 1. edit your build123d / CadQuery script, export STLs (individual + assembled `asm_*`)
uv run check.py asm_*.stl --interference-max 0.05 --min-wall 1.2   # gate: fails loudly on real bugs
uv run sheet.py asm_*.stl --slice z=50% -o sheet.png               # look at it
uv run viewer.py asm_*.stl -o preview.html && open preview.html    # let a human opine
uv run slice.py part_a.stl part_b.stl                              # time + cost, then print
```

Model multi-part designs in **assembled coordinates** and export both the individual parts and an `asm_*` set — `check.py` and `sheet.py` do interference detection across whatever files you hand them.

---

## Field notes (hard-won)

A few things that cost real time to learn:

- **trimesh:** load with `process=True` or CAD-exported STLs aren't `is_volume` (boolean ops refuse triangle soup). `polygons_full` silently drops holes unless **rtree** is installed.
- **Multi-start threads: an axial shift *is* a rotation.** Moving a helical part `dz` along its axis without also rotating it by `360·dz/lead` breaks the mesh phase and produces phantom interference. Applies to through-cut bores and to seating parts in an assembly.
- **Rounding meshes:** there is no reliable one-call "fillet everything." For your own code, fillet at the **sketch** level (2D fillets almost never fail; 3D `fillet(all_edges)` fails constantly on real parts). For downloaded STLs with no B-rep, a morphological offset (e.g. MeshLib's `doubleOffsetMesh`) rounds every convex edge in one pass — but marching-cubes leaves lumpy edges unless you use a fine voxel size and a volume-preserving smoothing pass before decimation. All mesh rounding *moves surfaces* — re-cut functional bores and mating faces afterward; rounding is for the cosmetic 90%, never datum surfaces.
- **Print fits (Bambu P1S, PLA, 0.20 mm — calibrate for your own machine):** ~0.15 mm radial clearance is a snug press fit that can bind under gravity; ~0.25 mm self-slides; ~0.35 mm has noticeable wobble. Elephant foot binds tight fits at end-of-travel.
- **Test at the smallest scale where the measured quantity is still representative.** Local fits → print a coupon/ring. Integral quantities (friction over full engagement, gravity self-slide) → only the full length is authoritative.

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
