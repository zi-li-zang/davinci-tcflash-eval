import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from export_policy import sdk_export_rules, resolve_export_rules


class ExportPolicyTests(unittest.TestCase):
    def test_jpeg_aliases_select_real_host_codec(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('a.jpg', 'b.jpeg', '._bad.exr'):
                (Path(tmp) / name).touch()
            rules = sdk_export_rules({'settings_source': 'sdk_aligned'}, tmp)
            project = MagicMock()
            project.GetRenderFormats.return_value = {'JPEG': 'jpg', 'OpenEXR': 'exr'}
            project.GetRenderCodecs.return_value = {'RGB 8 bit': 'RGB8'}
            selected = resolve_export_rules(project, rules)
            self.assertEqual((selected['format'], selected['codec']), ('jpg', 'RGB8'))
            self.assertFalse(selected['preserve_current_settings'])
            project.GetCurrentRenderFormatAndCodec.assert_not_called()

    def test_mixed_formats_and_explicit_conflict_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a.exr').touch()
            with self.assertRaisesRegex(ValueError, 'CONFLICT'):
                sdk_export_rules({'settings_source': 'sdk_aligned', 'format': 'jpg'}, tmp)
            (Path(tmp) / 'b.jpg').touch()
            with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
                sdk_export_rules({'settings_source': 'sdk_aligned'}, tmp)

    def test_exr_codec_requires_evidence_when_multiple_choices(self):
        project = MagicMock()
        project.GetRenderFormats.return_value = {'OpenEXR': 'exr'}
        project.GetRenderCodecs.return_value = {'RGB half': 'RGBHalf', 'RGBA float': 'RGBAFloat'}
        rules = {'settings_source': 'sdk_aligned', 'sdk_image_family': 'exr'}
        with self.assertRaisesRegex(ValueError, 'SDK_EXPORT_CODEC_REQUIRED'):
            resolve_export_rules(project, rules)
        selected = resolve_export_rules(project, dict(rules, codec='RGBAFloat', image_settings={'ExportAlpha': True}))
        self.assertEqual(selected['codec'], 'RGBAFloat')
        self.assertTrue(selected['image_settings']['ExportAlpha'])

    def test_missing_sdk_can_use_explicit_export_without_comparison(self):
        explicit = {'format': 'exr', 'codec': 'RGBFloat'}
        self.assertEqual(sdk_export_rules(explicit, None), explicit)
        with self.assertRaisesRegex(ValueError, 'SOURCE_MISSING'):
            sdk_export_rules({'settings_source': 'sdk_aligned'}, None)

    def test_host_format_unavailable_is_actionable(self):
        project = MagicMock()
        project.GetRenderFormats.return_value = {'QuickTime': 'mov'}
        with self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
            resolve_export_rules(project, {'settings_source': 'sdk_aligned', 'sdk_image_family': 'exr'})

    def test_render_cannot_enqueue_when_format_setter_does_not_take_effect(self):
        import resolve_worker as worker
        from unittest.mock import patch
        host = MagicMock()
        project = host.GetProjectManager.return_value.GetCurrentProject.return_value
        project.GetName.return_value = 'P'
        project.IsRenderingInProgress.return_value = False
        project.GetCurrentRenderFormatAndCodec.return_value = {'format': 'mov', 'codec': 'old'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker, 'connect_resolve', return_value=host):
            result = worker.action_render({'expected_project_name': 'P', 'export_rules': {'format': 'jpg', 'codec': 'RGB8'},
                                           'target_dir': tmp, 'custom_name': 'output'})
        self.assertEqual(result['failure_code'], 'EXPORT_FORMAT_READBACK_MISMATCH')
        project.AddRenderJob.assert_not_called()
