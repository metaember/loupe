# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["build123d>=0.11"]
# ///
"""Generic threaded jar + screw lid — the demo model behind loupe's README shots.

A clean single-start trapezoidal thread (nothing reverse-engineered). The lid's
internal thread is cut as the negative of the male dilated by CLEAR, so a seated
cross-section shows the flanks engaging with a uniform, visible clearance gap.

    uv run examples/threaded_jar.py            # -> asm_jar.stl, asm_lid.stl
    uv run sheet.py asm_jar.stl asm_lid.stl --views none \
        --slice x=50% --slice z=40 --slice z=47 -o hero.png
"""
from build123d import *

Rr, TD, PITCH, TURNS = 14.0, 2.4, 6.0, 3
HTHREAD = PITCH * TURNS
CLEAR = 0.5                                   # uniform male/female gap
BODY_R, BODY_H, WALL, NECK_BORE = 24.0, 34.0, 2.6, 10.5
Z0, Z1 = BODY_H, BODY_H + HTHREAD
C, MN = Align.CENTER, Align.MIN
HB, HC = 1.9, 0.85                            # male tooth half-widths (root, crest)

def thread_ridge(root_r, hb, hc, z0, depth=TD, inward=False, phase=0.0, htot=HTHREAD):
    """One helical trapezoidal ridge over the band [z0, z0+htot]."""
    d = -depth if inward else depth
    with BuildPart() as rp:
        with BuildLine():
            path = Helix(PITCH, htot, root_r)
        with BuildSketch(Plane(origin=path @ 0, z_dir=path % 0)):
            Polygon((0, -hb), (d, -hc), (d, hc), (0, hb), align=None)
        sweep(path=path, is_frenet=True)
    r = rp.part
    if phase:
        r = Rot(0, 0, phase) * r
    return Pos(0, 0, z0) * r

# ---------- JAR (male) ----------
jar  = Cylinder(BODY_R, BODY_H, align=(C, C, MN))
jar += Pos(0, 0, Z0) * Cylinder(Rr, HTHREAD, align=(C, C, MN))
jar += thread_ridge(Rr, HB, HC, Z0)
cav  = Pos(0, 0, WALL) * Cylinder(BODY_R - WALL, BODY_H - WALL, align=(C, C, MN))
cav += Pos(0, 0, Z0 - 0.5) * Cylinder(NECK_BORE, HTHREAD + 1, align=(C, C, MN))
jar  = jar - cav
# fine tessellation so the mating helical flanks don't graze at facet vertices
FINE = dict(tolerance=0.0005, angular_tolerance=0.03)
export_stl(jar, "asm_jar.stl", **FINE)

# ---------- LID (female = negative of the male, dilated by CLEAR) ----------
LID_R, LID_TOP, SKIRT_DROP = 19.0, 3.0, 3.0
LID_H = HTHREAD + LID_TOP + 1 + SKIRT_DROP             # skirt drops below the thread band
lid  = Pos(0, 0, Z0 - SKIRT_DROP) * Cylinder(LID_R, LID_H, align=(C, C, MN))
cut  = Pos(0, 0, Z0 - 0.5) * Cylinder(Rr + CLEAR, HTHREAD + 1.5, align=(C, C, MN))   # clear the neck core
cut += thread_ridge(Rr, HB + CLEAR, HC + CLEAR, Z0, depth=TD + CLEAR)                # dilated male groove (phase-aligned)
lid  = lid - cut
export_stl(lid, "asm_lid.stl", **FINE)
export_stl(Pos(1.6, 0, 0) * lid, "asm_lid_off.stl", **FINE)   # off-axis copy for the interference shot

print("jar bbox:", jar.bounding_box().size, "| lid bbox:", lid.bounding_box().size)
