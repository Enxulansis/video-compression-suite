"""Isolated regressions for parameter, output and delivery boundaries."""
import os
import io
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resource_guard import GuardRegression, J, V, MediaInfo, StreamInfo
from core import planner as P, repair as R, paths, memory as M
import importlib
B = importlib.import_module('core.probe')

CAPS = SimpleNamespace(ffmpeg='ffmpeg', ffprobe='ffprobe', available=lambda: True,
                       has_encoder=lambda name: name == 'libx264', has_filter=lambda name: True)


class DeliveryGuard(GuardRegression):
    def media(self, name='source.mp4', duration=4.0):
        src = Path(self.tmp.name) / name
        src.write_bytes(b'source sentinel' * 100)
        return MediaInfo(str(src), ok=True, duration=duration, size_bytes=src.stat().st_size,
                         streams=[StreamInfo(0, 'video', codec='h264', width=320, height=180,
                                             fps=24, pix_fmt='yuv420p')])

    def plan(self, info, **kwargs):
        return P.make_plan(info, P.get_profile('P2'), P.Overrides(out_dir=self.tmp.name, **kwargs),
                           caps=CAPS, exists_probe=os.path.lexists)

    def test_aliases_cannot_bypass_rules(self):
        for flag in ('-c', '-codec', '-vcodec', '-c:v:0', '-g:v:0', '-force_key_frames:v:0',
                     '-filter:v:0', '-x265-params:v:0'):
            with self.subTest(flag=flag), self.assertRaises(P.PlanError):
                P.check_extra_args([flag, 'keyint=9999' if 'params' in flag else 'copy'])

    def test_extra_outputs_and_io_are_never_allowed(self):
        for args in (['shadow.mp4'], ['-f','mp4','shadow.mp4'], ['-i','other.mp4'], ['-y'],
                     ['-progress','shadow.txt'], ['-an','shadow.mp4']):
            with self.subTest(args=args), self.assertRaises(P.PlanError):
                P.check_extra_args(args, allow_unsafe=True)

    def test_parameter_file_channels_are_never_allowed(self):
        for args in (['-x265-params','csv=source.mp4'],
                     ['-vf','metadata=mode=print:file=source.mp4'], ['-report']):
            with self.subTest(args=args), self.assertRaises(P.PlanError):
                P.check_extra_args(args,allow_unsafe=True)

    def test_expert_encoder_alias_matches_estimate(self):
        info = self.media()
        plan = self.plan(info,allow_unsafe=True,extra_args=['-codec:v:0','copy'])
        self.assertEqual(plan.encoder,'copy')
        self.assertEqual(J.JobManager._argv_encoder(plan.argv),'copy')
        self.assertEqual(M.last_option(plan.argv,('-codec',)), 'copy')
        with self.assertRaises(P.PlanError):
            self.plan(info,allow_unsafe=True,max_long_edge=160,extra_args=['-codec','copy'])

    def test_hardware_fallback_uses_software_parameters(self):
        info = self.media()
        for ident,encoder,preset in (('P1','libx264','medium'),('P3','libx265','medium'),
                                      ('P4','libsvtav1','8')):
            caps = SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda name: name == encoder,
                                   has_filter=lambda name: True)
            plan = P.make_plan(info,P.get_profile(ident),caps=caps)
            with self.subTest(profile=ident):
                self.assertEqual(plan.encoder,encoder)
                self.assertEqual(M.last_option(plan.argv,('-preset',)),preset)
                self.assertNotIn('-rc',plan.argv)
                self.assertNotIn('-cq',plan.argv)

    def test_container_is_a_component(self):
        info = self.media()
        for container in ('../escape', '..\\escape', 'mp4:stream', 'bad'):
            with self.subTest(container=container), self.assertRaises(ValueError):
                self.plan(info, container=container)

    def test_queue_reserves_distinct_outputs(self):
        info = self.media()
        manager = self.manager()
        with patch.object(J, 'probe', return_value=info), patch('core.caps.get_caps', return_value=CAPS):
            jobs = manager.submit([info.path, info.path], 'P2', {'out_dir':self.tmp.name})
        self.assertEqual(len({j['out_path'] for j in jobs}), 2)

    def test_hardlinks_are_the_same_file(self):
        info = self.media()
        alias = Path(self.tmp.name) / 'alias.mp4'
        os.link(info.path, alias)
        self.assertTrue(paths.is_same_file(Path(info.path), alias))

    def test_directory_name_is_taken(self):
        root = Path(self.tmp.name)
        (root/'result.mp4').mkdir()
        self.assertEqual(paths.unique_path(root,'result.mp4').name,'result_2.mp4')

    def test_restored_multi_output_is_refused(self):
        manager = self.manager()
        info = self.media()
        out = str(Path(self.tmp.name)/'out.mp4')
        for argv in (['ffmpeg','-i',info.path,'shadow.mp4',out],
                     ['ffmpeg','-i',info.path,out,'-y'],
                     ['ffmpeg','-i','other.mp4',out],
                     ['ffmpeg','-i',info.path,'-f','tee',out]):
            job = J.Job('restore0001',info.path,'P2',out_path=out,argv=argv)
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                manager._validate_execution(job)

    def test_edit_avoids_another_queued_output(self):
        info = self.media()
        manager = self.manager()
        with patch.object(J,'probe',return_value=info),patch('core.caps.get_caps',return_value=CAPS):
            jobs = manager.submit([info.path,info.path],'P2',{'out_dir':self.tmp.name})
            edited = manager.update_job(jobs[1]['id'],{'name_template':Path(jobs[0]['out_path']).stem})
        self.assertTrue(edited['ok'],edited)
        self.assertNotEqual(edited['job']['out_path'],jobs[0]['out_path'])

    def test_atomic_rename_refuses_a_late_target(self):
        manager = self.manager()
        info = self.media()
        current = Path(self.tmp.name)/'provisional.mp4'
        current.write_bytes(b'encoded')
        job = self.add(manager,out_path=str(current),argv=['ffmpeg',str(current)],
                       overrides={'name_template':'result_{filesize_bytes}'})
        job.src = info.path
        real_move = paths.rename_without_replace
        calls = []
        def race(src,dst):
            if not calls:
                dst.write_bytes(b'late target sentinel')
            calls.append(dst)
            real_move(src,dst)
        with patch.object(J,'probe',return_value=info),patch.object(paths,'rename_without_replace',side_effect=race):
            manager._finalize_name(job)
        self.assertEqual(calls[0].read_bytes(),b'late target sentinel')
        self.assertEqual(Path(job.out_path).read_bytes(),b'encoded')
        self.assertEqual(len(calls),2)

    def test_final_name_and_retry_keep_argv_in_sync(self):
        manager = self.manager()
        info = self.media()
        out = Path(self.tmp.name) / 'provisional.mp4'
        out.write_bytes(b'encoded')
        job = self.add(manager, out_path=str(out), status='done',
                       argv=['ffmpeg','-y','-i',info.path,'-c:v','libx264',str(out)],
                       overrides={'name_template':'result_{filesize_bytes}'})
        job.src = info.path
        with patch.object(J, 'probe', return_value=info):
            manager._finalize_name(job)
        final = Path(job.out_path)
        self.assertEqual(job.argv[-1], str(final))
        self.assertTrue(manager.retry(job.id))
        self.assertNotEqual(job.out_path, str(final))
        self.assertEqual(job.argv[-1], job.out_path)
        self.assertEqual(final.read_bytes(), b'encoded')

    def check_output(self, info, source, keys, **kwargs):
        out = Path(self.tmp.name) / 'out.mp4'
        out.write_bytes(b'output')
        scan = SimpleNamespace(frames=96, keys=keys, errors=[], notes=[])
        with patch.object(V, 'probe', return_value=info), patch.object(B, 'scan_stream', return_value=scan), \
                patch.object(B, 'packet_keyframe_times', return_value=[0,1,2,3]):
            return V.verify(str(out), source, **kwargs)

    def test_remux_still_checks_duration_and_tracks(self):
        source = self.media()
        info = self.media('other.mp4', duration=1)
        source.size_bytes = 7
        source.streams.append(StreamInfo(1,'audio',codec='aac'))
        result = self.check_output(info, source, [0], reencoded=False)
        self.assertEqual(result.verdict, 'failed')
        self.assertTrue(any(c.id == 'V3' and c.status == 'fail' for c in result.checks))

    def test_unknown_duration_does_not_pass(self):
        for duration in (0, float('nan'), float('inf')):
            with self.subTest(duration=duration):
                info = self.media(duration=duration)
                result = self.check_output(info, None, [0])
                self.assertNotEqual(result.verdict,'passed')

    def test_keyframe_limit_is_two_seconds(self):
        info = self.media(duration=4.08)
        result = self.check_output(info, None, [0, 2.04])
        self.assertEqual(result.verdict, 'failed')

    def test_nonfinite_keyframes_do_not_pass(self):
        info = self.media()
        for keys in ([0,float('nan')],[float('inf')]):
            with self.subTest(keys=keys):
                result = self.check_output(info,None,keys)
                self.assertEqual(result.verdict,'failed')

    def test_explicit_drop_and_unexpected_track_changes(self):
        source = self.media()
        source.streams += [StreamInfo(1,'audio'),StreamInfo(2,'subtitle')]
        info = self.media('small.mp4')
        result = self.check_output(info,source,[0,1,2,3],expected_audio=0,expected_subtitles=0)
        self.assertEqual(result.verdict,'passed')
        result = self.check_output(info,source,[0,1,2,3])
        self.assertEqual(result.verdict,'failed')
        info.streams.append(StreamInfo(1,'audio'))
        result = self.check_output(info,source,[0,1,2,3],expected_audio=0,expected_subtitles=0)
        self.assertEqual(result.verdict,'failed')

    def test_source_absent_is_only_partial_verification(self):
        result = self.check_output(self.media(),None,[0,1,2,3])
        self.assertEqual(result.verdict,'risky')

    def test_samples_keep_final_filters_and_rate_limits(self):
        info = self.media()
        plan = self.plan(info,max_long_edge=160,extra_args=['-maxrate','500k','-bufsize','1000k'])
        captured = []
        def samples(*args):
            captured.extend(args[4])
            return {'ok':True,'est_bytes':100}
        with patch.object(R,'get_caps',return_value=CAPS),patch.object(R,'_run_sample_encodes',side_effect=samples):
            R.measure_compress_cost(info,plan)
        self.assertIn('-vf',captured)
        self.assertEqual(captured[captured.index('-maxrate')+1],'500k')

    def test_keyframe_retry_empty_is_reported(self):
        with patch.object(V,'keyframe_times',return_value=([],[])):
            keys, error = V.keyframe_times_retry('unused', attempts=2, delay=0)
        self.assertEqual(keys,[])
        self.assertTrue(error)

    def test_probe_nonzero_partial_json_is_rejected(self):
        proc = SimpleNamespace(returncode=1, stdout='{"streams":[]}',stderr='truncated input')
        with patch.object(B,'get_caps',return_value=CAPS), patch.object(B.work,'run',return_value=proc):
            with self.assertRaises(RuntimeError):
                B._probe_json('unused')
            self.assertEqual(B.packet_keyframe_times('unused'), [])

    def test_repair_gop_has_one_value(self):
        diag = R.Diagnosis(path=str(Path(self.tmp.name)/'repair.mp4'), ok=True,
                           codec='h264', width=320,height=180,fps=0,duration=4)
        with patch.object(R,'get_caps',return_value=CAPS):
            plan = R.build_repair_plan(diag,'compat264',{'gop_seconds':9,'out_dir':self.tmp.name})
        self.assertEqual(plan['argv'][plan['argv'].index('-force_key_frames')+1],
                         'expr:gte(t,n_forced*2)')
        self.assertIn('2.0s', plan['rules_applied'][0])

    def test_coarse_repair_does_not_default_to_remux(self):
        diag = R.Diagnosis(path='unused',verdict='coarse',keyframes=2)
        strategies = [R.RepairStrategy('remux','remux',''),R.RepairStrategy('compat264','h264','')]
        self.assertEqual(R.pick_default_strategy(diag,strategies),'compat264')

    def test_diagnosis_uses_the_delivery_keyframe_limit(self):
        info = self.media(duration=4.08)
        with patch.object(R,'probe',return_value=info), patch.object(R,'keyframe_times',return_value=([0,2.04],2)):
            diag = R.diagnose(info.path)
        self.assertEqual(diag.verdict,'coarse')
        self.assertFalse(diag.seek_ok)

    def test_repair_preserves_explicit_zero_quality_and_preset(self):
        diag = R.Diagnosis(path='unused',ok=True,codec='h264')
        with patch.object(R,'get_caps',return_value=CAPS):
            plan = R.build_repair_plan(diag,'compat264',{'crf':0})
        self.assertEqual(M.last_option(plan['argv'],('-crf',)),'0')
        caps = SimpleNamespace(has_encoder=lambda name: name in ('libsvtav1','libx264'),ffmpeg='ffmpeg')
        with patch.object(R,'get_caps',return_value=caps):
            plan = R.build_repair_plan(diag,'av1',{'preset':0,'crf':0})
        self.assertEqual(M.last_option(plan['argv'],('-preset',)),'0')
        self.assertEqual(M.last_option(plan['argv'],('-crf',)),'0')

    def test_repair_rejects_unknown_strategy_and_container(self):
        diag = R.Diagnosis(path='unused',ok=True,codec='h264')
        with patch.object(R,'get_caps',return_value=CAPS):
            for strategy,options in (('unknown',{}),('compat264',{'container':'../escape'})):
                with self.subTest(strategy=strategy), self.assertRaises(ValueError):
                    R.build_repair_plan(diag,strategy,options)

    def test_remux_reports_no_new_gop_and_maps_subtitles(self):
        diag = R.Diagnosis(path='unused',ok=True,codec='h264',keyframes=4,verdict='seekable',
                           streams=[StreamInfo(0,'video',codec='h264'),StreamInfo(1,'subtitle',codec='subrip')])
        with patch.object(R,'get_caps',return_value=CAPS):
            plan = R.build_repair_plan(diag,'remux',{'container':'mkv','out_dir':self.tmp.name})
        self.assertNotIn('-g',plan['argv'])
        self.assertFalse(any(rule.startswith(('R1 ','R2 ')) for rule in plan['rules_applied']))
        self.assertIn('0:s?',plan['argv'])
        self.assertIn('0:t?',plan['argv'])


if __name__ == '__main__':
    suite = unittest.TestSuite(DeliveryGuard(name) for name in DeliveryGuard.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
