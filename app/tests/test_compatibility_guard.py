"""Synthetic-only codec and track contract regressions."""
import importlib
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_delivery_guard import DeliveryGuard, CAPS, P, R, M, MediaInfo, StreamInfo
B = importlib.import_module('core.probe')
from core import caps as C
from core import verifier as V


class CompatibilityGuard(DeliveryGuard):
    def complex_media(self):
        info = self.media()
        info.streams += [StreamInfo(1,'audio',codec='aac',language='eng',title='Original',default=True),
                         StreamInfo(2,'audio',codec='flac',language='jpn',title='Commentary'),
                         StreamInfo(3,'subtitle',codec='ass',language='eng',title='Styled',default=True),
                         StreamInfo(4,'subtitle',codec='subrip',language='chi',title='Plain')]
        return info

    def test_audio_copy_is_per_track(self):
        argv = P._audio_args(self.complex_media(),'mp4',P.Overrides())
        self.assertEqual(M.last_option(argv,('-c:a:0',)),'copy')
        self.assertEqual(M.last_option(argv,('-c:a:1',)),'aac')

    def test_webm_rejects_h264_and_hevc_before_encoding(self):
        for profile in ('P2','P7'):
            caps = SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda n: True,has_filter=lambda n: True)
            with self.subTest(profile=profile), self.assertRaises(P.PlanError):
                P.make_plan(self.media(),P.get_profile(profile),P.Overrides(container='webm'),caps=caps)

    def test_mp4_family_subtitles_use_mov_text(self):
        for container in ('mp4','mov','m4v'):
            args = P._subtitle_args(self.complex_media(),container,P.Overrides())
            self.assertEqual(M.last_option(args,('-c:s:0',)),'mov_text')

    def test_bitmap_subtitles_are_not_sent_to_text_encoder(self):
        info = self.complex_media()
        info.streams[-1].codec='hdmv_pgs_subtitle'
        with self.assertRaises(P.PlanError):
            P.make_plan(info,P.get_profile('P2'),P.Overrides(container='mp4'),caps=CAPS)
        info.streams[-2].codec='hdmv_pgs_subtitle'
        with self.assertRaises(P.PlanError):
            P.make_plan(info,P.get_profile('P2'),P.Overrides(subtitle_mode='burn'),caps=CAPS)

    def test_real_video_is_mapped_instead_of_cover(self):
        info = self.media()
        info.streams[0].index=1
        info.streams.insert(0,StreamInfo(0,'video',codec='mjpeg',is_image=True))
        plan = self.plan(info)
        self.assertEqual(plan.argv[plan.argv.index('-map')+1],'0:1')

    def test_hevc_ten_bit_profile_and_native_input(self):
        caps = SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda n: True,has_filter=lambda n: True)
        plan = P.make_plan(self.media(),P.get_profile('P3'),P.Overrides(ten_bit=True),caps=caps)
        self.assertEqual(M.last_option(plan.argv,('-profile:v',)),'main10')
        self.assertEqual(M.last_option(plan.argv,('-pix_fmt',)),'p010le')
        self.assertIn('-forced-idr',plan.argv)

    def test_mkv_has_no_mp4_codec_tag(self):
        caps = SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda n: True,has_filter=lambda n: True)
        plan = P.make_plan(self.media(),P.get_profile('P3'),P.Overrides(container='mkv'),caps=caps)
        self.assertNotIn('-tag:v',plan.argv)

    def test_ass_conversion_announces_style_loss(self):
        plan = self.plan(self.complex_media())
        self.assertTrue(any('样式' in note for note in plan.warnings),plan.warnings)

    def test_profile_audio_default_is_used(self):
        caps = SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda n: True,has_filter=lambda n: True)
        plan = P.make_plan(self.complex_media(),P.get_profile('A2'),caps=caps)
        self.assertEqual(M.last_option(plan.argv,('-c:a:0',)),'aac')

    def test_registered_but_failed_hardware_is_unavailable(self):
        caps = C.Capabilities()
        caps.encoders=['av1_qsv']
        caps.hardware_probes={'av1_qsv':{'ok':False,'error':'unavailable'}}
        self.assertFalse(caps.has_encoder('av1_qsv'))

    def test_mov_forced_subtitle_is_refused(self):
        info=self.complex_media()
        info.subtitle_streams[-1].forced=True
        with self.assertRaisesRegex(P.PlanError,'forced'):
            self.plan(info,container='mov')

    def test_opus_surround_retains_six_channels_with_explicit_layout(self):
        info=self.complex_media()
        info.audio_streams[-1].channels=6
        info.audio_streams[-1].channel_layout='5.1(side)'
        args=P._audio_args(info,'webm',P.Overrides())
        self.assertEqual(M.last_option(args,('-channel_layout:a:1',)),'5.1')
        self.assertTrue(any('六声道' in s for s in P.track_warnings(info,'webm',P.Overrides())))

    def test_unsupported_hardware_ten_bit_is_refused(self):
        caps=SimpleNamespace(ffmpeg='ffmpeg',has_encoder=lambda n: True,has_filter=lambda n: True,
                             supports_pix_fmt=lambda n,p: p!='p010le')
        with self.assertRaises(P.PlanError):
            P.make_plan(self.media(),P.get_profile('P3'),P.Overrides(ten_bit=True),caps=caps)

    def contract(self):
        source=self.complex_media()
        source.audio_streams[0].channels=1
        source.audio_streams[1].channels=6
        source.subtitle_streams[-1].forced=True
        source.streams.append(StreamInfo(5,'attachment',codec='ttf',filename='fixture.ttf',mimetype='application/x-truetype-font'))
        plan=self.plan(source,container='mkv')
        expected=V.stream_expectations(source,plan.argv)
        actual=copy.deepcopy(source)
        actual.video.bit_depth=8
        return source,actual,expected

    def test_delivery_rejects_wrong_codec_or_depth(self):
        for field,value in (('codec','hevc'),('bit_depth',10)):
            source,actual,expected=self.contract()
            setattr(actual.video,field,value)
            result=V.VerifyResult()
            V._stream_check(result,actual,expected,True)
            self.assertEqual(result.verdict,'failed')

    def test_delivery_rejects_same_count_wrong_track_identity(self):
        for kind,field,value in (('audio','language','fra'),('audio','title','Wrong'),
                                 ('audio','default',False),('audio','channels',2),
                                 ('subtitle','forced',False),('subtitle','codec','webvtt'),
                                 ('attachment','filename','wrong.ttf'),('attachment','mimetype','text/plain')):
            source,actual,expected=self.contract()
            stream=([s for s in actual.streams if s.kind==kind][-1] if field=='forced'
                    else [s for s in actual.streams if s.kind==kind][0])
            setattr(stream,field,value)
            result=V.VerifyResult()
            V._stream_check(result,actual,expected,True)
            self.assertEqual(result.verdict,'failed',(kind,field,result.to_dict()))

    def test_delivery_rejects_lost_attachment(self):
        source,actual,expected=self.contract()
        actual.streams=[s for s in actual.streams if s.kind!='attachment']
        result=V.VerifyResult()
        V._stream_check(result,actual,expected,True)
        self.assertEqual(result.verdict,'failed')

    def test_stripped_metadata_still_checks_codec_and_flags(self):
        source,actual,expected=self.contract()
        actual.audio_streams[0].language=''
        actual.audio_streams[0].title=''
        result=V.VerifyResult()
        V._stream_check(result,actual,expected,False)
        self.assertEqual(result.verdict,'passed')
        actual.subtitle_streams[-1].forced=False
        V._stream_check(result,actual,expected,False)
        self.assertEqual(result.verdict,'failed')

    def test_chapter_title_or_timing_change_warns(self):
        source,actual,expected=self.contract()
        source.chapters=[{'start_time':'0','end_time':'2','tags':{'title':'First'}}]
        for chapter in ({'start_time':'0','end_time':'2','tags':{'title':'Wrong'}},
                        {'start_time':'0','end_time':'3','tags':{'title':'First'}}):
            actual.chapters=[chapter]
            result=V.VerifyResult()
            V._metadata_check(result,Path(source.path),source,actual,False,True)
            self.assertEqual(result.verdict,'risky')

    def test_repair_adapts_each_audio_track_independently(self):
        info=self.complex_media()
        diag=R.Diagnosis(info.path,ok=True,codec='h264',width=320,height=180,fps=24,
                         duration=4,container='matroska',verdict='seekable',keyframes=4,streams=info.streams)
        with patch.object(R,'get_caps',return_value=CAPS):
            plan=R.build_repair_plan(diag,'remux',{'container':'mp4','out_dir':self.tmp.name})
        self.assertEqual(M.last_option(plan['argv'],('-c:a:0',)),'copy')
        self.assertEqual(M.last_option(plan['argv'],('-c:a:1',)),'aac')


if __name__ == '__main__':
    suite = unittest.TestSuite(CompatibilityGuard(n) for n in CompatibilityGuard.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
