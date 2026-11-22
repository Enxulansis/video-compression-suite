"""Serial phased detection, isolated fresh modules and bounded synthetic workers."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parent.parent / 'backend/core/caps.py'

class StartupPhases(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('owned_phase_caps', PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.release = threading.Event()
        self.secondary = threading.Event()
        self.calls = []
        self.fail_secondary = False
        names = [n for n in self.module.DISPLAY_ENCODERS if n.endswith(self.module.HARDWARE_SUFFIXES)]
        self.names = names
        def read(argv, **kwargs):
            if '-encoders' in argv:
                return '\n'.join(' V..... ' + n + ' fixture' for n in names)
            if '-h' in argv:
                return 'Supported pixel formats: yuv420p p010le'
            return 'ffmpeg version fixture'
        self.read = read
        def test(name, fmt, deadline):
            self.calls.append((name, fmt, threading.get_ident()))
            if name == 'av1_qsv':
                self.secondary.set()
                if not self.release.wait(2):
                    raise TimeoutError('test worker not released')
                if self.fail_secondary:
                    raise RuntimeError('fixture secondary failure')
            return {'ok': name != 'av1_qsv', 'error': '' if name != 'av1_qsv' else 'fixture unavailable'}
        self.enterContext(patch.object(self.module, '_find_binary', return_value='owned-fixture'))
        self.enterContext(patch.object(self.module, '_run', side_effect=read))
        self.enterContext(patch.object(self.module.Capabilities, '_test_hardware', side_effect=test))
        def finish():
            self.release.set()
            if self.module._CAPS_LOADING:
                self.module.get_caps(wait_for_all=True)
        self.addCleanup(finish)

    def primary(self):
        result = self.module.get_caps()
        self.assertTrue(self.secondary.wait(1))
        return result

    def test_required_runtime_and_ten_bit_are_verified_before_ready(self):
        caps = self.primary()
        for name in ('h264_nvenc', 'hevc_nvenc', 'av1_nvenc'):
            self.assertTrue(caps.has_encoder(name))
        for name in ('hevc_nvenc', 'av1_nvenc'):
            self.assertTrue(caps.supports_pix_fmt(name, 'p010le'))
        self.assertFalse(caps.has_encoder('av1_qsv'))
        self.assertFalse(caps.supports_pix_fmt('av1_qsv', 'yuv420p'))
        self.assertFalse(caps.to_dict()['hardware_complete'])
        self.assertEqual(set(caps.hardware_pending), {'av1_qsv', 'h264_qsv', 'hevc_qsv', 'av1_amf'})

    def test_final_snapshot_replaces_without_mutating_earlier_plan_inputs(self):
        primary = self.primary()
        before = json.dumps(primary.to_dict(), sort_keys=True)
        self.release.set()
        full = self.module.get_caps(wait_for_all=True)
        self.assertIsNot(full, primary)
        self.assertTrue(full.to_dict()['hardware_complete'])
        self.assertEqual(before, json.dumps(primary.to_dict(), sort_keys=True))
        self.assertEqual(len({c[2] for c in self.calls}), 1)
        self.assertEqual(len(self.calls), 9)

    def test_concurrent_callers_share_one_primary_and_one_probe(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.module.get_caps())) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(1)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(results), 6)
        self.assertTrue(all(item is results[0] for item in results))
        self.assertEqual(len([c for c in self.calls if c[:2] == ('hevc_nvenc', 'p010le')]), 1)

    def test_full_wait_does_not_finish_at_primary_milestone(self):
        self.primary()
        done = threading.Event()
        thread = threading.Thread(target=lambda: (self.module.get_caps(wait_for_all=True), done.set()))
        thread.start()
        self.assertFalse(done.wait(.03))
        self.release.set()
        thread.join(1)
        self.assertTrue(done.is_set())

    def test_secondary_exception_keeps_confirmed_capabilities_and_reports_failure(self):
        self.fail_secondary = True
        first = self.primary()
        self.release.set()
        full = self.module.get_caps(wait_for_all=True)
        self.assertTrue(full.has_encoder('hevc_nvenc'))
        self.assertFalse(full.has_encoder('hevc_qsv'))
        self.assertIn('fixture secondary failure', full.supplemental_error)
        self.assertTrue(full.to_dict()['hardware_complete'])
        self.assertIn('av1_qsv', first.hardware_pending)

    def test_custom_profile_required_hardware_is_not_published_early(self):
        with patch.object(self.module, '_primary_hardware', return_value={'hevc_nvenc', 'av1_qsv'}):
            done = threading.Event()
            thread = threading.Thread(target=lambda: (self.module.get_caps(), done.set()))
            thread.start()
            self.assertTrue(self.secondary.wait(1))
            self.assertFalse(done.is_set())
            self.release.set()
            thread.join(1)
            self.assertTrue(done.is_set())

    def test_profile_hardware_outside_display_list_is_still_verified_first(self):
        def read(argv, **kwargs):
            if '-encoders' in argv:
                return '\n'.join(' V..... ' + n + ' fixture' for n in self.names + ['h264_amf'])
            return self.read(argv, **kwargs)
        required = {'hevc_nvenc', 'av1_nvenc', 'h264_nvenc', 'h264_amf'}
        with patch.object(self.module, '_primary_hardware', return_value=required), \
             patch.object(self.module, '_run', side_effect=read):
            result = self.primary()
            self.assertTrue(result.has_encoder('h264_amf'))
            self.assertIn(('h264_amf', 'yuv420p'), [c[:2] for c in self.calls])
            self.release.set()
            self.module.get_caps(wait_for_all=True)

    def test_refresh_waits_for_old_worker_and_returns_complete_new_snapshot(self):
        previous = self.primary()
        self.release.set()
        refreshed = self.module.get_caps(refresh=True)
        self.assertIsNot(previous, refreshed)
        self.assertTrue(refreshed.to_dict()['hardware_complete'])
        self.assertEqual(len(self.calls), 18)

    def test_primary_failure_is_not_reported_ready(self):
        with patch.object(self.module.Capabilities, 'probe', side_effect=RuntimeError('fixture primary failure')):
            with self.assertRaisesRegex(RuntimeError, 'primary failure'):
                self.module.get_caps()
            self.assertIsNone(self.module._CAPS)

    def test_failed_worker_start_does_not_leave_cache_permanently_loading(self):
        with patch.object(self.module.threading.Thread, 'start', side_effect=RuntimeError('fixture thread failure')):
            with self.assertRaisesRegex(RuntimeError, 'thread failure'):
                self.module.get_caps()
        self.assertFalse(self.module._CAPS_LOADING)

    def test_detector_keeps_python_alive_until_bounded_child_cleanup(self):
        original = self.module.threading.Thread
        with patch.object(self.module.threading, 'Thread', wraps=original) as factory:
            self.primary()
        self.assertIs(factory.call_args.kwargs['daemon'], False)

    def test_serial_probe_reservation_is_released_after_complete_detection(self):
        self.primary()
        self.assertEqual(self.module.startup_probe_reserve_mb(), 512)
        self.release.set()
        self.module.get_caps(wait_for_all=True)
        self.assertEqual(self.module.startup_probe_reserve_mb(), 0)

if __name__ == '__main__':
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StartupPhases)).wasSuccessful() else 1)
