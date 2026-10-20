import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compare_outputs as compare
import resolve_worker as worker
import numpy as np
import cv2


def case_for_images():
    return {
        'identity': {'test_mode': 'sdk_equivalence'},
        'locked_settings': {'resolution': [2, 2], 'source_fps': 24, 'timeline_fps': 48},
        'export_rules': {'format': 'png', 'mark_in': 0, 'mark_out': 0, 'expected_frame_count': 1},
        'comparison_rules': {
            'file_pattern': 'sdk_{index}.png', 'resolve_output_pattern': 'out_{index}.png',
            'pixel_domain': {'channels': 'RGB'},
            'frame_mapping_rule': {'frame_count': 1, 'interpolation_timestep': 0.5, 'anchor_phase': 0},
            'tolerances': {'max_abs_error_limit': 0.004, 'mean_abs_error_limit': 0.0005,
                           'outlier_pixel_threshold': 0.002, 'max_outlier_pixel_ratio': 0.001},
            'visual_evidence': {'top_n_worst_frames': 0, 'mandatory_boundary_frames': []},
        },
        'execution_policy': {'iterations': 1},
    }


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ('out', 'sdk', 'cmp'):
            (self.root / folder).mkdir()
        self.case = case_for_images()
        self.image = np.zeros((2, 2, 3), dtype=np.uint8)
        cv2.imwrite(str(self.root / 'out/out_0.png'), self.image)
        cv2.imwrite(str(self.root / 'sdk/sdk_0.png'), self.image)

    def run_compare(self):
        return compare.run_comparison(self.case, str(self.root/'out'), str(self.root/'sdk'), str(self.root/'cmp'))

    def test_exact_match_records_actual_mapping_and_hashes(self):
        self.assertEqual(self.run_compare()['match_classification'], 'EXACT_MATCH')
        mapping = json.loads((self.root/'cmp/frame_mapping.json').read_text())
        self.assertEqual(len(mapping), 1)
        self.assertEqual(len(mapping[0]['sdk_sha256']), 64)

    def test_wrong_filename_never_falls_back(self):
        (self.root/'out/out_0.png').rename(self.root/'out/wrong.png')
        self.assertFalse(self.run_compare()['ok'])

    def test_mismatch_has_failure_code_and_reason(self):
        cv2.imwrite(str(self.root/'out/out_0.png'), self.image + 10)
        result = self.run_compare()
        self.assertEqual(result['failure_code'], 'PIXEL_OUT_OF_TOLERANCE')
        self.assertTrue(result['reason'])

    def test_pixel_ratio_counts_any_channel_per_pixel(self):
        image = self.image.copy()
        image[0, 0, 0] = 255
        cv2.imwrite(str(self.root/'out/out_0.png'), image)
        self.assertEqual(self.run_compare()['per_frame'][0]['outlier_pixel_ratio'], 0.25)

    def test_duplicate_mapping_rejected(self):
        with self.assertRaises(ValueError):
            compare.build_frame_mapping({'frame_mapping_rule': {'entries': [
                {'output_frame_index': 0, 'sdk_frame_index': 0},
                {'output_frame_index': 0, 'sdk_frame_index': 1}]}})

    def test_timestep_must_be_explicit(self):
        with self.assertRaises(ValueError):
            compare.build_frame_mapping({'frame_mapping_rule': {'frame_count': 3}})

    def test_declared_timestep_used(self):
        mapping = compare.build_frame_mapping({'frame_mapping_rule': {'frame_count': 3, 'interpolation_timestep': 0.25, 'anchor_phase': 0}})
        self.assertEqual(mapping[1]['expected_timestep'], 0.25)

    def test_missing_tolerance_rejected(self):
        self.case['comparison_rules']['tolerances'] = {}
        self.assertEqual(self.run_compare()['verdict'], 'BLOCKED')

    def test_nonfinite_rejected_with_diagnostic(self):
        arr = np.zeros((2, 2, 3)); arr[0, 0, 0] = np.nan
        with patch.object(compare, '_load_image_normalized', return_value=(arr, (2,2,3), 32)):
            result = self.run_compare()
        self.assertEqual(result['failure_code'], 'NONFINITE_PIXELS')







class ExtraRegressionTests(unittest.TestCase):
    setUp = ComparatorTests.setUp
    run_compare = ComparatorTests.run_compare
    def test_visual_error_does_not_erase_known_failure(self):
        cv2.imwrite(str(self.root/'out/out_0.png'),self.image+20)
        with patch.object(compare,'_generate_visual_artifacts',side_effect=RuntimeError('visual failure')):
            result=self.run_compare()
        self.assertEqual(result['verdict'],'FAIL')
        self.assertTrue(result.get('evidence_errors'))

    def test_float_small_error_not_rounded_to_exact(self):
        a=np.zeros((2,2,3)); b=a+1e-9
        with patch.object(compare,'_load_image_normalized',side_effect=[(a,(2,2,3),32),(b,(2,2,3),32)]):
            result=self.run_compare()
        self.assertGreater(result['metrics']['max_abs_error'],0)

    def test_input_frame_required_in_explicit_mapping(self):
        with self.assertRaises(ValueError):
            compare.build_frame_mapping({'frame_mapping_rule':{'entries':[{'output_frame_index':0,'sdk_frame_index':0,'frame_type':'anchor','expected_timestep':0}]}})

    def test_compute_ssim_exact_and_difference(self):
        img_a = np.zeros((32, 32, 3), dtype=np.float64)
        img_b = img_a.copy()
        self.assertEqual(compare._compute_ssim(img_a, img_b), 1.0)

        img_b[10:20, 10:20, :] = 0.5
        ssim_diff = compare._compute_ssim(img_a, img_b)
        self.assertLess(ssim_diff, 1.0)
        self.assertGreater(ssim_diff, 0.0)

        # Mismatched shape returns 0.0
        self.assertEqual(compare._compute_ssim(img_a, np.zeros((16, 16, 3))), 0.0)

    def test_subpath_allowed_in_mapping(self):
        rule = {
            'frame_mapping_rule': {
                'entries': [{
                    'output_frame_index': 0,
                    'sdk_frame_index': 0,
                    'frame_type': 'anchor',
                    'input_frame_a': 0,
                    'input_frame_b': 0,
                    'expected_timestep': 0.0,
                    'resolve_filename': 'out_0000.exr',
                    'sdk_filename': 'LE/frame_0000.exr',
                }]
            }
        }
        mapping = compare.build_frame_mapping(rule)
        self.assertEqual(mapping[0]['sdk_filename'], 'LE/frame_0000.exr')

    def test_path_traversal_rejected_in_mapping(self):
        for bad_path in ('/abs/path.exr', '../outside.exr', 'sub/../../bad.exr'):
            rule = {
                'frame_mapping_rule': {
                    'entries': [{
                        'output_frame_index': 0,
                        'sdk_frame_index': 0,
                        'frame_type': 'anchor',
                        'input_frame_a': 0,
                        'input_frame_b': 0,
                        'expected_timestep': 0.0,
                        'resolve_filename': 'out_0000.exr',
                        'sdk_filename': bad_path,
                    }]
                }
            }
            with self.assertRaises(ValueError):
                compare.build_frame_mapping(rule)


if __name__ == '__main__':
    unittest.main()
