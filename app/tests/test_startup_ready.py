"""Cold-start contracts: synthetic capabilities, isolated HTTP and fake GUI/server."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_live_memory import LiveMemory, server
from core import sizing, caps, memory, work
import desktop
import app as entry


class StartupReady(unittest.TestCase):
    setUp = LiveMemory.setUp
    manager = LiveMemory.manager
    connection = LiveMemory.connection
    call = LiveMemory.call

    def test_health_does_not_initialize_manager_or_probe(self):
        manager=self.manager()
        with self.connection(manager) as c, \
             patch.object(server,'get_manager',side_effect=AssertionError('must not restore queue')) as mgr, \
             patch.object(server,'get_caps',side_effect=AssertionError('must not probe')) as caps:
            status,data=self.call(c,'/api/health')
        self.assertEqual(status,200,data);self.assertEqual(data['service'],'video-compression-suite')
        self.assertGreater(data['pid'],0);mgr.assert_not_called();caps.assert_not_called()

    def test_metadata_endpoints_do_not_construct_queue_manager(self):
        fixture=Mock();fixture.to_dict.return_value={'available':True};fixture.has_encoder.return_value=False
        with self.connection(self.manager()) as c, patch.object(server,'get_manager',side_effect=AssertionError('queue unrelated')),patch.object(server,'get_caps',return_value=fixture):
            for path in ('/api/caps','/api/profiles'):
                self.assertEqual(self.call(c,path)[0],200)

    def test_recommendation_has_no_subprocess_and_fits_limits_without_gpu(self):
        fixture=Mock();fixture.has_encoder.return_value=False
        with patch.object(sizing,'get_caps',return_value=fixture),patch.object(sizing,'physical_cores',return_value=24), \
             patch('subprocess.run',side_effect=AssertionError('no driver telemetry process')):
            recommended=sizing.recommend()
        self.assertEqual(recommended['gpu'],1);self.assertFalse(recommended['has_nvenc']);self.assertFalse(recommended['has_qsv'])
        manager=self.manager();saved=manager.save_settings({'limits':{k:recommended[k] for k in ('gpu','cpu')}})
        self.assertEqual(saved['limits'],{'gpu':1,'cpu':2})

    def test_runnable_hardware_is_still_required_for_recommendation(self):
        fixture=Mock();fixture.has_encoder.side_effect=lambda name:name=='hevc_nvenc'
        with patch.object(sizing,'get_caps',return_value=fixture),patch.object(sizing,'physical_cores',return_value=24):
            self.assertEqual(sizing.recommend()['gpu'],4)

    def test_capabilities_probe_remains_single_and_refresh_replaces_snapshot(self):
        p=Path(entry.__file__).parent/'core/caps.py';spec=importlib.util.spec_from_file_location('owned_caps',p)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        count=[]
        def probe(c):
            count.append(c);time.sleep(.03);c.encoders=['hevc_nvenc'];c.hardware_probes={'hevc_nvenc':{'ok':False,'error':'fixture device unavailable'}};return c
        results=[]
        with patch.object(module,'_find_binary',return_value='fixture-binary'),patch.object(module.Capabilities,'probe',probe):
            threads=[threading.Thread(target=lambda:results.append(module.get_caps())) for _ in range(6)]
            for t in threads:t.start()
            for t in threads:t.join(timeout=2);self.assertFalse(t.is_alive())
            self.assertEqual(len(count),1);self.assertTrue(all(c is results[0] for c in results))
            self.assertFalse(results[0].has_encoder('hevc_nvenc'))
            newer=module.get_caps(refresh=True);self.assertIsNot(newer,results[0]);self.assertEqual(len(count),2)

    def test_default_cli_serves_without_hardware_or_queue_initialization(self):
        fake_server=Mock();fake_server.serve_forever.side_effect=KeyboardInterrupt
        with patch.object(sys,'argv',['app.py','--no-browser']),patch.object(entry,'get_manager',side_effect=AssertionError('defer initialization')) as mgr, \
             patch.object(entry,'create_server',return_value=fake_server):
            self.assertEqual(entry.main(),0)
        mgr.assert_not_called();fake_server.shutdown.assert_called_once();fake_server.server_close.assert_called_once()

    def test_explicit_concurrency_is_applied_when_requested(self):
        fake_server=Mock();fake_server.serve_forever.side_effect=KeyboardInterrupt;manager=Mock()
        with patch.object(sys,'argv',['app.py','--no-browser','--concurrency','3']),patch.object(entry,'get_manager',return_value=manager),patch.object(entry,'create_server',return_value=fake_server):
            self.assertEqual(entry.main(),0)
        manager.set_concurrency.assert_called_once_with(3)

    def test_unknown_and_busy_predecessors_are_never_replaceable(self):
        host=object.__new__(desktop.DesktopHost)
        with patch.object(host,'_is_stale',return_value=True):
            for value in (None,'invalid',-1):self.assertEqual(host._stale_action({'active_jobs':value}),'reuse')
            self.assertEqual(host._stale_action({'active_jobs':1}),'busy')
            self.assertEqual(host._stale_action({'active_jobs':0}),'replace')

    def test_background_probe_peak_is_reserved_before_queue_admission(self):
        manager = self.manager()
        mem = {'available':1, 'free_mb':3000, 'total_mb':32000,
               'commit_used_mb':50000, 'commit_limit_mb':84000}
        with patch.object(manager, '_job_memory_mb', return_value=100), \
             patch.object(memory, 'machine_memory', return_value=mem):
            with patch.object(caps, 'startup_probe_reserve_mb', return_value=512):
                self.assertFalse(manager.memory_gate(Mock())['ok'])
                self.assertEqual(manager.memory_snapshot()['reserved_mb'],512)
            with patch.object(caps, 'startup_probe_reserve_mb', return_value=0):
                self.assertTrue(manager.memory_gate(Mock())['ok'])
        self.assertEqual(manager.list_jobs(),[])

    def test_background_probe_peak_also_protects_commit_admission(self):
        manager = self.manager()
        mem = {'available':1, 'free_mb':6000, 'total_mb':32000,
               'commit_used_mb':81000, 'commit_limit_mb':84000}
        with patch.object(manager, '_job_memory_mb', return_value=100), \
             patch.object(memory, 'machine_memory', return_value=mem), \
             patch.object(caps, 'startup_probe_reserve_mb', return_value=512):
            self.assertFalse(manager.memory_gate(Mock())['ok'])
            self.assertEqual(manager.memory_snapshot()['commit_reserved_mb'],512)

    def test_startup_probe_shares_two_child_limit_and_releases_on_failure(self):
        with work.startup_probe_slot(.1) as remaining:
            self.assertGreater(remaining,0)
            self.assertLessEqual(remaining,.1)
            with work.startup_probe_slot(.1):
                with self.assertRaises(TimeoutError):
                    with work.startup_probe_slot(.01):
                        self.fail('third child must not enter')
        with self.assertRaises(RuntimeError):
            with work.startup_probe_slot(.1):
                raise RuntimeError('fixture child failure')
        with work.startup_probe_slot(.1), work.startup_probe_slot(.1):
            pass


if __name__=='__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StartupReady)).wasSuccessful())
