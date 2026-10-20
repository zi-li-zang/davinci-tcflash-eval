import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_first as runner
import compare_outputs as compare
import run_full_eval_pipeline as pipeline


class RevisionTests(unittest.TestCase):
    def test_nested_baseline_path_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'sdk'; root.mkdir()
            (root / 'LE').mkdir(); (root / 'LE/f.exr').touch()
            self.assertEqual(compare.safe_frame_path(root, 'LE/f.exr'), str((root / 'LE/f.exr').resolve()))
            (root / 'escape').symlink_to(Path(tmp), target_is_directory=True)
            for bad in ('../f.exr', '/f.exr', 'escape/f.exr'):
                with self.assertRaises(ValueError): compare.safe_frame_path(root, bad)

    def test_nonzero_contract_is_not_silently_rewritten(self):
        case = {'comparison_rules': {'pixel_domain': {'channels': 'RGB'},
                                    'tolerances': {'max_abs_error_limit': 1}}}
        with self.assertRaises(ValueError): runner.comparison_case({'comparison': case})

    def test_manifest_defaults_to_sdk_aligned_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'drp').mkdir(); (root / 'sdk').mkdir()
            (root / 'drp/a.drp').touch()
            m = pipeline.build_manifest(root / 'drp', root / 'sdk')
            self.assertEqual(m['projects'][0]['export'], {'settings_source': 'sdk_aligned'})

    def test_pause_resume_preserves_first_failure_and_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'inputs'; source.mkdir()
            (source / 'a.drp').write_bytes(b'drp')
            manifest = {'projects': [{'id': 'a', 'drp_path': str(source / 'a.drp')} ]}
            fail = [True]
            calls = []
            def fake(action, payload, directory, *args, **kw):
                calls.append(action)
                if action == 'inventory': return {'ok': True, 'data': {'timelines': ['A', 'B']}}
                if action == 'render_selected':
                    if fail[0]:
                        return {'ok': False, 'failure_code': 'TIMEOUT', 'reason': 'first timeout',
                                'host_state': 'UNKNOWN'}
                    out = Path(payload['target_dir']); out.mkdir()
                    (out / 'frame.exr').write_bytes(b'image')
                    return {'ok': True, 'data': {'elapsed_sec': 1}}
                return {'ok': True}
            batch = root / 'batch'
            rows = runner.run(manifest, batch, call=fake)
            self.assertEqual(calls.count('close_project'), 0)
            self.assertEqual(json.loads((batch / 'report.json').read_text())['batch_state'], 'PAUSED')
            first_dir = Path(rows[0]['run_dir']); self.assertTrue((first_dir / 'report.md').exists())
            self.assertFalse((first_dir / 'report_detailed.md').exists())
            fail[0] = False
            rows = runner.run(manifest, batch, call=fake, resume=True, retry_items=[rows[0]['item_id']])
            self.assertEqual(len(rows[0]['attempts']), 2)
            self.assertEqual(rows[0]['attempts'][0]['result']['failure_code'], 'TIMEOUT')
            self.assertTrue(all(r['render_status'] == 'COMPLETE' for r in rows))
            self.assertEqual(json.loads((batch / 'report.json').read_text())['batch_state'], 'COMPLETE')
            self.assertTrue((batch / 'manifest.json').exists())


class EvidenceRegressionTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import cv2
        self.cv2 = cv2
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('sdk', 'render', 'comparison'): (self.root / name).mkdir()
        self.image = np.zeros((2, 2, 3), dtype=np.uint8)
        from test_hardening import case_for_images
        self.case = case_for_images()
        self.cv2.imwrite(str(self.root / 'sdk/sdk_0.png'), self.image)
        self.cv2.imwrite(str(self.root / 'render/out_0.png'), self.image)

    def compare(self):
        return compare.run_comparison(self.case, str(self.root / 'render'), str(self.root / 'sdk'), str(self.root / 'comparison'))

    def test_nested_pattern_is_compared_not_just_validated(self):
        (self.root / 'sdk/LE').mkdir()
        (self.root / 'sdk/sdk_0.png').rename(self.root / 'sdk/LE/sdk_0.png')
        self.case['comparison_rules']['file_pattern'] = 'LE/sdk_{index}.png'
        self.assertEqual(self.compare()['match_classification'], 'EXACT_MATCH')

    def test_extra_frame_prevents_subset_pass(self):
        self.cv2.imwrite(str(self.root / 'render/out_1.png'), self.image)
        result = self.compare()
        self.assertEqual(result['failure_code'], 'FRAME_COVERAGE_MISMATCH')
        self.assertEqual(len(result['coverage']['extra_resolve_files']), 1)

    def test_hidden_nas_metadata_does_not_count_as_frame(self):
        (self.root / 'sdk/._sdk_0.png').write_bytes(b'AppleDouble')
        self.assertEqual(self.compare()['verdict'], 'PASS')

    def test_filename_mapping_needs_no_invented_motion_metadata(self):
        self.case['comparison_rules']['frame_mapping_rule'] = {'entries': [
            {'output_frame_index': 0, 'sdk_frame_index': 0, 'resolve_filename': 'out_0.png', 'sdk_filename': 'sdk_0.png'}]}
        result = self.compare()
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['per_frame'][0]['frame_type'], 'other')
        self.assertIsNone(result['per_frame'][0]['expected_timestep'])

    def test_standard_mapping_cannot_invent_phase(self):
        del self.case['comparison_rules']['frame_mapping_rule']['anchor_phase']
        self.assertEqual(self.compare()['verdict'], 'BLOCKED')

    def test_render_failure_keeps_timeout_when_host_unknown(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock
        host = MagicMock(); project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        timeline = project.GetTimelineByIndex.return_value
        timeline.GetName.return_value = 'A'; timeline.GetStartFrame.return_value = 0; timeline.GetEndFrame.return_value = 10
        project.IsRenderingInProgress.return_value = None
        project.GetCurrentRenderFormatAndCodec.return_value = {'format': 'exr', 'codec': 'RGBFloat'}
        project.GetCurrentRenderMode.return_value = 1
        with patch('plugin_presence.inspect_timeline', return_value={'status': 'PRESENT'}), \
             patch.object(worker, 'connect_resolve', return_value=host), \
             patch.object(worker, 'action_inventory', return_value={'ok': True}), \
             patch.object(worker, 'action_render', return_value={'ok': False, 'failure_code': 'TIMEOUT', 'reason': 'original'}):
            result = worker.action_render_selected({'expected_project_name': 'P', 'timeline_index': 1, 'export_rules': {}})
        self.assertEqual(result['failure_code'], 'TIMEOUT')
        self.assertEqual(result['reason'], 'original')
        self.assertEqual(result['host_state'], 'UNKNOWN')

    def test_close_project_return_success_alone_is_not_recovery_proof(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock
        host = MagicMock(); project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        project.GetName.return_value = 'EVAL_RUN_test'; project.IsRenderingInProgress.return_value = False
        host.GetProjectManager.return_value.CloseProject.return_value = True
        with patch.object(worker, 'connect_resolve', return_value=host):
            result = worker.action_close_project({'expected_project_name': 'EVAL_RUN_test'})
        self.assertEqual(result['failure_code'], 'CLOSE_PROJECT_UNCONFIRMED')

    def test_post_failure_state_query_timeout_keeps_render_checkpoint(self):
        import subprocess
        logs = self.root / 'logs'; logs.mkdir()
        (logs / 'render_result.json').write_text(json.dumps({'ok': False, 'failure_code': 'TIMEOUT', 'reason': 'render deadline'}))
        with patch.object(runner.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 5)):
            result = runner.invoke('render_selected', {}, logs, 5)
        self.assertEqual(result['failure_code'], 'TIMEOUT')
        self.assertEqual(result['host_state'], 'UNKNOWN')
        self.assertTrue((logs / 'operations/001_render_selected.log').is_file())

    def test_report_is_structured_and_keeps_observations(self):
        from reporting import summarize
        batch = self.root / 'batch'; batch.mkdir()
        item = batch / 'items/001_a__t01'; item.mkdir(parents=True)
        (item / 'observations.json').write_text(json.dumps({'visual_review': '已检查；证据在 diagnostics/check.png'}))
        row = {'item_id': '001_a__t01', 'project_id': 'a', 'drp_path': '/readonly/a.drp',
               'timeline': 'A', 'status': 'RENDERED_NOT_COMPARED', 'render_status': 'COMPLETE',
               'compare_status': 'NOT_READY', 'run_dir': str(item), 'attempts': [], 'reason': '需要映射',
               'evidence': {'render': {'data': {'observed_before_render': {
                   'format_codec': {'format': 'mov', 'codec': 'old_codec'}, 'render_mode': 0,
                   'export_rules': {'format': 'exr', 'codec': 'RGBFloat'}}}}}}
        summarize(batch, [row], 'COMPLETE')
        self.assertEqual((item / 'report.md').read_text().count('## '), 8)
        self.assertEqual((batch / 'report.md').read_text().count('## '), 5)
        self.assertIn('已检查', (item / 'report.md').read_text())
        self.assertEqual(json.loads((batch / 'report.json').read_text())['counts']['RENDERED_NOT_COMPARED'], 1)
        report = (item / 'report.md').read_text()
        self.assertNotIn('```json', report)
        # Pre-configuration state must not be presented as the final output spec.
        before = next(line for line in report.splitlines() if '配置前观察导出格式/编码' in line)
        request = next(line for line in report.splitlines() if '本次解析后导出请求' in line)
        self.assertIn('old_codec', before)
        self.assertIn('RGBFloat', request)
        self.assertNotIn('实测导出格式/编码', report)
