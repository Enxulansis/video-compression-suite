"""Isolated synthetic metadata/HTTP tests; no FFmpeg, scheduler or real store."""
import copy
import hashlib
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resource_guard import GuardRegression, J, M, S, server, MediaInfo, StreamInfo
from core import planner as P, caps as C, batch as B


class SimpleConsole(GuardRegression):
    def setUp(self):
        super().setUp()
        self.caps = C.Capabilities()
        self.caps.ffmpeg, self.caps.ffprobe = 'ffmpeg', 'ffprobe'
        self.caps.encoders = list(P.SIMPLE_QUALITY)
        self.caps.hardware_probes = {n: {'ok': True, 'ten_bit': {'ok': True}}
                                    for n in self.caps.encoders if n.endswith('_nvenc')}
        self.caps.pixel_formats = {n: ['yuv420p', 'p010le'] for n in self.caps.hardware_probes}
        for obj, name, value in ((C, 'get_caps', self.caps), (server, 'get_caps', self.caps)):
            mock = patch.object(obj, name, return_value=value)
            mock.start(); self.addCleanup(mock.stop)
        # Any accidental real media execution is a test failure.
        mock = patch('subprocess.Popen', side_effect=AssertionError('real process forbidden'))
        mock.start(); self.addCleanup(mock.stop)

    def media(self, name='fixture.mp4', hdr=False, width=1920, height=1080):
        src = Path(self.tmp.name) / name
        src.write_bytes(b'isolated input sentinel')
        return MediaInfo(str(src), ok=True, duration=450, size_bytes=2400000000,
                         streams=[StreamInfo(0, 'video', codec='h264', width=width,
                             height=height, fps=30, pix_fmt='yuv420p10le' if hdr else 'yuv420p',
                             bit_depth=10 if hdr else 8, bit_rate=42000000,
                             color_transfer='smpte2084' if hdr else '')])

    def ov(self, goal='balanced', compatibility='modern', slow=False, **kw):
        return P.Overrides(intent={'goal': goal, 'compatibility': compatibility, 'slow_av1': slow}, **kw)

    def plan(self, info=None, **kw):
        return P.make_plan(info or self.media(), P.get_profile('P3'), self.ov(**kw), caps=self.caps)

    def test_default_is_three_goals_and_modern(self):
        self.assertEqual(P.validate_intent({'goal': 'balanced'}),
                         {'goal': 'balanced', 'compatibility': 'modern', 'slow_av1': False})
        self.assertFalse(S.DEFAULTS['show_advanced']); self.assertFalse(S.DEFAULTS['show_extreme'])
        self.assertFalse(S.DEFAULTS['slow_av1'])

    def test_each_strength_uses_same_hevc_with_ordered_quality(self):
        info = self.media()
        plans = [self.plan(info, goal=g) for g in ('light', 'balanced', 'strong')]
        self.assertEqual([p.encoder for p in plans], ['hevc_nvenc'] * 3)
        self.assertEqual([M.last_option(p.argv, ('-cq',)) for p in plans], ['24', '28', '32'])
        self.assertGreater(plans[0].est_output_bytes, plans[1].est_output_bytes)
        self.assertGreater(plans[1].est_output_bytes, plans[2].est_output_bytes)

    def test_broad_sdr_uses_h264(self):
        self.assertEqual(self.plan(compatibility='broad').encoder, 'h264_nvenc')

    def test_broad_hdr_blocks_instead_of_tone_mapping(self):
        with self.assertRaisesRegex(P.PlanError, 'HDR'):
            self.plan(self.media(hdr=True), compatibility='broad')

    def test_av1_permission_does_not_force_light_av1(self):
        self.assertEqual(self.plan(compatibility='av1', goal='light').encoder, 'hevc_nvenc')
        self.assertEqual(self.plan(compatibility='av1').encoder, 'av1_nvenc')

    def test_unconfirmed_hardware_registration_is_not_selected(self):
        self.caps.hardware_probes.pop('av1_nvenc')
        self.assertEqual(self.plan(compatibility='av1').encoder, 'hevc_nvenc')

    def test_failed_av1_does_not_silently_use_svt(self):
        self.caps.hardware_probes['av1_nvenc']['ok'] = False
        self.assertEqual(self.plan(goal='strong', compatibility='av1').encoder, 'hevc_nvenc')
        self.assertEqual(self.plan(goal='balanced', compatibility='av1', slow=True).encoder, 'hevc_nvenc')
        plan = self.plan(goal='strong', compatibility='av1', slow=True)
        self.assertEqual(plan.encoder, 'libsvtav1')
        self.assertEqual(M.last_option(plan.argv, ('-preset',)), '6')
        self.assertEqual(M.last_option(plan.argv, ('-crf',)), '40')

    def test_tiny_source_skips_hardware(self):
        self.assertEqual(self.plan(self.media(width=128, height=96)).encoder, 'libx265')

    def test_hdr_requires_runtime_ten_bit(self):
        self.caps.hardware_probes['hevc_nvenc']['ten_bit']['ok'] = False
        self.assertEqual(self.plan(self.media(hdr=True)).encoder, 'libx265')
        self.caps.encoders.remove('libx265')
        with self.assertRaisesRegex(P.PlanError, '位深'):
            self.plan(self.media(hdr=True))

    def test_hdr_plan_preserves_ten_bit_without_resize(self):
        plan = self.plan(self.media(hdr=True, width=3840, height=2160), goal='strong')
        self.assertEqual(M.last_option(plan.argv, ('-pix_fmt',)), 'p010le')
        self.assertEqual(M.last_option(plan.argv, ('-profile:v',)), 'main10')
        self.assertNotIn('-vf', plan.argv)
        self.assertNotIn('-r', plan.argv)

    def test_unsupported_hdr_precision_is_explicit(self):
        info = self.media(hdr=True); info.video.bit_depth = 12
        with self.assertRaisesRegex(P.PlanError, '精度'): self.plan(info)

    def test_complex_content_uses_mkv_and_preserves_mappings(self):
        for codec in ('hdmv_pgs_subtitle', 'dvd_subtitle', 'ass'):
            info = self.media()
            info.streams += [StreamInfo(1, 'audio', codec='flac'), StreamInfo(2, 'subtitle', codec=codec),
                             StreamInfo(3, 'attachment', codec='ttf')]
            plan = self.plan(info)
            self.assertTrue(plan.out_path.endswith('.mkv'))
            self.assertIn('0:t?', plan.argv)
            self.assertEqual(M.last_option(plan.argv, ('-c:a:0',)), 'copy')
            self.assertEqual(M.last_option(plan.argv, ('-c:s:0',)), 'copy')

    def test_simple_controls_cannot_inherit_loss_or_unsafe(self):
        plan = self.plan(max_long_edge=640, hdr_to_sdr=True, strip_metadata=True,
                         audio_mode='drop', subtitle_mode='drop', allow_unsafe=True,
                         extra_args=['-g', '9999'], crf=60, loudness=-16, autocrop=True)
        self.assertNotIn('-vf', plan.argv); self.assertNotIn('-an', plan.argv)
        self.assertEqual(plan.resolved_overrides['audio_mode'], 'copy')
        self.assertFalse(plan.resolved_overrides['allow_unsafe'])
        self.assertIn('-map_metadata', plan.argv)
        self.assertEqual(M.last_option(plan.argv, ('-g',)), '30')

    def test_simple_sanitized_before_auxiliary_submission_work(self):
        ov = J.JobManager._make_overrides(self.ov(loudness=-16, autocrop=True).to_dict())
        self.assertFalse(ov.autocrop); self.assertEqual(ov.loudness, 0)
        self.assertTrue(ov.intent)

    def test_low_memory_does_not_change_encoder_or_quality(self):
        with patch.object(M, 'machine_memory', return_value={'available': 1, 'free_mb': 800, 'total_mb': 32000}):
            plan = self.plan(self.media(hdr=True, width=3840, height=2160), goal='strong')
        self.assertEqual(plan.encoder, 'hevc_nvenc')
        self.assertEqual(M.last_option(plan.argv, ('-cq',)), '32')
        self.assertTrue(any('等待内存' in w or '等待' in w for w in plan.warnings))

    def test_batch_matches_final_plan_and_sum_of_ranges(self):
        infos = [self.media(), self.media('hdr.mkv', hdr=True)]
        manager = self.manager()
        with patch.object(J, 'get_manager', return_value=manager):
            data = B.estimate_batch(infos, P.get_profile('P3'), self.ov())
        for info, row in zip(infos, data['rows']):
            plan = self.plan(info)
            self.assertEqual(row['est_bytes'], plan.est_output_bytes)
            self.assertEqual(row['est_memory_mb'], plan.est_memory_mb)
        self.assertEqual(data['totals']['est_low'], sum(r['est_low'] for r in data['rows']))
        self.assertEqual(data['totals']['est_high'], sum(r['est_high'] for r in data['rows']))

    def test_batch_conflict_is_partial_not_full_benefit(self):
        infos = [self.media(), self.media('hdr.mkv', hdr=True)]
        with patch.object(J, 'get_manager', return_value=self.manager()):
            data = B.estimate_batch(infos, P.get_profile('P3'), self.ov(compatibility='broad'))
        self.assertFalse(data['complete']); self.assertEqual(len(data['blocked']), 1)
        self.assertEqual(data['files'], 1); self.assertEqual(data['requested'], 2)

    def test_explicit_per_file_manual_profile_overrides_auto(self):
        info = self.media()
        profiles = {p['id']: p for p in P.load_profiles()}
        profile, ov = B.resolve_per_file(info, P.get_profile('P3'), self.ov(),
                        {info.path: {'profile': 'A2', 'crf': 40}}, profiles)
        self.assertEqual(profile['id'], 'A2'); self.assertFalse(ov.intent)

    def test_invalid_intent_rejected(self):
        for raw in ([], 'strong', {'goal': 'extreme'}, {'compatibility': 'anything'},
                    {'slow_av1': 'false'}, {'allow_unsafe': True}):
            with self.subTest(raw=raw), self.assertRaises(ValueError): P.validate_intent(raw)

    def test_visibility_settings_do_not_mutate_queue_or_thresholds(self):
        manager = self.manager(); info = self.media()
        with patch.object(J, 'probe', return_value=info), patch.object(manager, '_kick'):
            job = manager.submit([info.path], 'P3', self.ov().to_dict())[0]
        before = Path(J.QUEUE_FILE).read_bytes()
        status, data = self.request(manager, {'show_advanced': True, 'show_extreme': True,
                                              'compatibility': 'av1', 'slow_av1': True})
        self.assertEqual(status, 200, data)
        self.assertEqual(Path(J.QUEUE_FILE).read_bytes(), before)
        self.assertEqual(manager.memory_policy()['margin_mb'], 2500)
        self.assertEqual(job['overrides']['allow_unsafe'], False)
        S._CACHE.clear(); self.assertTrue(S.load()['show_extreme'])

    def test_invalid_settings_are_atomic(self):
        manager = self.manager(); before = S.load()
        for raw in ({'show_advanced': 'false'}, {'show_extreme': 1}, {'slow_av1': {}},
                    {'show_advanced': True, 'compatibility': 'bad'}):
            status, data = self.request(manager, raw)
            self.assertEqual(status, 400, data); self.assertEqual(S.load(), before)

    def test_http_plan_submit_restore_share_resolved_parameters(self):
        manager = self.manager(); info = self.media('complex.mkv', hdr=True)
        info.streams.append(StreamInfo(1, 'subtitle', codec='hdmv_pgs_subtitle'))
        body = {'path': info.path, 'profile': 'P3', 'overrides': self.ov(goal='strong').to_dict()}
        # Internal-only measurements are never sent by the UI.
        body['overrides'].pop('loudness_measured')
        with patch.object(server, '_is_allowed', return_value=True), patch.object(server, 'probe', return_value=info), patch.object(J, 'probe', return_value=info), patch.object(manager, '_kick'):
            status, payload = self.request(manager, body, '/api/plan')
            self.assertEqual(status, 200, payload)
            status, submitted_payload = self.request(manager,
                {'paths': [info.path], 'profile': 'P3', 'overrides': body['overrides']}, '/api/jobs')
            self.assertEqual(status, 201, submitted_payload)
            submitted = submitted_payload['jobs'][0]
        self.assertEqual(submitted['argv'], payload['plan']['argv'])
        self.assertEqual(submitted['intent']['goal'], 'strong')
        self.assertFalse(submitted['overrides']['intent'])
        S.save({'compatibility': 'broad', 'show_advanced': True})
        restored = self.manager().list_jobs()[0]
        self.assertEqual(restored['argv'], submitted['argv'])
        self.assertEqual(restored['intent'], submitted['intent'])
        self.assertEqual(self.manager().restore_state()['queued'], 1)

    def test_http_hdr_conflict_is_actionable_400(self):
        body = {'path': self.media(hdr=True).path, 'overrides': {'intent': {'goal': 'balanced', 'compatibility': 'broad'}}}
        with patch.object(server, '_is_allowed', return_value=True), patch.object(server, 'probe', return_value=self.media(hdr=True)):
            status, data = self.request(self.manager(), body, '/api/plan')
        self.assertEqual(status, 400, data); self.assertIn('HDR', data['error'])

    def test_simple_to_manual_does_not_change_estimate(self):
        info = self.media()
        auto = self.plan(info, goal='light')
        manual = P.make_plan(info, P.get_profile(auto.profile_id),
                             P.Overrides(**auto.resolved_overrides), caps=self.caps)
        self.assertEqual(auto.est_output_bytes, manual.est_output_bytes)
        self.assertEqual(auto.argv, manual.argv)

    def test_per_file_quality_only_preview_and_submit_agree(self):
        info = self.media()
        profile, ov = B.resolve_per_file(info, P.get_profile('P3'), self.ov(),
                                        {info.path: {'crf': 40}})
        self.assertFalse(ov.intent)
        self.assertEqual(P.make_plan(info, profile, ov, caps=self.caps).resolved_overrides['crf'], 40)

    def test_queued_profile_change_clears_resolved_hardware_preset(self):
        manager = self.manager(); info = self.media()
        with patch.object(J, 'probe', return_value=info), patch.object(manager, '_kick'):
            job = manager.submit([info.path], 'P3', self.ov(goal='strong').to_dict())[0]
            changed = manager.update_job(job['id'], {'max_long_edge': 0}, 'P2')
        self.assertTrue(changed['ok'], changed)
        self.assertEqual(M.last_option(changed['job']['argv'], ('-preset',)), 'medium')
        self.assertEqual(M.last_option(changed['job']['argv'], ('-crf',)), '23')
        self.assertFalse(changed['job']['intent'])


if __name__ == '__main__':
    suite = unittest.TestSuite(SimpleConsole(n) for n in SimpleConsole.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
