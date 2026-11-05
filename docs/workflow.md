# Electronics project workflow

Run `uv run /path/to/loupe/loupe.py --project /path/to/device doctor`, then `build`,
`check`, `review`, and `release --target jlcpcb`. `status` reports fresh/stale evidence.
These commands operate locally; none uploads, orders, flashes a device or starts a print.

The first supported environment is macOS with KiCad10, uv and Python>=3.11.
KiCad is discovered in /Applications or ~/Applications; `LOUPE_KICAD` overrides it.
The scripts still declare their isolated Python dependencies through PEP723.
No cache-directory or machine-specific interpreter path belongs in a project manifest.
UV_CACHE_DIR can select a writable cache; first use may need network access for uv dependencies.
The tested runtime versions and source contents are recorded in stage evidence.
Per-tool dependency resolution remains uv-managed; a cross-machine locked dependency
bundle is not implemented in this milestone.

## Project contract

`loupe.json` version1 declares an ordered list of stages. Each stage has a unique
`name`, `kind` (`build`, `check`, `review`), an argv-array `command`, explicit `inputs`
(globs permitted), optional `optional_inputs` (including absent rule sidecars), explicit `outputs`, and optional `requires` pointing to earlier
stages. Optional `reports` name JSON outputs that must contain `ok: true`.
`stdout_report` captures JSON stdout into a report while keeping stderr separate.

Commands substitute only `{python}`, `{uv}`, `{loupe}`, `{project}`, `{kicad_cli}`,
and `{kicad_python}`. Other braces remain literal. Commands never pass through a shell.
Dependencies are freshness requirements; `check` does not secretly rebuild source.
Run build/check/review in order. Fresh stages skip automatically; `--force` reruns a phase.
Logs and evidence are under `.loupe/`, which should be ignored by version control.

Every pass records:

- hashes of all declared inputs and outputs, including glob membership;
- the project manifest, Loupe Python sources and actual Python/uv/KiCad versions;
- hashes of evidence required from preceding stages.

A changed/missing file, changed tool identity, changed rule/configuration, failed rerun,
or modified report makes the affected stage and dependents stale. A failure removes
its previous passing evidence. Explicit project inputs must include custom libraries,
models and rules used by that stage. External stock KiCad libraries are covered by
KiCad version identity; in-place edits to installed stock libraries are unsupported.
Treat custom/modified library files as versioned project inputs.

`release.targets` is an explicit set; initially the exporters support JLC format.
Each bundle declares explicit `files` and an `output` ZIP. No globs enter release
bundles, and every file must be covered by a stage's input/output declaration.
The release includes evidence and SHA256SUMS and checks ZIP CRCs/readback. Reports
remain engineering evidence, not proof of circuit function or manufacturing approval.
Sourcing and physical-test holds remain visible in the release manifest. Private
.env files, secrets.yaml, .venv, .git and paths/symlinks escaping the project are rejected.

Project commands are trusted local build code. This is not a sandbox for untrusted
project manifests. Other project-specific secrets must also be excluded explicitly.

## Native design ownership

Keep the reviewed routed PCB and schematic as declared source inputs unless the
project explicitly owns their generation. The external acceptance project preserves
both during ordinary build; circuit intent is compared against the native files.
Its explicit schematic generator and board regeneration remain project operations.
Regenerate a board in a separate project copy, then inspect and adopt its result.
A rebuild must not silently replace manual routing.

`schematic.load_symbol` supplies inherited-symbol resolution for native schematic
generators. `loupe circuit-check --board ... --schematic ... --design ... --output ...`
runs fresh native ERC, netlist export, strict DRC and pin comparison. Mechanical/DNC
pads may be absent from schematic symbols, but must exist on the native PCB.

`loupe assembly-check --design ... --bom ... --cpl ... --output ...` checks JLC BOM/CPL
membership, duplicate references, supplier IDs, placement coordinates, side and rotation.
It does not certify supplier pin1 rotation offsets or current component availability.
Parts use ref/pins, at (or xy), rot, side, lcsc, optional assembly and dnp.
The design includes board dimensions/holes and optional native lower-left frame x/y.

## Routing

`routing.export_dsn(board, path)` is the shared native-geometry DSN bridge.
`loupe route source.kicad_pcb --output new.kicad_pcb --jar official.jar --jar-sha256 HASH`
requires a new output path, verifies the JAR checksum, disables analytics/API-server
operation, and runs Freerouting locally. Install Java separately. Freerouting2.5.0
is the qualified release; obtain it from the official release page, not an unknown mirror.
The source PCB is untouched. SES import preserves component UUID transforms and front/back side.
The native fixture exercises a rotated top part, a rotated bottom part, an offset custom
pad and an NPTH hole, and requires zero native DRC violations/unconnected items.

The standalone route command is experimental and restricted to axis-aligned rectangular
two-layer boards. Conservative pad bounds may reduce routing opportunities. Do not use
it for controlled-impedance certification, arbitrary cutouts or advanced stackups.
Inspect and run project parity/library checks on the new output before adopting it.
Project-specific plane, keepout and model preparation remains project code.

## Agent interface and tests

MCP adds `project_status` and `project_run` over this same engine. Existing geometry
MCP tools remain available. Project failures propagate errors; the stage log is local.

Validation commands:

```sh
python3 -m unittest discover -p test_workflow.py -v
uv run test_check.py
```

`test_workflow.py` covers stale sources/reports/tools, failed reruns, release membership,
private/escaping paths, dependencies, symbol inheritance and pin/placement regressions.
`test_check.py` uses self-contained synthetic fixtures for localized thin regions, sharp-edge
handling and indeterminate checks. Consumer-specific saved-artifact tests belong in the
consumer repository and are not discovered or read by this public test suite.
Native KiCad/router integration is exercised separately by examples/route_fixture.py.
The external reference project is an external acceptance consumer; do not vendor its secrets,
large component downloads or private device configuration into Loupe.

Slicing remains a legacy adapter. This milestone neither qualifies its P1S settings
nor adds printer control. Inspect native slicer profiles/toolpaths before printing.


## First acceptance run (2026-11-05)

The scoped new/changed tooling passes Ruff. A full legacy `ruff check *.py` still
reports 34 existing issues in draftcheck.py, draftsheet.py, pcb.py and sheet.py;
those unrelated style changes were left out of this milestone. The native router
fixture exposed and fixed a bottom-side SES import bug. Fresh firmware setup exposed
missing littlefs-python/fatfs-ng dependencies; the external firmware adapter now pins both.
The MCP adapter pins SDK 1.x because the previous open-ended dependency resolves to
an incompatible 2.x API. No printer, factory upload/order or device flash is exercised.
