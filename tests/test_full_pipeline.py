"""Unit tests for run_full_eval_pipeline manifest and file matching logic."""

import os
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from run_full_eval_pipeline import natural_sort_key, build_manifest, find_sdk_files_for_timeline


class FullPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.drp_dir = self.root / "drps"
        self.sdk_dir = self.root / "sdk"
        self.drp_dir.mkdir()
        self.sdk_dir.mkdir()

    def test_natural_sort_key(self):
        items = ["item10.exr", "item2.exr", "item1.exr"]
        sorted_items = sorted(items, key=natural_sort_key)
        self.assertEqual(sorted_items, ["item1.exr", "item2.exr", "item10.exr"])

    def test_build_manifest_matching_and_filter(self):
        # Create mock DRPs
        (self.drp_dir / "project_a.drp").touch()
        (self.drp_dir / "project_b.drp").touch()

        # Create mock SDK folders
        sdk_a = self.sdk_dir / "project_a"
        sdk_a.mkdir()
        (sdk_a / "frame_0001.exr").touch()

        sdk_b = self.sdk_dir / "project_b"
        sdk_b.mkdir()
        (sdk_b / "frame_0001.tif").touch()

        # Full manifest
        manifest = build_manifest(self.drp_dir, self.sdk_dir)
        self.assertEqual(len(manifest["projects"]), 2)
        self.assertEqual(manifest["projects"][0]["export"], {"settings_source": "sdk_aligned"})
        self.assertEqual(manifest["projects"][1]["export"], {"settings_source": "sdk_aligned"})

        # Filtered manifest
        manifest_filtered = build_manifest(self.drp_dir, self.sdk_dir, project_ids=["project_a"])
        self.assertEqual(len(manifest_filtered["projects"]), 1)
        self.assertEqual(manifest_filtered["projects"][0]["id"], "project_a")

    def test_find_sdk_files_stereo_subdirs(self):
        sdk_case = self.sdk_dir / "stereo_case"
        (sdk_case / "LE").mkdir(parents=True)
        (sdk_case / "RE").mkdir(parents=True)
        (sdk_case / "LE" / "0001.exr").touch()
        (sdk_case / "LE" / "0002.exr").touch()
        (sdk_case / "RE" / "0001.exr").touch()
        (sdk_case / "RE" / "0002.exr").touch()

        files = find_sdk_files_for_timeline(sdk_case)
        self.assertEqual(len(files), 4)
        self.assertEqual([f.name for f in files], ["0001.exr", "0002.exr", "0001.exr", "0002.exr"])
        self.assertTrue(str(files[0]).endswith("LE/0001.exr"))
        self.assertTrue(str(files[2]).endswith("RE/0001.exr"))


if __name__ == "__main__":
    unittest.main()
