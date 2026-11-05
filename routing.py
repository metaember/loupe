"""Local Freerouting bridge; experimental rectangular two-layer boards only.

All geometry is read through KiCad. Preserve component UUID/transforms on SES
import. Caller receives a new native board; no manual source routing is replaced.
"""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def route_board(source, output, jar, expected_sha):
    from pcb import kicad_python, kicad_cli

    source, output, jar = [Path(p).resolve() for p in (source, output, jar)]
    if source == output or output.exists():
        raise ValueError("routing output must be a new path, different from source")
    if hashlib.sha256(jar.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("Freerouting JAR checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="loupe-route-") as td:
        work = Path(td)
        board = work / source.name
        # Copy the project rules too: the native DRC must use the source constraints.
        for suffix in [".kicad_pro", ".kicad_dru"]:
            extra = source.with_suffix(suffix)
            if extra.exists():
                (work / extra.name).write_bytes(extra.read_bytes())
        board.write_bytes(source.read_bytes())
        subprocess.run(
            [str(kicad_python()), __file__, "prepare", str(board)], check=True
        )
        subprocess.run(
            [
                "java",
                "-Djava.awt.headless=true",
                "-jar",
                str(jar),
                "--user_data_path=" + str(work / "router"),
                "--logging.file.enabled=false",
                "--gui.enabled=false",
                "--api_server.enabled=false",
                "-da",
                "-de",
                str(board.with_suffix(".dsn")),
                "-do",
                str(board.with_suffix(".ses")),
                "--router.autorouter.max_passes=40",
                "--router.optimizer.max_threads=2",
            ],
            check=True,
        )
        subprocess.run(
            [str(kicad_python()), __file__, "import", str(board)], check=True
        )
        drc = work / "drc.json"
        subprocess.run(
            [
                str(kicad_cli()),
                "pcb",
                "drc",
                str(board),
                "--format",
                "json",
                "--exit-code-violations",
                "-o",
                str(drc),
            ],
            check=False,
        )
        report = json.loads(drc.read_text())
        if report["violations"] or report["unconnected_items"]:
            raise ValueError(
                "routed output did not pass native DRC: "
                + json.dumps(report["violations"] + report["unconnected_items"])
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(board.read_bytes())
    return dict(
        ok=True,
        output=str(output),
        jar_sha256=expected_sha,
        note="source untouched; inspect output and library/model paths before use",
    )


def export_dsn(board, output):
    """Project-local DSN bridge: KiCad 10's standalone exporter returns false.

    Use KiCad's actual transformed pad bounds conservatively. No guessed pin
    coordinates. The final KiCad DRC is authoritative, including custom pads.
    """

    import pcbnew as pcb

    def q(value):
        return json.dumps(str(value))

    def u(value):
        return round(float(value) * 1000)

    layers = ["F.Cu", "B.Cu"]
    images = []
    stacks = []
    places = []
    nets = {}
    hole_keepouts = []
    for f in board.GetFootprints():
        ref = f.GetReference()
        origin = f.GetPosition()
        fx, fy = pcb.ToMM(origin.x), -pcb.ToMM(origin.y)
        image = []
        for index, p in enumerate(f.Pads()):
            if not p.GetNumber():
                if p.GetAttribute() == pcb.PAD_ATTRIB_NPTH:
                    hp = p.GetPosition()
                    drill = p.GetDrillSize()
                    diam = pcb.ToMM(max(drill.x, drill.y)) + 0.5
                    for layer_name in layers:
                        hole_keepouts.append(
                            f'(keepout "" (circle {q(layer_name)} {u(diam)} {u(pcb.ToMM(hp.x))} {u(-pcb.ToMM(hp.y))}))'
                        )
                continue
            if not ref:
                continue
            name = str(p.GetNumber()) + "_" + str(index)
            bb = p.GetBoundingBox()
            w = pcb.ToMM(bb.GetWidth())
            h = pcb.ToMM(bb.GetHeight())
            # Custom pad anchors need not coincide with their bbox centres.
            bx = pcb.ToMM(bb.GetX()) + w / 2
            by = -pcb.ToMM(bb.GetY()) - h / 2
            shape = f"{ref}_{index}"
            active = [
                layer_name
                for layer_name in layers
                if p.IsOnLayer(pcb.F_Cu if layer_name == "F.Cu" else pcb.B_Cu)
            ]
            shapes = " ".join(
                f"(shape (rect {q(layer_name)} {u(-w / 2)} {u(-h / 2)} {u(w / 2)} {u(h / 2)}))"
                for layer_name in active
            )
            stacks.append(f"(padstack {q(shape)} {shapes} (attach off))")
            image.append(f"(pin {q(shape)} {q(name)} {u(bx - fx)} {u(by - fy)})")
            if p.GetNetCode():
                nets.setdefault(p.GetNetname(), []).append(ref + "-" + name)
        if not ref:
            continue
        images.append(f"(image {q(ref)} " + "".join(image) + ")")
        places.append(f"(component {q(ref)} (place {q(ref)} {u(fx)} {u(fy)} front 0))")
    via = '(padstack "VIA" (shape (circle "F.Cu" 600)) (shape (circle "B.Cu" 600)) (attach off))'
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = pcb.ToMM(bb.GetX()), -pcb.ToMM(bb.GetY())
    x1, y1 = x0 + pcb.ToMM(bb.GetWidth()), y0 - pcb.ToMM(bb.GetHeight())
    points = [(x0, y1), (x1, y1), (x1, y0), (x0, y0), (x0, y1)]
    outline = " ".join(f"{u(x)} {u(y)}" for x, y in points)
    keepouts = list(hole_keepouts)
    # Project-specific antenna/edge keepouts must be supplied by the caller.
    nettext = "".join(
        f"(net {q(n)} (pins " + " ".join(pins) + "))" for n, pins in nets.items()
    )
    classes = (
        '(class "default" '
        + " ".join(q(n) for n in nets)
        + ' (circuit (use_via "VIA")) (rule (width 250) (clearance 200)))'
    )
    text = f"""(pcb {q(Path(output).stem)}
      (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "KiCad") (host_version "10.0.4"))
      (resolution um 10) (unit um)
      (structure (layer "F.Cu" (type signal) (property (index 0)))
        (layer "B.Cu" (type signal) (property (index 1)))
        (boundary (path pcb 0 {outline})) (via "VIA")
        (rule (width 250) (clearance 200)) {"".join(keepouts)})
      (placement {"".join(places)})
      (library {"".join(images)} {"".join(stacks)} {via})
      (network {nettext} {classes}) (wiring))"""
    Path(output).write_text(text)


def import_session(board, session):
    """Restore placement, orientation AND side after native SES import."""
    import pcbnew as pcb

    before = {
        f.m_Uuid.AsString(): (
            f.GetPosition().x,
            f.GetPosition().y,
            f.GetOrientationDegrees(),
            f.IsFlipped(),
        )
        for f in board.GetFootprints()
    }
    if not pcb.ImportSpecctraSES(board, str(session)):
        raise ValueError("SES import failed")
    if set(before) != {f.m_Uuid.AsString() for f in board.GetFootprints()}:
        raise ValueError("SES import changed component UUIDs")
    for f in board.GetFootprints():
        x, y, angle, flipped = before[f.m_Uuid.AsString()]
        if f.IsFlipped() != flipped:
            f.Flip(f.GetPosition(), False)
        f.SetPosition(pcb.VECTOR2I(x, y))
        f.SetOrientationDegrees(angle)
    after = {
        f.m_Uuid.AsString(): (
            f.GetPosition().x,
            f.GetPosition().y,
            f.GetOrientationDegrees(),
            f.IsFlipped(),
        )
        for f in board.GetFootprints()
    }
    if before != after:
        raise ValueError("SES import changed native placement transforms")
    return board


def driver(mode, filename):
    global pcb
    import wx

    wx.DisableAsserts()
    import pcbnew as pcb

    board = pcb.LoadBoard(filename)
    if board.GetCopperLayerCount() != 2:
        raise ValueError("router bridge supports two copper layers only")
    if mode == "prepare":
        # This bridge only models a rectangular outline; fail rather than route off-board.
        edges = list(board.GetDrawings())
        edges = [e for e in edges if e.GetLayer() == pcb.Edge_Cuts]
        if len(edges) != 4 or any(e.GetShape() != pcb.SHAPE_T_SEGMENT for e in edges):
            raise ValueError("router bridge requires four straight rectangular edges")
        for edge in edges:
            a, b = edge.GetStart(), edge.GetEnd()
            if a.x != b.x and a.y != b.y:
                raise ValueError(
                    "router bridge requires axis-aligned rectangular edges"
                )
        for track in list(board.GetTracks()):
            board.Remove(track)
        pcb.SaveBoard(filename, board)
        export_dsn(board, Path(filename).with_suffix(".dsn"))
    elif mode == "import":
        import_session(board, Path(filename).with_suffix(".ses"))
        pcb.SaveBoard(filename, board)
        # Zone fill needs a saved/reloaded board; a newly created board may crash KiCad10.
        loaded = pcb.LoadBoard(filename)
        pcb.ZONE_FILLER(loaded).Fill(loaded.Zones())
        pcb.SaveBoard(filename, loaded)
    else:
        raise ValueError("unknown driver mode")


if __name__ == "__main__":
    import sys

    driver(*sys.argv[1:])
