"""Pure/mock regressions for explicit bitmap decoding; no media processes."""
import importlib
import io
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import verifier as V
B=importlib.import_module('core.probe')


class BitmapGuard(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out=Path(self.tmp.name)/'out.mkv'
        self.out.write_bytes(b'isolated sentinel')
        self.info=B.MediaInfo(str(self.out),ok=True,duration=2,size_bytes=100,
            streams=[B.StreamInfo(0,'video',codec='h264',width=160,height=90,fps=12),
                     B.StreamInfo(3,'subtitle',codec='hdmv_pgs_subtitle'),
                     B.StreamInfo(7,'subtitle',codec='dvd_subtitle')])
        self.caps=SimpleNamespace(ffprobe='ffprobe',available=lambda:True)
        self.commands=[]

    def scan(self,outputs,code=0):
        replies=iter(outputs)
        @contextmanager
        def stream(argv,**kwargs):
            self.commands.append((argv,kwargs))
            yield SimpleNamespace(stdout=io.StringIO(next(replies)),wait=lambda **kw:code)
        with patch.object(B,'get_caps',return_value=self.caps),patch.object(B.work,'stream',side_effect=stream):
            return B.scan_bitmap_subtitles(str(self.out),self.info)

    def verify(self,scan,**kwargs):
        with patch.object(V,'probe',return_value=self.info), \
             patch.object(B,'scan_stream',return_value=B.ScanResult(24,[0,1],[],[])), \
             patch.object(B,'packet_keyframe_times',return_value=[0,1]), \
             patch.object(B,'scan_bitmap_subtitles',return_value=scan) as invoked:
            result=V.verify(str(self.out),self.info,**kwargs)
        return result,invoked

    def test_selects_absolute_indices_and_counts_clear(self):
        scan=self.scan(['packet|pts=0\nsubtitle|pts=0|num_rects=1\npacket|pts=1000000\nsubtitle|num_rects=0\n',
                        'packet|pts=0\nsubtitle|num_rects=1\n'])
        self.assertEqual(scan.frames,3)
        self.assertFalse(scan.errors)
        self.assertFalse(scan.notes)
        self.assertEqual([argv[argv.index('-select_streams')+1] for argv,kw in self.commands],['3','7'])
        self.assertTrue(all('-show_frames' in argv and kw['timeout']==1800 for argv,kw in self.commands))

    def test_zero_exit_with_diagnostic_fails(self):
        scan=self.scan(['packet|pts=0\n[pgssub] Insufficient RLE data\n'])
        self.assertTrue(scan.errors)
        self.assertIn('RLE',scan.errors[0])
        self.assertEqual(len(self.commands),1)

    def test_discarded_dvd_packet_without_event_fails(self):
        scan=self.scan(['packet|pts=0\nsubtitle|num_rects=1\n','packet|pts=0\n'])
        self.assertTrue(any('没有可解码事件' in e for e in scan.errors))

    def test_nonzero_exit_fails_even_with_event(self):
        scan=self.scan(['packet|pts=0\nsubtitle|num_rects=1\n'],code=2)
        self.assertTrue(any('退出码 2' in e for e in scan.errors))

    def test_empty_tracks_are_unverified(self):
        scan=self.scan(['',''])
        self.assertFalse(scan.errors)
        self.assertEqual(len(scan.notes),2)
        result,_=self.verify(scan,deep=True)
        self.assertEqual(next(c for c in result.checks if c.id=='V1s').status,'warn')
        self.assertEqual(result.verdict,'risky')

    def test_late_diagnostic_is_not_hidden_by_event(self):
        scan=self.scan(['packet|pts=0\nsubtitle|num_rects=1\n[dvdsub] Discarding invalid packet\n'])
        self.assertEqual(scan.frames,1)
        self.assertTrue(scan.errors)

    def test_error_storage_is_bounded(self):
        scan=self.scan(['[pgssub] invalid\n'*10000])
        self.assertLessEqual(len(scan.errors),21)

    def test_no_bitmap_tracks_launches_nothing(self):
        self.info.streams[1].codec='ass'
        self.info.streams[2].codec='subrip'
        scan=self.scan([])
        self.assertEqual(scan.frames,0)
        self.assertEqual(self.commands,[])

    def test_unavailable_decoder_fails(self):
        with patch.object(B,'get_caps',return_value=SimpleNamespace(available=lambda:False)):
            scan=B.scan_bitmap_subtitles(str(self.out),self.info)
        self.assertTrue(scan.errors)

    def test_deep_delivery_adds_bitmap_check(self):
        result,invoked=self.verify(B.ScanResult(3,[],[],[]),deep=True)
        invoked.assert_called_once()
        self.assertEqual(next(c for c in result.checks if c.id=='V1s').status,'ok')

    def test_corrupt_bitmap_fails_delivery(self):
        result,_=self.verify(B.ScanResult(0,[],['DVD packet invalid'],[]),deep=True)
        self.assertEqual(result.verdict,'failed')
        self.assertEqual(next(c for c in result.checks if c.id=='V1s').status,'fail')

    def test_fast_delivery_does_not_claim_bitmap_decode(self):
        result,invoked=self.verify(B.ScanResult(3,[],[],[]),deep=False)
        invoked.assert_not_called()
        self.assertFalse(any(c.id=='V1s' for c in result.checks))

    def test_pending_peak_inventory_is_exact(self):
        import test_memory_peak as peak
        rows=[row for row in peak.cases('ffmpeg') if row[0] in peak.PENDING_CASES]
        self.assertEqual({r[0] for r in rows},{'x265_1080_10bit','svt_1080','hdr_float_4k_to_360'})
        self.assertTrue(all(r[2]>1000 for r in rows))

    def test_peak_failure_is_not_hidden_by_skip(self):
        import test_memory_peak as peak
        self.assertEqual(peak.exit_status([{'status':'failed'},{'status':'skipped'}]),1)
        self.assertEqual(peak.exit_status([{'status':'observed'},{'status':'skipped'}]),77)
        self.assertEqual(peak.exit_status([{'status':'observed'}]),0)

    def test_remaining_peak_selection_excludes_completed_cases(self):
        import test_memory_peak as peak
        selected=peak.selected_cases(['--cases','svt_1080','hdr_float_4k_to_360'])
        self.assertEqual(selected,{'svt_1080','hdr_float_4k_to_360'})
        self.assertNotIn('x265_1080_10bit',selected)
        self.assertIsNone(peak.selected_cases([]))
        self.assertEqual(peak.selected_cases(['--pending-only']),peak.PENDING_CASES)

    def test_invalid_peak_selection_cannot_be_silently_ignored(self):
        import test_memory_peak as peak
        for args in (['--cases','unknown'],['--cases'],['--pending-only','--cases','svt_1080']):
            with self.subTest(args=args),patch('sys.stderr',new=io.StringIO()),self.assertRaises(SystemExit) as error:
                peak.selected_cases(args)
            self.assertEqual(error.exception.code,2)

    def test_peak_second_gate_requires_known_commit(self):
        import test_memory_peak as peak
        directory=Path(self.tmp.name)/'peak'
        directory.mkdir()
        memory={'available':1,'free_mb':20000,'total_mb':32000,
                'commit_limit_mb':0,'commit_used_mb':0}
        proc=SimpleNamespace(poll=lambda:0,returncode=0,wait=lambda **kw:0)
        scope=SimpleNamespace(close=lambda:None)
        with patch.object(peak.M,'machine_memory',return_value=memory), \
             patch.object(peak.subprocess,'Popen',return_value=proc):
            result=peak.run_case(directory,['ffmpeg','-f','null','-'],3373,lambda child:scope)
        self.assertEqual(result['status'],'skipped')
        self.assertIn('提交',result['reason'])
        self.assertFalse((directory/'go').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
