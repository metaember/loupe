#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "trimesh", "scipy", "rtree", "manifold3d>=3.5.2"]
# ///
"""Self-contained synthetic geometry-preflight regressions."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import trimesh

import check

ROOT = Path(__file__).resolve().parent


def localized_skin():
    base = trimesh.creation.box([40, 40, 4])
    skin = trimesh.creation.box([2, 2, 0.02])
    skin.apply_translation([20, 0, 1.99])
    return trimesh.boolean.union([base, skin], engine="manifold")


class WallTests(unittest.TestCase):
    def test_valid_box_and_exact_floor_pass(self):
        for dimensions in ([10, 12, 3], [3, 4, 0.4]):
            with self.subTest(dimensions=dimensions):
                report = check.min_wall(trimesh.creation.box(dimensions))
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["thin_patches"], [])
                self.assertEqual(report["unresolved_samples"], 0)

    def test_actual_intersection_distances_and_sub_point_zero_one_skin(self):
        report = check.min_wall(trimesh.creation.box([20, 30, 0.005]))
        self.assertEqual(report["status"], "thin")
        self.assertAlmostEqual(report["min"], 0.005, places=9)
        self.assertTrue(all(abs(p["min_mm"] - 0.005) < 1e-8
                            for p in report["thin_patches"]))
        self.assertGreater(report["thin_patches"][0]["area_mm2"], 500)

    def test_localized_skin_fails_despite_good_percentile(self):
        mesh = localized_skin()
        self.assertTrue(mesh.is_volume)
        self.assertEqual(len(mesh.split()), 1)
        report = check.min_wall(mesh)
        self.assertEqual(report["status"], "thin")
        self.assertGreaterEqual(report["p5"], 0.4)
        patch = report["thin_patches"][0]
        self.assertAlmostEqual(patch["min_mm"], 0.02, places=6)
        self.assertGreaterEqual(patch["area_mm2"], 1)
        self.assertGreater(patch["location_mm"][0], 20)

    def test_rotated_translated_localized_skin_is_detected(self):
        mesh = localized_skin()
        transform = trimesh.transformations.rotation_matrix(0.71, [1, 2, 3])
        transform[:3, 3] = [60, -13, 2]
        mesh.apply_transform(transform)
        report = check.min_wall(mesh)
        self.assertEqual(report["status"], "thin")
        self.assertAlmostEqual(report["thin_patches"][0]["min_mm"], 0.02, places=5)

    def test_repeated_runs_are_identical_and_do_not_use_random_samples(self):
        mesh = localized_skin()
        with mock.patch("trimesh.sample.sample_surface", side_effect=AssertionError("random")):
            first = check.min_wall(mesh)
            np.random.seed(921)
            second = check.min_wall(mesh)
        self.assertEqual(first, second)

    def test_chamfer_tip_short_chords_are_diagnostic_not_thin_membranes(self):
        profile = [(0, 0), (10, 0), (10, 3), (3, 3)]
        mesh = trimesh.convex.convex_hull([[x, y, z] for x in (-5, 5)
                                          for y, z in profile])
        report = check.min_wall(mesh, sample_area=0.05)
        self.assertEqual(report["status"], "ok")
        self.assertGreater(report["edge_short_area_mm2"], 0)
        self.assertEqual(report["thin_patches"], [])

    def test_curved_solid_passes(self):
        mesh = trimesh.creation.icosphere(subdivisions=2, radius=5)
        report = check.min_wall(mesh)
        self.assertEqual(report["status"], "ok")

    def test_rounded_plate_edge_passes(self):
        angles = np.linspace(np.pi, np.pi / 2, 33)
        profile = [(3 + 3 * np.cos(a), 3 * np.sin(a)) for a in angles]
        profile.extend([(10, 0), (10, 3)])
        mesh = trimesh.convex.convex_hull([[x, y, z] for x in (-5, 5)
                                          for y, z in profile])
        self.assertEqual(check.min_wall(mesh, sample_area=0.25)["status"], "ok")

    def test_every_face_is_sampled_and_quadrature_area_is_conserved(self):
        mesh = trimesh.creation.box([20, 30, 0.02])
        points, faces, weights = check._wall_samples(mesh, 4)
        self.assertEqual(set(faces), set(range(len(mesh.faces))))
        self.assertLessEqual(weights.max(), 4)
        self.assertAlmostEqual(weights.sum(), mesh.area)
        self.assertTrue(np.isfinite(points).all())

    def test_no_hit_is_indeterminate_never_a_silent_pass(self):
        mesh = trimesh.creation.box([3, 4, 5])
        def missing(solid, points, normals, epsilon, length):
            return np.full(len(points), np.nan), np.full(len(points), np.nan)
        with mock.patch("check._wall_rays", side_effect=missing):
            report = check.min_wall(mesh)
        self.assertEqual(report["status"], "indeterminate")
        self.assertIsNone(report["min"])
        self.assertGreater(report["unresolved_samples"], 0)
        self.assertAlmostEqual(report["unresolved_area_mm2"], mesh.area)
        self.assertTrue(report["errors"])

    def test_one_unresolved_ray_still_fails_closed(self):
        original = check._wall_rays
        def one_missing(*args):
            distances, opposition = original(*args)
            distances[0] = np.nan
            return distances, opposition
        with mock.patch("check._wall_rays", side_effect=one_missing):
            report = check.min_wall(trimesh.creation.box([3, 4, 5]))
        self.assertEqual(report["status"], "indeterminate")
        self.assertEqual(report["unresolved_samples"], 1)
        self.assertEqual(len(report["unresolved_location_mm"]), 3)
        self.assertIsNotNone(report["min"])

    def test_non_volume_and_empty_mesh_are_indeterminate(self):
        box = trimesh.creation.box([3, 4, 5])
        box.update_faces(np.arange(len(box.faces)) != 0)
        for mesh in (box, trimesh.Trimesh()):
            report = check.min_wall(mesh)
            self.assertEqual(report["status"], "indeterminate")
            self.assertTrue(report["errors"])

    def test_sampling_budget_failure_is_explicit(self):
        with mock.patch("check.MAX_WALL_SAMPLES", 1):
            report = check.min_wall(trimesh.creation.box([3, 4, 5]))
        self.assertEqual(report["status"], "indeterminate")
        self.assertIn("sampling exceeds", report["errors"][0])

    def test_override_and_invalid_thresholds(self):
        mesh = trimesh.creation.box([5, 5, 0.2])
        self.assertEqual(check.min_wall(mesh, 0.1)["status"], "ok")
        self.assertEqual(check.min_wall(mesh, 0.3)["status"], "thin")
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                check.min_wall(mesh, value)


class CliTests(unittest.TestCase):
    def run_check(self, mesh, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.stl"
            mesh.export(path)
            result = subprocess.run([sys.executable, str(ROOT / "check.py"),
                                     str(path), "--json", *args],
                                    capture_output=True, text=True, timeout=30)
        return result, json.loads(result.stdout) if result.stdout else None

    def test_default_rejects_skin_with_compatible_report(self):
        result, report = self.run_check(trimesh.creation.box([3, 4, 0.02]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report["ok"])
        self.assertTrue(report["failures"])
        self.assertIn("part", report["parts"])
        self.assertEqual(report["parts"]["part"]["wall"]["status"], "thin")

    def test_min_wall_rejects_localized_patch_not_percentile(self):
        result, report = self.run_check(localized_skin(), "--min-wall", "0.4")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertGreaterEqual(report["parts"]["part"]["wall"]["p5"], 0.4)
        self.assertIn("thin patch", report["failures"][0])

    def test_valid_part_passes_and_overrides_are_explicit(self):
        result, report = self.run_check(trimesh.creation.box([3, 4, 5]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])
        skin = trimesh.creation.box([3, 4, 0.02])
        result, report = self.run_check(skin, "--no-wall-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["parts"]["part"]["wall"]["status"], "skipped")
        result, report = self.run_check(skin, "--min-wall", "0.01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report["ok"])

    def test_non_volume_fails_closed(self):
        mesh = trimesh.creation.box([3, 4, 5])
        mesh.update_faces(np.arange(len(mesh.faces)) != 0)
        result, report = self.run_check(mesh)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(report["parts"]["part"]["wall"]["status"], "indeterminate")

    def test_indeterminate_exit_is_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.stl"
            trimesh.creation.box([3, 4, 5]).export(path)
            error = {"status": "indeterminate", "min": None, "p5": None,
                     "errors": ["inward rays without a reliable exit"], "thin_patches": []}
            with mock.patch("sys.argv", ["check.py", str(path), "--json"]), \
                 mock.patch("check.min_wall", return_value=error), \
                 mock.patch("builtins.print") as output:
                with self.assertRaises(SystemExit) as exit:
                    check.main()
            self.assertEqual(exit.exception.code, 1)
            self.assertFalse(json.loads(output.call_args.args[0])["ok"])

    def test_zero_is_not_an_implicit_opt_out(self):
        result, _ = self.run_check(trimesh.creation.box([3, 4, 5]), "--min-wall", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--no-wall-check", result.stderr)




if __name__ == "__main__":
    unittest.main()
