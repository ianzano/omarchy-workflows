import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import backend
from history import RunStore


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, XDG_STATE_HOME=str(self.root / 'state'), XDG_RUNTIME_DIR=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = RunStore()

    def item(self, **changes):
        return dict(dict(id='test', name='Example <&>', steps=['true'], notifications=False), **changes)

    def run_item(self, item):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            backend.run_workflow(item)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_completion_persists_start_finish_and_exit_code(self):
        events = self.run_item(self.item())
        started = next(event for event in events if event['event'] == 'started')['lastRun']
        result = events[-1]['lastRun']
        self.assertEqual(started['status'], 'running')
        self.assertIsNone(started['completedAt'])
        self.assertEqual(started['runId'], result['runId'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['code'], 0)
        self.assertGreaterEqual(result['completedAt'], result['startedAt'])
        self.assertEqual(self.store.read('test'), result)
        self.assertEqual(backend.latest_run('test'), result)
        self.assertEqual(set(result), {'runId', 'startedAt', 'completedAt', 'status', 'code'})
        self.assertEqual((self.store.path / 'test.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o700)

    def test_failed_spawn_records_failure_and_sends_one_notification(self):
        with patch.object(backend, 'send_result') as notify:
            events = self.run_item(self.item(cwd=str(self.root / 'missing'), notifications=True))
        result = events[-1]
        self.assertFalse(result['ok'])
        self.assertEqual(self.store.read('test')['status'], 'failed')
        self.assertIsNotNone(result['lastRun']['completedAt'])
        notify.assert_called_once_with('Example <&>', 'failed')

    def test_notifications_are_once_per_result_and_never_on_refresh(self):
        with patch.object(backend, 'send_result') as notify:
            self.run_item(self.item(notifications=True))
            self.run_item(self.item(steps=['exit 8'], notifications=True))
            for _ in range(3):
                backend.latest_run('test')
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(notify.call_args.args, ('Example <&>', 'failed'))
            self.run_item(self.item())
            self.assertEqual(notify.call_count, 2)

    def test_notification_failure_keeps_real_result_and_redacts_diagnostics(self):
        with patch.object(backend, 'send_result', side_effect=RuntimeError('PRIVATE PAYLOAD')):
            result = self.run_item(self.item(notifications=True))[-1]
        self.assertTrue(result['ok'])
        self.assertEqual(self.store.read('test')['status'], 'completed')
        self.assertIn('notification could not be delivered', result['warning'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_interrupted_run_has_unknown_completion_time(self):
        record = dict(runId='a' * 32, startedAt=int(time.time() * 1000), completedAt=None, status='running', code=None)
        self.store.write('test', record)
        lock = backend.acquire_lock('test')
        try:
            self.assertEqual(backend.latest_run('test')['status'], 'running')
        finally:
            os.close(lock)
        recovered = backend.latest_run('test')
        self.assertEqual(recovered['status'], 'interrupted')
        self.assertIsNone(recovered['completedAt'])
        self.assertEqual(self.store.read('test'), recovered)

    def test_concurrent_runs_keep_independent_records(self):
        config = self.root / 'workflows.json'
        config.write_text(json.dumps(dict(version=1, workflows=[self.item(), self.item(id='second')])))
        processes = [subprocess.Popen([sys.executable, backend.__file__, '--config', str(config), 'run', key], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for key in ('test', 'second')]
        for process in processes:
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, err)
        self.assertEqual(self.store.read('test')['status'], 'completed')
        self.assertEqual(self.store.read('second')['status'], 'completed')
        self.assertNotEqual(self.store.read('test')['runId'], self.store.read('second')['runId'])
        result = subprocess.run([sys.executable, backend.__file__, '--config', str(config), 'list'], capture_output=True, text=True, check=True)
        self.assertTrue(all(item['lastRun']['status'] == 'completed' for item in json.loads(result.stdout)['workflows']))

    def test_shell_disconnect_still_saves_cancellation(self):
        config = self.root / 'workflows.json'
        config.write_text(json.dumps(dict(version=1, workflows=[self.item(steps=['sleep 60'])])))
        process = subprocess.Popen([sys.executable, backend.__file__, '--config', str(config), 'run', 'test', '--panel'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(json.loads(process.stdout.readline())['event'], 'started')
            process.stdout.close()
            process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(process.stderr.read(), b'')
            record = self.store.read('test')
            self.assertEqual(record['status'], 'cancelled')
            self.assertIsNotNone(record['completedAt'])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()

    def test_symlinks_and_invalid_records(self):
        self.store.path.mkdir(mode=0o700, parents=True)
        target = self.root / 'target'
        target.write_text('untouched')
        (self.store.path / 'test.json').symlink_to(target)
        with self.assertRaises(OSError):
            self.store.read('test')
        self.run_item(self.item())
        self.assertEqual(target.read_text(), 'untouched')
        self.assertFalse((self.store.path / 'test.json').is_symlink())
        (self.store.path / 'test.json').write_text('{"status":"<img src=x>"}')
        with self.assertRaises(ValueError):
            self.store.read('test')
        with self.assertRaises(ValueError):
            self.store.read('../outside')


if __name__ == '__main__':
    unittest.main()
