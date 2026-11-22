"""Pure/mock regressions: no actual media process or large memory allocation."""
import importlib
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_compatibility_guard import CompatibilityGuard, P, M, V, StreamInfo, CAPS
W=importlib.import_module('core.work')
B=importlib.import_module('core.probe')


class BoundaryGuard(CompatibilityGuard):
    def memory_plan(self,chain='',encoder='hevc_nvenc',source=(3840,2160),fmt='yuv420p',output='yuv420p'):
        return SimpleNamespace(encoder=encoder,out_size=source,source_size=source,source_pix_fmt=fmt,
            argv=['ffmpeg','-i','fixture.mkv','-c:v',encoder,'-pix_fmt',output]+(['-vf',chain] if chain else []))

    def test_semiplanar_high_depth_storage(self):
        for fmt,factor in (('p210le',4/3),('p212be',4/3),('p216le',4/3),('nv20le',4/3),
                           ('p410le',2),('p412be',2),('p416le',2)):
            with self.subTest(fmt=fmt):
                self.assertEqual(M.pixel_layout(fmt),(True,factor))
                self.assertEqual(M.decoder_reserve_mb(7680,4320,fmt),
                                 math.ceil(7680*4320*3*factor*32/1048576))

    def test_float_rgb_storage(self):
        for fmt,bpp in (('rgbf32le',12),('rgbaf32le',16),('rgb96le',12),('rgba128le',16)):
            with self.subTest(fmt=fmt):
                self.assertEqual(M.decoder_reserve_mb(3840,2160,fmt),
                                 math.ceil(3840*2160*bpp*32/1048576))

    def test_hdr_float_pool_precedes_downscale(self):
        chain='zscale=t=linear,format=gbrpf32le,tonemap=hable,format=yuv420p,scale=640:360'
        amount,basis=M.estimate_plan_mb(self.memory_plan(chain))
        self.assertGreaterEqual(amount,200+M.decoder_reserve_mb(3840,2160,'yuv420p')+
                                M.decoder_reserve_mb(3840,2160,'gbrpf32le'))
        self.assertIn('未实测',basis)
        self.assertIn('中间',basis)

    def test_hardware_downscale_keeps_output_pool(self):
        amount,_=M.estimate_plan_mb(self.memory_plan('scale=3840:2160',source=(7680,4320),output='p010le'))
        self.assertGreaterEqual(amount,200+M.decoder_reserve_mb(7680,4320,'yuv420p')+
                                M.decoder_reserve_mb(3840,2160,'p010le'))

    def test_same_size_denoise_has_filter_pool(self):
        base,_=M.estimate_plan_mb(self.memory_plan())
        filtered,_=M.estimate_plan_mb(self.memory_plan('hqdn3d=4:3:6:4.5'))
        self.assertGreater(filtered,base)

    def test_auxiliary_and_queue_share_filter_reserve(self):
        plan=self.memory_plan('format=gbrpf32le,tonemap=hable,format=yuv420p,scale=640:360')
        budget=W.Budget(self.manager(),float('inf'),
            {str(Path('fixture.mkv').resolve()):((3840,2160),'yuv420p')})
        amount,pool=W._need(plan.argv,budget)
        self.assertEqual(amount,M.estimate_plan_mb(plan)[0])
        self.assertEqual(pool,'gpu')

    def test_video_only_auxiliary_includes_float_pool(self):
        plan=self.memory_plan('format=gbrpf32le',encoder='rawvideo')
        budget=W.Budget(self.manager(),float('inf'),
            {str(Path('fixture.mkv').resolve()):((3840,2160),'yuv420p')})
        amount,pool=W._need(plan.argv,budget)
        self.assertGreaterEqual(amount,M.decoder_reserve_mb(3840,2160,'gbrpf32le'))

    def test_same_count_layout_change_fails(self):
        source,actual,expected=self.contract()
        source.audio_streams[1].channel_layout='5.1(side)'
        expected=V.stream_expectations(source,self.plan(source,container='mkv').argv)
        actual.audio_streams[1].channel_layout='5.1'
        result=V.VerifyResult()
        V._stream_check(result,actual,expected,True)
        self.assertEqual(result.verdict,'failed')

    def test_expected_layout_follows_explicit_opus_rematrix(self):
        source=self.complex_media()
        source.audio_streams[1].channels=6
        source.audio_streams[1].channel_layout='5.1(side)'
        argv=['ffmpeg','-map','0:a?']+P._audio_args(source,'webm',P.Overrides())+['-c:v','libx264']
        row=V.stream_expectations(source,argv)['audio'][1]
        self.assertEqual(row['channel_layout'],'5.1')
        self.assertEqual(row['channels'],6)

    def test_unknown_layout_imposes_no_invented_requirement(self):
        source,actual,expected=self.contract()
        actual.audio_streams[0].channel_layout='mono'
        result=V.VerifyResult()
        V._stream_check(result,actual,expected,True)
        self.assertEqual(result.verdict,'passed')

    def test_opus_rejects_unsupported_known_layout_before_encoding(self):
        for channels,layout in ((4,'quad(side)'),(6,'6.0'),(8,'7.1(wide)'),(16,'')):
            source=self.complex_media()
            source.audio_streams[1].channels=channels
            source.audio_streams[1].channel_layout=layout
            with self.subTest(layout=layout), self.assertRaisesRegex(P.PlanError,'MKV'):
                P._audio_args(source,'webm',P.Overrides())

    def test_known_layout_is_explicit_for_transcoded_audio(self):
        for channels,layout in ((1,'mono'),(2,'stereo'),(3,'3.0'),(4,'quad'),
                                (5,'5.0'),(6,'5.1'),(7,'6.1'),(8,'7.1')):
            source=self.complex_media()
            source.audio_streams[1].channels=channels
            source.audio_streams[1].channel_layout=layout
            for container in ('mp4','webm'):
                with self.subTest(layout=layout,container=container):
                    argv=P._audio_args(source,container,P.Overrides(audio_mode='transcode'))
                    self.assertEqual(M.last_option(argv,('-channel_layout:a:1',)),layout)
                    if container=='webm':
                        self.assertEqual(M.last_option(argv,('-mapping_family:a:1',)),
                                         '1' if channels>2 else '0')

    def test_side_surround_adaptation_is_disclosed_for_five_channels(self):
        source=self.complex_media()
        source.audio_streams[1].channels=5
        source.audio_streams[1].channel_layout='5.0(side)'
        argv=P._audio_args(source,'webm',P.Overrides())
        self.assertEqual(M.last_option(argv,('-channel_layout:a:1',)),'5.0')
        self.assertTrue(any('五声道' in n for n in P.track_warnings(source,'webm',P.Overrides())))

    def test_unusual_layout_can_be_copied_to_mkv(self):
        source=self.complex_media()
        source.audio_streams[1].channels=8
        source.audio_streams[1].channel_layout='7.1(wide)'
        argv=P._audio_args(source,'mkv',P.Overrides(audio_mode='copy'))
        self.assertEqual(M.last_option(argv,('-c:a:1',)),'copy')
        self.assertNotIn('-channel_layout:a:1',argv)

    def test_dropped_audio_has_no_layout_warning(self):
        source=self.complex_media()
        source.audio_streams[1].channels=6
        source.audio_streams[1].channel_layout='5.1(side)'
        self.assertFalse(any('Opus' in n for n in P.track_warnings(source,'webm',P.Overrides(audio_mode='drop'))))

    def test_aac_side_surround_adaptation_is_explicit_and_disclosed(self):
        source=self.complex_media()
        source.audio_streams[1].channels=6
        source.audio_streams[1].channel_layout='5.1(side)'
        ov=P.Overrides(audio_mode='transcode')
        argv=P._audio_args(source,'mp4',ov)
        self.assertEqual(M.last_option(argv,('-channel_layout:a:1',)),'5.1')
        self.assertTrue(any('AAC' in n and '六声道' in n for n in P.track_warnings(source,'mp4',ov)))
        self.assertEqual(V.stream_expectations(source,argv)['audio'][1]['channel_layout'],'5.1')

    def test_planned_opus_command_passes_execution_contract(self):
        source=self.complex_media()
        source.audio_streams[1].channels=5
        source.audio_streams[1].channel_layout='5.0'
        plan=self.plan(source,container='mkv',audio_mode='transcode')
        manager=self.manager()
        job=self.add(manager,out_path=plan.out_path,argv=plan.argv)
        job.src=source.path
        manager._validate_execution(job)

    def test_loudness_measurement_failure_is_risky(self):
        source=self.media()
        output=Path(self.tmp.name)/'out.mp4'
        output.write_bytes(b'output')
        with patch.object(V,'probe',return_value=source), \
             patch.object(B,'scan_stream',return_value=B.ScanResult(96,[0,1,2,3],[],[])), \
             patch.object(V.audio,'measure_output',side_effect=RuntimeError('meter unavailable')):
            result=V.verify(str(output),source,loudness=-14)
        self.assertEqual(result.verdict,'risky')
        self.assertEqual(next(c for c in result.checks if c.id=='L1').status,'warn')

    def test_loudness_failure_preserves_prior_failure(self):
        source=self.media()
        output=Path(self.tmp.name)/'out.mp4'
        output.write_bytes(b'output')
        with patch.object(V,'probe',return_value=source), \
             patch.object(B,'scan_stream',return_value=B.ScanResult(96,[0],[],[])), \
             patch.object(V.audio,'measure_output',side_effect=RuntimeError('meter unavailable')):
            result=V.verify(str(output),source,loudness=-14)
        self.assertEqual(result.verdict,'failed')

    def test_mkv_keeps_pgs_and_dvd_separately(self):
        source=self.complex_media()
        source.subtitle_streams[0].codec='hdmv_pgs_subtitle'
        source.subtitle_streams[1].codec='dvd_subtitle'
        args=P._subtitle_args(source,'mkv',P.Overrides())
        self.assertEqual(M.last_option(args,('-c:s:0',)),'copy')
        self.assertEqual(M.last_option(args,('-c:s:1',)),'copy')
        for container in ('mp4','webm'):
            with self.assertRaises(P.PlanError):
                P._subtitle_args(source,container,P.Overrides())

    def test_burn_discloses_other_subtitle_omission(self):
        source=self.complex_media()
        source.subtitle_streams[1].codec='hdmv_pgs_subtitle'
        plan=self.plan(source,container='mkv',subtitle_mode='burn')
        self.assertTrue(any('其余字幕不再内挂' in n for n in plan.warnings))
        self.assertNotIn('0:s?',plan.argv)


if __name__=='__main__':
    suite=unittest.TestSuite(BoundaryGuard(n) for n in BoundaryGuard.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
