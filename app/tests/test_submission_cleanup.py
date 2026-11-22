"""Isolated consent, handoff and source lifecycle regressions. No actual recycle/encode."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('VCS_STORE_DIR', tempfile.mkdtemp(prefix='vcs-cleanup-import-'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
from core import jobs as J, actions as A, settings as S
from core import history as H
from core.probe import MediaInfo
import server
REAL_RECYCLE = A.recycle


class SubmissionCleanup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vcs-cleanup-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module, key, value in [(J, 'STORE_DIR', self.root), (J, 'JOBS_DIR', self.root / 'jobs'),
                                   (J, 'QUEUE_FILE', self.root / 'queue.json'),
                                   (S, 'STORE_DIR', self.root), (S, 'SETTINGS_FILE', self.root / 'settings.json'),
                                   (S, '_CACHE', {}), (J.JobManager, '_auto_limits', lambda _: {'gpu': 1, 'cpu': 1})]:
            p = patch.object(module, key, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(A, 'recycle_bin_available', return_value=(True, ''))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(A, 'recycle', return_value={'ok': True})
        self.recycle = p.start(); self.addCleanup(p.stop)
        self.manager = J.JobManager()
        for module, key, value in [(H, 'JOBS_DIR', self.root / 'jobs'), (H, '_CACHE', {})]:
            p = patch.object(module, key, value); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.manager._seek_pool.shutdown, wait=True)

    def add(self, name, *, source=None, status='done', verdict='passed', batch='batch', consent=True):
        src = source or self.root / (name + '.source')
        if not src.exists(): src.write_bytes(b'synthetic source only')
        out = self.root / 'out' / (name + '.output')
        out.parent.mkdir(exist_ok=True); out.write_bytes(b'synthetic output only')
        job = J.Job(name, str(src), 'P1', out_path=str(out),
                    argv=['fixture', '-i', str(src), str(out)], status=status,
                    verify={'verdict': verdict}, finished=100 if status in J.TERMINAL else 0,
                    cleanup_batch=batch if consent else '', recycle_source=consent,
                    source_cleanup='pending' if consent else '',
                    source_identity=A.file_identity(str(src)), output_identity=A.file_identity(str(out)))
        self.manager._jobs[name] = job; self.manager._order.append(name)
        report = J.JOBS_DIR / name / 'report.json'
        report.parent.mkdir(parents=True); report.write_text(json.dumps({'source': {'sentinel': True}}), encoding='utf-8')
        return job

    def request(self, body, endpoint='/api/jobs/batch'):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'get_manager', return_value=self.manager), patch.object(server, '_is_allowed', return_value=True):
            worker.start()
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            try:
                connection.request('POST', endpoint, json.dumps(body), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close(); httpd.shutdown(); httpd.server_close(); worker.join(timeout=2)
                self.assertFalse(worker.is_alive())

    def test_default_and_global_policy_cannot_authorize(self):
        self.assertNotIn('recycle_source', J.DEFAULT_ON_SUCCESS)
        self.assertFalse(self.manager.set_on_success({'recycle_source': True})['ok'])
        self.add('default', consent=False)
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()

    def test_batch_rejects_missing_and_non_boolean_consent(self):
        for flag, confirm in [(True, False), (True, 'true'), ('true', True), (1, True)]:
            with self.subTest(flag=flag, confirm=confirm), self.assertRaises(ValueError):
                self.manager.submit_batch([], recycle_sources=flag, confirm_recycle=confirm)

    def test_batch_records_partial_errors_and_excludes_old_jobs(self):
        old = self.add('old', status='queued', consent=False)
        src = self.root / 'new.source'; src.write_bytes(b'only test bytes')
        entries = [{'path': str(src), 'profile': 'P1', 'overrides': {}},
                   {'path': 'broken-fixture', 'profile': 'P1', 'overrides': {}}]
        info = MediaInfo(path=str(src), ok=True, size_bytes=src.stat().st_size)
        out = str(self.root / 'queued.output')
        plan = SimpleNamespace(out_path=out, profile_id='P1', intent={}, decision=[], est_output_bytes=1,
                               resolved_overrides={}, command='fixture', est_memory_mb=100, argv=['fixture', '-i', str(src), out],
                               warnings=[], rules_applied=[], encoder='libx264')
        with patch.object(J, 'probe', side_effect=[info, RuntimeError('owned failure')]), \
                patch.object(J, 'get_profile', return_value={'id': 'P1'}), patch.object(J, 'make_plan', return_value=plan):
            result = self.manager.submit_batch(entries, recycle_sources=True, confirm_recycle=True)
        self.assertEqual(len(result['jobs']), 1); self.assertEqual(len(result['errors']), 1)
        accepted = self.manager._jobs[result['jobs'][0]['id']]
        self.assertTrue(accepted.recycle_source); self.assertEqual(accepted.cleanup_batch, result['batch_id'])
        self.assertTrue(accepted.source_identity); self.assertFalse(old.recycle_source)
        self.assertFalse(self.manager._submitting_batches); self.assertFalse(self.manager._submitting_sources)
        self.recycle.assert_not_called()

    def test_batch_waits_for_every_member(self):
        a = self.add('a'); b = self.add('b', status='queued')
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        b.status = 'done'; b.finished = 100
        self.manager._run_source_cleanups(); self.assertEqual(self.recycle.call_count, 2)
        self.assertEqual(a.source_cleanup, 'recycled')

    def test_unsealed_batch_cannot_clean_early(self):
        job = self.add('early'); self.manager._submitting_batches.add(job.cleanup_batch)
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        self.manager._submitting_batches.clear(); self.manager._run_source_cleanups()
        self.recycle.assert_called_once_with(job.src, recovery_path=job.source_recovery_path, expected_identity=job.source_identity)

    def test_same_source_other_batch_waits_and_runs_once(self):
        a = self.add('a'); b = self.add('b', source=Path(a.src), status='queued', batch='other', consent=False)
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        self.assertIn('等待', a.source_cleanup_note)
        b.status = 'done'; b.finished = 100
        self.manager._run_source_cleanups(); self.manager._run_source_cleanups()
        self.recycle.assert_called_once_with(a.src, recovery_path=a.source_recovery_path, expected_identity=a.source_identity)

    def test_same_source_two_consents_only_one_recycle(self):
        a = self.add('a'); b = self.add('b', source=Path(a.src))
        self.manager._run_source_cleanups()
        self.recycle.assert_called_once_with(a.src, recovery_path=a.source_recovery_path, expected_identity=a.source_identity)
        self.assertEqual([a.source_cleanup, b.source_cleanup], ['recycled', 'recycled'])

    def test_inflight_submission_and_worker_tail_block_cleanup(self):
        job = self.add('job')
        with self.manager._source_submission([job.src]):
            self.manager._run_source_cleanups(); self.recycle.assert_not_called()
            self.manager._working_ids.add(job.id)
        self.recycle.assert_not_called()
        self.manager._working_ids.clear(); self.manager._run_source_cleanups()
        self.recycle.assert_called_once()

    def test_other_batch_has_to_finish_all_members_before_same_path_cleanup(self):
        a = self.add('a', batch='a-batch')
        b = self.add('b', source=Path(a.src), batch='b-batch')
        c = self.add('c', status='queued', batch='b-batch')
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        c.status = 'done'; self.manager._run_source_cleanups()
        self.assertEqual(self.recycle.call_count, 2)
        self.assertEqual(b.source_cleanup, 'recycled')

    def test_failed_cancelled_risky_or_unknown_same_source_keeps_original(self):
        a = self.add('a')
        b = self.add('b', source=Path(a.src), consent=False)
        for status, verdict in [('failed', 'failed'), ('cancelled', 'passed'), ('interrupted', ''), ('done', 'risky'), ('done', '')]:
            with self.subTest(status=status, verdict=verdict):
                a.recycle_source = True; a.source_cleanup = 'pending'; b.status = status; b.verify = {'verdict': verdict}
                self.manager._run_source_cleanups(); self.assertEqual(a.source_cleanup, 'kept')
        self.recycle.assert_not_called(); self.assertTrue(Path(a.src).exists())

    def test_failed_other_source_does_not_prevent_successful_item_cleanup(self):
        a = self.add('a'); b = self.add('b', status='failed', verdict='failed')
        self.manager._run_source_cleanups(); self.recycle.assert_called_once_with(a.src, recovery_path=a.source_recovery_path, expected_identity=a.source_identity)
        self.assertEqual(b.source_cleanup, 'kept')

    def test_changed_source_kept(self):
        job = self.add('changed'); Path(job.src).write_bytes(b'different bytes')
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        self.assertIn('源文件身份', job.source_cleanup_note)

    def test_replaced_source_same_size_and_mtime_kept(self):
        job = self.add('replaced'); original = Path(job.src); snapshot = original.stat()
        original.rename(original.with_suffix('.before'))
        original.write_bytes(b'X' * snapshot.st_size)
        os.utime(original, ns=(snapshot.st_atime_ns, snapshot.st_mtime_ns))
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        self.assertEqual(job.source_cleanup, 'kept')

    def test_missing_or_changed_output_kept(self):
        job = self.add('output'); Path(job.out_path).rename(Path(job.out_path).with_suffix('.gone'))
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        self.assertIn('成品', job.source_cleanup_note)

    def test_output_modified_after_verification_kept(self):
        job = self.add('mutated'); Path(job.out_path).write_bytes(b'new content')
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()

    def test_identity_missing_kept(self):
        job = self.add('unknown'); job.source_identity = {}
        self.manager._run_source_cleanups(); self.recycle.assert_not_called()

    def test_persistence_failure_prevents_recycle(self):
        job = self.add('persist')
        with patch.object(self.manager, '_persist', return_value=False): self.manager._run_source_cleanups()
        self.recycle.assert_not_called(); self.assertEqual(job.source_cleanup, 'kept')

    def test_source_changes_after_durable_marker_kept(self):
        job = self.add('between'); real = self.manager._persist
        def persist():
            result = real()
            if job.source_cleanup == 'processing': Path(job.src).write_bytes(b'changed at boundary')
            return result
        with patch.object(self.manager, '_persist', side_effect=persist): self.manager._run_source_cleanups()
        self.recycle.assert_not_called(); self.assertEqual(job.source_cleanup, 'kept')

    def test_recycle_failure_keeps_source_and_is_reported(self):
        job = self.add('refused'); self.recycle.return_value = {'ok': False, 'error': 'owned refusal'}
        self.manager._run_source_cleanups(); self.assertEqual(job.source_cleanup, 'kept')
        self.assertIn('owned refusal', job.warnings[-1])
        report = json.loads((J.JOBS_DIR / job.id / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['source'], {'sentinel': True}); self.assertEqual(report['source_cleanup'], 'kept')

    def test_concurrent_finishers_do_not_repeat_source_action(self):
        job = self.add('concurrent')
        threads = [threading.Thread(target=self.manager._run_source_cleanups) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=2); self.assertFalse(t.is_alive())
        self.recycle.assert_called_once_with(job.src, recovery_path=job.source_recovery_path, expected_identity=job.source_identity)

    def test_restart_cancels_pending_and_processing_authorization(self):
        pending = self.add('pending'); processing = self.add('processing'); processing.source_cleanup = 'processing'
        self.manager._persist(); restored = J.JobManager()
        self.addCleanup(restored._seek_pool.shutdown, wait=True)
        restored._run_source_cleanups(); self.recycle.assert_not_called()
        for original in [pending, processing]:
            self.assertFalse(restored._jobs[original.id].recycle_source)
        self.assertEqual(restored._jobs[pending.id].source_cleanup, 'kept')
        self.assertEqual(restored._jobs[processing.id].source_cleanup, 'review')
        self.assertIn('待核查', restored._jobs[processing.id].source_cleanup_note)

    def test_clear_only_success_keeps_pending_risky_and_failures(self):
        success = self.add('success', consent=False)
        for name, status, verdict in [('failure', 'failed', 'failed'), ('cancel', 'cancelled', ''), ('interrupt', 'interrupted', ''), ('risk', 'done', 'risky')]:
            self.add(name, status=status, verdict=verdict, consent=False)
        pending = self.add('pending'); self.manager._working_ids.add(success.id)
        self.assertEqual(self.manager.clear_successful(), [])
        self.manager._working_ids.clear()
        removed = self.manager.clear_successful()
        self.assertEqual(removed, [success.id]); self.assertIn(pending.id, self.manager._jobs)
        self.assertTrue(Path(success.src).is_file()); self.assertTrue(Path(success.out_path).is_file())
        self.assertTrue((J.JOBS_DIR / success.id / 'report.json').is_file())

    def test_retry_clears_source_authorization(self):
        job = self.add('retry', status='failed', verdict='failed')
        self.assertTrue(self.manager.retry(job.id)); self.assertFalse(job.recycle_source)
        self.assertEqual(job.cleanup_batch, ''); self.assertEqual(job.source_cleanup, '')
        self.recycle.assert_not_called()

    def test_cancel_last_queued_item_settles_consent_without_recycle(self):
        job = self.add('cancelled', status='queued')
        self.assertTrue(self.manager.cancel(job.id)); self.assertEqual(job.source_cleanup, 'kept')
        self.recycle.assert_not_called()

    def test_http_requires_confirmation_and_matching_count_before_submit(self):
        source = str(self.root / 'source.bin')
        base = {'paths': [source], 'per_file': {source: {'profile': 'P1', 'overrides': {}}}, 'recycle_sources': True}
        for extra in [{}, {'confirm_recycle': 'true', 'expected_count': 1},
                      {'confirm_recycle': True, 'expected_count': 2}, {'confirm_recycle': True, 'expected_count': True}]:
            with self.subTest(extra=extra), patch.object(self.manager, 'submit_batch') as submit:
                status, result = self.request(dict(base, **extra)); self.assertEqual(status, 400, result)
                submit.assert_not_called()

    def test_http_confirmed_scope_and_clear_exact_ids(self):
        source = str(self.root / 'source.bin')
        body = {'paths': [source], 'per_file': {source: {'profile': 'P1', 'overrides': {}}},
                'recycle_sources': True, 'confirm_recycle': True, 'expected_count': 1}
        with patch.object(self.manager, 'submit_batch', return_value={'jobs': [], 'errors': [], 'batch_id': 'owned'}) as submit:
            status, result = self.request(body); self.assertEqual(status, 201, result)
            self.assertTrue(submit.call_args.kwargs['recycle_sources'])
            self.assertTrue(submit.call_args.kwargs['confirm_recycle'])
        good = self.add('good', consent=False); self.add('bad', status='failed', consent=False)
        status, result = self.request({}, '/api/jobs/clear')
        self.assertEqual(status, 200, result); self.assertEqual(result['removed_ids'], [good.id]); self.assertEqual(result['removed'], 1)

    def test_http_does_not_silently_ignore_misspelled_consent_or_string_flags(self):
        source = str(self.root / 'source.bin')
        base = {'paths': [source], 'per_file': {source: {'profile': 'P1', 'overrides': {}}}}
        for extra in [{'recycle_source': True}, {'skip_completed': 'false'}, {'recycle_sources': 'true'}]:
            with self.subTest(extra=extra), patch.object(self.manager, 'submit_batch') as submit:
                status, result = self.request(dict(base, **extra)); self.assertEqual(status, 400, result)
                submit.assert_not_called()

    def test_recycle_flags_require_recoverable_operation(self):
        self.assertTrue(A._RECYCLE_FLAGS & A._FOFX_RECYCLEONDELETE)
        self.assertTrue(A._RECYCLE_FLAGS & A._FOFX_EARLYFAILURE)

    def test_skip_requires_passed_and_unchanged_source_and_output(self):
        job = self.add('history', consent=False)
        report = J.JOBS_DIR / job.id / 'report.json'
        def save():
            report.write_text(json.dumps(job.to_dict()), encoding='utf-8'); H.invalidate()
        save(); self.assertEqual(H.latest_for_source(job.src)['id'], job.id)
        for status, verdict in [('failed', 'failed'), ('done', 'risky'), ('done', '')]:
            job.status = status; job.verify = {'verdict': verdict}; save()
            self.assertIsNone(H.latest_for_source(job.src))
        job.status = 'done'; job.verify = {'verdict': 'passed'}; save()
        Path(job.src).write_bytes(b'changed source'); self.assertIsNone(H.latest_for_source(job.src))
        job.source_identity = A.file_identity(job.src); save()
        Path(job.out_path).write_bytes(b'changed output'); self.assertIsNone(H.latest_for_source(job.src))
        job.output_identity = {}; save(); self.assertIsNone(H.latest_for_source(job.src))

    def test_one_batch_rejects_case_aliases_and_hardlinks(self):
        source = self.root / 'one.bin'; source.write_bytes(b'owned bytes')
        link = self.root / 'hardlink.bin'; os.link(source, link)
        for alias in [str(source).upper(), str(link)]:
            selected = [str(source), alias]
            body = {'paths': selected, 'per_file': {p: {'profile': 'P1', 'overrides': {}} for p in selected}}
            with patch.object(self.manager, 'submit_batch') as submit:
                status, result = self.request(body); self.assertEqual(status, 400, result)
                submit.assert_not_called()

    def test_crash_marker_keeps_recovery_location_and_blocks_automatic_clear(self):
        job = self.add('crash'); job.source_cleanup = 'processing'
        backup = Path(job.src).with_name('.vcs-recovery-' + 'a' * 32)
        os.link(job.src, backup); job.source_recovery_path = str(backup)
        self.manager._persist(); restored = J.JobManager()
        self.addCleanup(restored._seek_pool.shutdown, wait=True)
        recovered = restored._jobs[job.id]
        self.assertEqual(recovered.source_cleanup, 'review')
        self.assertIn(str(backup), recovered.source_cleanup_note)
        self.assertEqual(restored.clear_successful(), [])
        self.assertFalse(restored.remove(job.id)); self.assertFalse(restored.retry(job.id))
        self.assertTrue(backup.is_file()); self.recycle.assert_not_called()

    def test_public_recycle_rechecks_confirmed_identity_before_protection(self):
        source = self.root / 'expected.bin'; source.write_bytes(b'owned original')
        expected = A.file_identity(str(source)); source.write_bytes(b'owned modified with different length')
        self.assertNotEqual(A.file_identity(str(source)), expected)
        with patch.object(A, '_recycle_windows') as shell:
            result = REAL_RECYCLE(str(source), expected_identity=expected)
        self.assertFalse(result['ok']); shell.assert_not_called()
        self.assertEqual(source.read_bytes(), b'owned modified with different length')

    def test_worker_triggers_batch_cleanup_only_after_releasing_its_use(self):
        job = self.add('worker', status='running')
        self.manager._working_ids.add(job.id); self.manager._busy['cpu'] = 1
        def finish(current):
            current.status = 'done'; current.finished = 100
            self.manager._run_source_cleanups(); self.recycle.assert_not_called()
        with patch.object(self.manager, '_run_job', side_effect=finish):
            self.manager._job_worker(job, 'cpu')
        self.recycle.assert_called_once()
        self.assertEqual(self.manager._busy['cpu'], 0)
        self.assertNotIn(job.id, self.manager._working_ids)
        self.assertEqual(job.source_cleanup, 'recycled')

    def test_shell_reports_success_without_recoverable_destination_restores_source(self):
        source = self.root / 'restore-source.bin'; source.write_bytes(b'owned bytes')
        def destructive_shell(path):
            Path(path).unlink()
            return {'ok': True}
        with patch.object(A, '_recycle_windows', side_effect=destructive_shell):
            result = REAL_RECYCLE(str(source))
        self.assertFalse(result['ok']); self.assertEqual(source.read_bytes(), b'owned bytes')
        self.assertEqual(list(self.root.glob('.vcs-recovery-*')), [])

    def test_verified_destination_releases_only_own_temporary_link(self):
        source = self.root / 'source.bin'; source.write_bytes(b'owned bytes')
        destination = self.root / '$R_owned.bin'
        def recoverable_shell(path):
            Path(path).rename(destination)
            return {'ok': True, 'recycled_path': str(destination)}
        with patch.object(A, '_recycle_windows', side_effect=recoverable_shell), \
                patch.object(A, '_is_recycle_destination', return_value=True):
            result = REAL_RECYCLE(str(source))
        self.assertTrue(result['ok']); self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b'owned bytes')
        self.assertEqual(list(self.root.glob('.vcs-recovery-*')), [])

    def test_ordinary_destination_is_not_mistaken_for_recycle_bin(self):
        source = self.root / 'source.bin'; source.write_bytes(b'owned bytes')
        destination = self.root / 'ordinary-location.bin'
        def wrong_destination(path):
            Path(path).rename(destination)
            return {'ok': True, 'recycled_path': str(destination)}
        with patch.object(A, '_recycle_windows', side_effect=wrong_destination):
            result = REAL_RECYCLE(str(source))
        self.assertFalse(result['ok']); self.assertEqual(source.read_bytes(), b'owned bytes')

    def test_cannot_create_recovery_link_leaves_source_untouched(self):
        source = self.root / 'source.bin'; source.write_bytes(b'owned bytes')
        with patch.object(A.os, 'link', side_effect=PermissionError('owned refusal')), patch.object(A, '_recycle_windows') as shell:
            result = REAL_RECYCLE(str(source))
        self.assertFalse(result['ok']); shell.assert_not_called()
        self.assertEqual(source.read_bytes(), b'owned bytes')

    def test_unrelated_replacement_is_not_overwritten_during_recovery(self):
        source = self.root / 'source.bin'; source.write_bytes(b'owned original')
        def replaced_shell(path):
            Path(path).unlink(); Path(path).write_bytes(b'unrelated replacement')
            return {'ok': False, 'error': 'owned shell interruption'}
        with patch.object(A, '_recycle_windows', side_effect=replaced_shell):
            result = REAL_RECYCLE(str(source))
        self.assertFalse(result['ok']); self.assertEqual(source.read_bytes(), b'unrelated replacement')
        copies = list(self.root.glob('.vcs-recovery-*'))
        self.assertEqual(len(copies), 1); self.assertEqual(copies[0].read_bytes(), b'owned original')


if __name__ == '__main__':
    unittest.main(verbosity=2)
