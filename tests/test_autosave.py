"""Host-free save protection and static-first batch tests."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import resolve_worker as worker


class AutoSaveTests(unittest.TestCase):
    def host(self):
        resolve = Mock(); resolve.GetVersionString.return_value = '21.1'
        pm = resolve.GetProjectManager.return_value
        project = pm.GetCurrentProject.return_value
        project.GetName.return_value = 'Untitled Project'
        project.GetUniqueId.return_value = 'project-1'
        project.IsRenderingInProgress.return_value = False
        pm.SaveProject.return_value = True
        return resolve, pm, project

    def test_save_before_switch(self):
        resolve, pm, project = self.host()
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertTrue(result['ok'], result)
        pm.SaveProject.assert_called_once()
        self.assertEqual(result['data']['save_current_project']['status'], 'SAVED')

    def test_failed_save_blocks(self):
        resolve, pm, project = self.host(); pm.SaveProject.return_value = False
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'PROJECT_SAVE_FAILED')

    def test_project_change_blocks(self):
        resolve, pm, project = self.host()
        project.GetUniqueId.side_effect = ['project-1', 'project-2']
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'ENVIRONMENT_CHANGED')

    def test_rendering_never_saves(self):
        resolve, pm, project = self.host(); project.IsRenderingInProgress.return_value = True
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'HOST_BUSY')
        pm.SaveProject.assert_not_called()

    def test_no_authorization_never_saves(self):
        resolve, pm, project = self.host()
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({})
        self.assertEqual(result['failure_code'], 'USER_PROJECT_ACTIVE')
        pm.SaveProject.assert_not_called()


    def test_unknown_render_state_never_saves(self):
        resolve, pm, project = self.host()
        project.IsRenderingInProgress.return_value = None
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'HOST_STATE_UNKNOWN')
        pm.SaveProject.assert_not_called()

    def test_unknown_render_state_after_save_blocks(self):
        resolve, pm, project = self.host()
        project.IsRenderingInProgress.side_effect = [False, None]
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'HOST_STATE_UNKNOWN')

    def test_render_starts_after_save_blocks_switch(self):
        resolve, pm, project = self.host()
        project.IsRenderingInProgress.side_effect = [False, True]
        with patch.object(worker, 'connect_resolve', return_value=resolve):
            result = worker.action_preflight({'save_current_project': True})
        self.assertEqual(result['failure_code'], 'HOST_BUSY')
