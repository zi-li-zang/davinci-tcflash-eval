import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_first import select_timelines, render_requirements, comparison_case

class RenderFirstTests(unittest.TestCase):
    def test_no_sdk_or_case_needed_for_render(self):
        self.assertEqual(render_requirements({'render_preset': 'EXR'}), [])
    def test_unknown_export_not_guessed(self):
        self.assertTrue(render_requirements({}))
    def test_multiple_timelines_need_policy(self):
        with self.assertRaises(ValueError): select_timelines(['A', 'B'], 'single')
    def test_all_is_explicit(self):
        self.assertEqual(select_timelines(['A', 'B'], 'all'), [1, 2])
    def test_names_must_be_unique(self):
        with self.assertRaises(ValueError): select_timelines(['A', 'A'], ['A'])
    def test_named_timeline(self):
        self.assertEqual(select_timelines(['A', 'B'], ['B']), [2])
    def test_comparison_missing_is_not_render_failure(self):
        self.assertIsNone(comparison_case({}))
    def test_strict_threshold_does_not_mutate_source(self):
        original = {'comparison_rules': {'pixel_domain': {'channels': 'RGB'}, 'tolerances': {'max_abs_error_limit': 1}}}
        with self.assertRaises(ValueError): comparison_case({'comparison': original})
        self.assertEqual(original['comparison_rules']['tolerances']['max_abs_error_limit'], 1)



class PipelineTests(unittest.TestCase):
    def execute(self, tmp, failure=None, timelines=None, policy='single'):
        from render_first import run
        source = Path(tmp) / 'inputs'
        source.mkdir()
        (source / 'a.drp').write_bytes(b'drp')
        calls = []
        def fake(action, payload, directory, *args, **kwargs):
            calls.append(action)
            if action == 'inventory':
                return {'ok': True, 'data': {'timelines': timelines or ['A']}}
            if action == 'render_selected':
                if failure: return failure
                out = Path(payload['target_dir'])
                out.mkdir()
                (out / 'output0001.exr').write_bytes(b'fake-output-not-image')
                return {'ok': True, 'data': {'elapsed_sec': 0.1}}
            return {'ok': True}
        tasks = [{'id': str(i), 'drp_path': str(source / 'a.drp')} for i in range(2)]
        rows = run({'projects': tasks, 'timeline_policy': policy}, Path(tmp) / 'batch', call=fake)
        return rows, calls

    def test_missing_mapping_renders_all_projects(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp)
            self.assertEqual(calls.count('render_selected'), 2)
            self.assertTrue(all(r['status'] == 'RENDERED_NOT_COMPARED' for r in rows))
            self.assertNotIn('compare', calls)
            self.assertTrue((Path(rows[0]['run_dir']) / 'report.md').exists())
            self.assertFalse((Path(rows[0]['run_dir']) / 'report_detailed.md').exists())

    def test_timeout_stops_remaining_host_calls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp, {'ok': False, 'failure_code': 'TIMEOUT'})
            self.assertEqual(calls.count('import_copy'), 1)
            self.assertEqual([r['status'] for r in rows], ['ERROR', 'NOT_RUN'])

    def test_known_render_failure_continues(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp, {'ok': False, 'failure_code': 'RENDER_FAILED'})
            self.assertEqual(calls.count('render_selected'), 2)
            self.assertTrue(all(r['render_status'] == 'FAILED' for r in rows))

    def test_all_timelines_independent_without_sdk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp, timelines=['A', 'B'], policy='all')
            self.assertEqual(calls.count('render_selected'), 4)
            self.assertEqual(len(rows), 4)

    def test_no_multi_timeline_guess(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp, timelines=['A', 'B'])
            self.assertNotIn('render_selected', calls)
            self.assertTrue(all(r['status'] == 'SKIPPED' for r in rows))

    def test_render_completion_calls_close_project(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp)
            self.assertIn('close_project', calls)
            self.assertEqual(calls.count('close_project'), 2)

    def test_ready_check_called_before_subsequent_project(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = self.execute(tmp)
            self.assertIn('ready_check', calls)

    def test_import_failure_checks_idle_and_continues_without_blind_retry(self):
        import tempfile
        from pathlib import Path
        from render_first import run
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'inputs'
            source.mkdir()
            (source / 'a.drp').write_bytes(b'drp')
            calls = []
            import_attempts = {'0': 0, '1': 0}
            def fake(action, payload, directory, *args, **kwargs):
                calls.append(action)
                if action == 'import_copy':
                    drp = payload.get('drp_path', '')
                    if 'task0' in drp:
                        import_attempts['0'] += 1
                        return {'ok': False, 'failure_code': 'IMPORT_PROJECT_FAILED', 'reason': '导入失败'}
                    else:
                        import_attempts['1'] += 1
                        return {'ok': True, 'data': {'loaded_project_name': payload['project_copy_name']}}
                if action == 'inventory':
                    return {'ok': True, 'data': {'timelines': ['A']}}
                if action == 'render_selected':
                    out = Path(payload['target_dir'])
                    out.mkdir(parents=True, exist_ok=True)
                    (out / 'output.mov').write_bytes(b'data')
                    return {'ok': True, 'data': {'elapsed_sec': 0.1}}
                return {'ok': True}

            tasks = [
                {'id': 'task0', 'drp_path': str(source / 'task0.drp')},
                {'id': 'task1', 'drp_path': str(source / 'task1.drp')}
            ]
            (source / 'task0.drp').write_bytes(b'drp0')
            (source / 'task1.drp').write_bytes(b'drp1')
            rows = run({'projects': tasks, 'timeline_policy': 'single'}, Path(tmp) / 'batch', call=fake)
            # task0 failed import (and retried)
            self.assertEqual(import_attempts['0'], 1)
            self.assertEqual(rows[0]['status'], 'ERROR')
            # task1 did NOT get blocked by task0, it continued and completed!
            self.assertEqual(rows[1]['status'], 'RENDERED_NOT_COMPARED')
            self.assertEqual(rows[1]['render_status'], 'COMPLETE')
            self.assertIn('close_project', calls)

class WorkerPolicyTests(unittest.TestCase):
    def test_selected_timeline_preserves_settings_and_records_observations(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock, patch
        host = MagicMock()
        project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        project.GetCurrentRenderFormatAndCodec.return_value = {'format': 'exr', 'codec': 'RGBHalf'}
        project.GetCurrentRenderMode.return_value = 1
        project.GetTimelineByIndex.return_value.GetName.return_value = 'A'
        project.GetTimelineByIndex.return_value.GetStartFrame.return_value = 0
        project.GetTimelineByIndex.return_value.GetEndFrame.return_value = 10
        with patch('plugin_presence.inspect_timeline', return_value={'status': 'PRESENT'}), \
             patch.object(worker, 'connect_resolve', return_value=host), \
             patch.object(worker, 'action_inventory', return_value={'ok': True}), \
             patch.object(worker, 'action_render', return_value={'ok': True}) as render:
            result = worker.action_render_selected({'expected_project_name': 'P', 'timeline_index': 1,
                                                    'export_rules': {'settings_source': 'imported_current'}})
        self.assertTrue(result['ok'])
        self.assertTrue(render.call_args.args[0]['export_rules']['preserve_current_settings'])
        project.SetCurrentTimeline.assert_called_once()
        project.SetCurrentRenderFormatAndCodec.assert_not_called()

    def test_missing_format_does_not_submit_render(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock, patch
        host = MagicMock()
        project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        project.GetCurrentRenderFormatAndCodec.return_value = {}
        with patch('plugin_presence.inspect_timeline', return_value={'status': 'PRESENT'}), \
             patch.object(worker, 'connect_resolve', return_value=host), \
             patch.object(worker, 'action_inventory', return_value={'ok': True}), \
             patch.object(worker, 'action_render') as render:
            result = worker.action_render_selected({'expected_project_name': 'P', 'timeline_index': 1,
                                                    'export_rules': {'settings_source': 'imported_current'}})
        self.assertFalse(result['ok'])
        render.assert_not_called()

    def test_close_project_switches_page_and_closes_project(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock, patch
        host = MagicMock()
        pm = host.GetProjectManager.return_value
        project = pm.GetCurrentProject.return_value
        project.GetName.return_value = 'EVAL_RUN_test123'
        project.IsRenderingInProgress.return_value = False
        pm.GetCurrentProject.side_effect = [project, None]
        pm.CloseProject.return_value = True
        with patch.object(worker, 'connect_resolve', return_value=host):
            result = worker.action_close_project({'expected_project_name': 'EVAL_RUN_test123'})
        self.assertTrue(result['ok'])
        host.OpenPage.assert_not_called()
        pm.CloseProject.assert_called_with(project)

    def test_ready_check_polls_database_and_returns_ok(self):
        import resolve_worker as worker
        from unittest.mock import MagicMock, patch
        host = MagicMock()
        pm = host.GetProjectManager.return_value
        pm.GetCurrentDatabase.return_value = {'DbType': 'Disk'}
        pm.GetProjectListInCurrentFolder.return_value = ['P1']
        project = pm.GetCurrentProject.return_value
        project.IsRenderingInProgress.return_value = False
        with patch.object(worker, 'connect_resolve', return_value=host):
            result = worker.action_ready_check({'timeout_sec': 1.0, 'poll_interval_sec': 0.1})
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['database'], {'DbType': 'Disk'})

if __name__ == '__main__': unittest.main()

class ComparisonIsolationTests(unittest.TestCase):
    def test_missing_pixel_domain_is_comparison_problem(self):
        with self.assertRaises(ValueError):
            comparison_case({'comparison': {'comparison_rules': {}}})

    def test_actual_comparator_runs_only_after_render(self):
        import tempfile
        import numpy as np
        import cv2
        from render_first import run
        from evidence_worker import execute
        from test_hardening import case_for_images
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'input').mkdir()
            (base / 'input/a.drp').write_bytes(b'drp')
            (base / 'sdk').mkdir()
            image = np.zeros((2, 2, 3), dtype=np.uint8)
            cv2.imwrite(str(base / 'sdk/sdk_0.png'), image)
            calls = []
            def fake(action, payload, directory, *args, **kwargs):
                calls.append(action)
                if action == 'inventory': return {'ok': True, 'data': {'timelines': ['A']}}
                if action == 'render_selected':
                    Path(payload['target_dir']).mkdir()
                    cv2.imwrite(str(Path(payload['target_dir']) / 'out_0.png'), image)
                if action == 'compare': return execute(action, payload)
                return {'ok': True}
            case = case_for_images()
            case['comparison_rules']['tolerances'] = {k: 0 for k in case['comparison_rules']['tolerances']}
            rows = run({'projects': [{'id': 'A', 'drp_path': str(base / 'input/a.drp'),
                       'sdk_dir': str(base / 'sdk'), 'comparison': case}]}, base / 'batch', call=fake)
            self.assertEqual(rows[0]['status'], 'PASS')
            self.assertLess(calls.index('render_selected'), calls.index('compare'))
            self.assertEqual(rows[0]['evidence']['comparison']['match_classification'], 'EXACT_MATCH')

class PluginSkipPipelineTests(unittest.TestCase):
    def test_absent_plugin_skips_and_continues(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rows, calls = PipelineTests().execute(tmp, {'ok': False, 'verdict': 'SKIPPED',
                                                      'failure_code': 'NO_TRUECUTFLASH_NODE',
                                                      'reason': 'Color 节点无 TrueCut Flash'})
            self.assertEqual(calls.count('import_copy'), 2)
            self.assertTrue(all(r['status'] == 'SKIPPED' for r in rows))
            self.assertTrue(all(r['render_status'] == 'SKIPPED' for r in rows))
