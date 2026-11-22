"""Settings persistence and live-policy atomicity, owned stores and bounded HTTP only."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_memory import LiveMemory, S, J, M, server


class SettingsAtomic(unittest.TestCase):
    setUp = LiveMemory.setUp
    manager = LiveMemory.manager
    connection = LiveMemory.connection
    call = LiveMemory.call

    def test_selection_persisted_and_restart_preserves(self):
        manager = self.manager()
        with self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'show_extreme':True})
        self.assertEqual(status, 200, data)
        S._CACHE.clear()
        self.assertTrue(S.load()['show_extreme'])
        self.assertFalse(S.load()['show_advanced'])
        self.assertEqual(self.manager().limits(), manager.limits())

    def test_write_failure_keeps_disk_cache_and_live_policy(self):
        S.save({'show_extreme':False}); manager = self.manager()
        disk = S.SETTINGS_FILE.read_bytes(); limits = manager.limits(); memory = manager.memory_policy()
        with patch.object(S.os, 'replace', side_effect=PermissionError('injected disk failure')), \
             patch.object(manager, '_kick') as kick, self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'show_extreme':True, 'limits':{'gpu':2}, 'memory_margin_mb':4000})
            self.assertEqual(status, 503, data); kick.assert_not_called()
        self.assertEqual(S.SETTINGS_FILE.read_bytes(), disk)
        self.assertFalse(S.load()['show_extreme'])
        self.assertEqual(manager.limits(), limits); self.assertEqual(manager.memory_policy(), memory)
        S._CACHE.clear(); self.assertFalse(S.load()['show_extreme'])

    def test_write_text_failure_keeps_successful_cache(self):
        S.save({'show_extreme':False})
        with patch.object(Path, 'write_text', side_effect=PermissionError('synthetic denied')):
            with self.assertRaises(OSError): S.save({'show_extreme':True})
        self.assertFalse(S.load()['show_extreme'])

    def test_invalid_location_does_not_apply_other_parameters(self):
        manager = self.manager(); before = manager.limits()
        with self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'limits':{'gpu':2}, 'show_extreme':True, 'location':'invalid'})
        self.assertEqual(status, 400, data)
        self.assertEqual(manager.limits(), before); self.assertFalse(S.load()['show_extreme'])

    def test_invalid_memory_does_not_apply_limits_or_create_directory(self):
        manager = self.manager(); before = manager.limits(); directory = Path(self.tmp.name)/'must-not-create'
        with patch.object(server, '_is_allowed_write_dir', return_value=True), self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'limits':{'gpu':2}, 'memory_margin_mb':1000, 'default_out_dir':str(directory)})
        self.assertEqual(status, 400, data); self.assertEqual(manager.limits(), before)
        self.assertFalse(directory.exists()); self.assertFalse(S.SETTINGS_FILE.exists())

    def test_invalid_limits_rejected_without_clamping_or_side_effects(self):
        manager = self.manager(); before = manager.limits()
        with self.connection(manager) as c:
            for bad in ({'gpu':0},{'gpu':99},{'cpu':True},{'gpu':1.5},{'gpu':'2'},{'unknown':1},[],{}):
                with self.subTest(limits=bad):
                    status, data = self.call(c, '/api/settings', {'limits':bad,'show_extreme':True})
                    self.assertEqual(status, 400, data)
                    self.assertEqual(manager.limits(), before); self.assertFalse(S.load()['show_extreme'])

    def test_partial_pool_patches_preserve_other_pool_and_restart(self):
        manager = self.manager()
        with self.connection(manager) as c:
            self.assertEqual(self.call(c, '/api/settings', {'limits':{'gpu':2}})[0], 200)
            self.assertEqual(self.call(c, '/api/settings', {'limits':{'cpu':3}})[0], 200)
            status, data = self.call(c, '/api/settings')
        self.assertEqual(status, 200); self.assertEqual(data['settings']['limits'], {'gpu':2,'cpu':3})
        S._CACHE.clear(); self.assertEqual(self.manager().limits(), {'gpu':2,'cpu':3})

    def test_reset_recommendation_is_durable(self):
        manager = self.manager()
        with patch.object(server.sizing, 'recommend', return_value={'gpu':2,'cpu':1,'basis':'fixture'}), self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'reset_limits':True})
        self.assertEqual(status, 200, data); self.assertEqual(manager.limits(), {'gpu':2,'cpu':1})
        S._CACHE.clear(); self.assertEqual(self.manager().limits(), {'gpu':2,'cpu':1})

    def test_invalid_boolean_and_unknown_fields_rejected(self):
        manager = self.manager()
        with self.connection(manager) as c:
            for body in ({'show_extreme':'true'},{'deep_verify':1},{'vmaf_check':[]},{'mystery':True},{'reset_limits':False}):
                self.assertEqual(self.call(c, '/api/settings', body)[0], 400)
        self.assertFalse(S.SETTINGS_FILE.exists())

    def test_directory_denied_keeps_limits_and_settings(self):
        manager = self.manager(); before = manager.limits()
        with patch.object(server, '_is_allowed_write_dir', return_value=False), self.connection(manager) as c:
            self.assertEqual(self.call(c, '/api/settings', {'limits':{'gpu':2}, 'default_out_dir':str(Path(self.tmp.name)/'denied')})[0], 403)
        self.assertEqual(manager.limits(), before); self.assertFalse(S.SETTINGS_FILE.exists())

    def test_directory_creation_failure_does_not_report_success(self):
        manager = self.manager(); before = manager.limits(); target = Path(self.tmp.name)/'new-output'
        original = Path.mkdir
        def denied(p,*args,**kwargs):
            if p == target: raise PermissionError('owned fixture denied')
            return original(p,*args,**kwargs)
        with patch.object(server, '_is_allowed_write_dir', return_value=True), patch.object(Path, 'mkdir', denied), self.connection(manager) as c:
            self.assertEqual(self.call(c, '/api/settings', {'limits':{'gpu':2}, 'default_out_dir':str(target)})[0], 503)
        self.assertEqual(manager.limits(), before); self.assertFalse(S.SETTINGS_FILE.exists())

    def test_success_changes_runtime_after_file_is_durable_and_leaves_queue(self):
        manager = self.manager(); sentinel = J.Job('owned-sentinel','owned-not-a-video','P3'); sentinel.argv=['immutable']
        manager._jobs[sentinel.id]=sentinel; manager._order.append(sentinel.id)
        before = sentinel.to_dict(); old = S.save
        def observe(patch):
            self.assertEqual(manager.memory_policy()['margin_mb'],2500)
            self.assertNotEqual(manager.limits()['gpu'],2)
            return old(patch)
        with patch.object(S, 'save', side_effect=observe), self.connection(manager) as c:
            status, data = self.call(c, '/api/settings', {'limits':{'gpu':2},'memory_margin_mb':4000,'deep_verify':True})
        self.assertEqual(status,200,data); self.assertEqual(manager.memory_policy()['margin_mb'],4000)
        self.assertEqual(before,sentinel.to_dict())


if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SettingsAtomic)).wasSuccessful())
