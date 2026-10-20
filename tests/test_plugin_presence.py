import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from plugin_presence import inspect_timeline


def graph(tools):
    g = Mock()
    g.GetNumNodes.return_value = 1
    g.GetToolsInNode.return_value = tools
    return g


class PresenceTests(unittest.TestCase):
    def setup_host(self, tools):
        p, t, clip = Mock(), Mock(), Mock()
        p.GetSetting.return_value = '1'
        t.GetNodeGraph.return_value = graph([])
        t.GetTrackCount.return_value = 1
        t.GetItemListInTrack.return_value = [clip]
        clip.GetNodeGraph.return_value = graph(tools)
        clip.GetColorGroup.return_value = None
        return p, t, clip

    def test_present(self):
        p, t, _ = self.setup_host(['OFX: TrueCut Flash'])
        self.assertEqual(inspect_timeline(p, t)['status'], 'PRESENT')

    def test_node_label_is_not_plugin(self):
        p, t, c = self.setup_host(['Color Corrector'])
        c.GetNodeGraph.return_value.GetNodeLabel.return_value = 'TrueCut Flash'
        self.assertEqual(inspect_timeline(p, t)['status'], 'ABSENT')

    def test_unknown_not_absent(self):
        p, t, c = self.setup_host([])
        c.GetNodeGraph.return_value.GetToolsInNode.return_value = None
        self.assertEqual(inspect_timeline(p, t)['status'], 'UNKNOWN')

    def test_group_plugin(self):
        p, t, c = self.setup_host([])
        group = Mock()
        c.GetColorGroup.return_value = group
        group.GetPreClipNodeGraph.return_value = graph(['com.pixelworks.truecutflash.ofx'])
        group.GetPostClipNodeGraph.return_value = graph([])
        self.assertEqual(inspect_timeline(p, t)['status'], 'PRESENT')

    def test_other_stack_layer(self):
        p, t, c = self.setup_host([])
        p.GetSetting.return_value = '2'
        c.GetNodeGraph.side_effect = [graph([]), graph(['TrueCut Flash'])]
        self.assertEqual(inspect_timeline(p, t)['status'], 'PRESENT')

class RenderGuardTests(unittest.TestCase):
    def test_no_plugin_never_creates_render_job(self):
        import resolve_worker as worker
        from unittest.mock import patch
        host = Mock()
        project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        project.SetCurrentTimeline.return_value = True
        for status, code in [('ABSENT', 'NO_TRUECUTFLASH_NODE'), ('UNKNOWN', 'PLUGIN_INSPECTION_UNAVAILABLE')]:
            with self.subTest(status=status), \
                 patch.object(worker, 'connect_resolve', return_value=host), \
                 patch.object(worker, 'action_inventory', return_value={'ok': True}), \
                 patch('plugin_presence.inspect_timeline', return_value={'status': status}), \
                 patch.object(worker, 'action_render') as render:
                result = worker.action_render_selected({'expected_project_name': 'P', 'timeline_index': 1,
                                                        'export_rules': {'settings_source': 'imported_current'}})
                self.assertEqual(result['verdict'], 'SKIPPED' if status == 'ABSENT' else 'BLOCKED')
                self.assertEqual(result['failure_code'], code)
                render.assert_not_called()
                project.AddRenderJob.assert_not_called()

class NullToolHostRegressionTests(unittest.TestCase):
    def test_empty_first_node_does_not_hide_second_plugin(self):
        p, t, clip = PresenceTests().setup_host([])
        clip.GetNodeGraph.return_value.GetNumNodes.return_value = 2
        clip.GetNodeGraph.return_value.GetToolsInNode.side_effect = [None, ['OFX: TrueCut Flash']]
        self.assertEqual(inspect_timeline(p, t)['status'], 'PRESENT')
