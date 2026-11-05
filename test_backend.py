import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import backend


class WorkflowsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workflows.json'
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.root), XDG_STATE_HOME=str(self.root / 'state'))

    def save(self, steps, **fields):
        item = dict(id='test', name='Test <img src="https://invalid">', steps=steps, notifications=False, **fields)
        self.path.write_text(json.dumps(dict(version=1, workflows=[item])))
        return item

    def launch(self, panel=False):
        cmd = [sys.executable, str(Path(backend.__file__)), '--config', str(self.path), 'run', 'test']
        return subprocess.Popen(cmd + (['--panel'] if panel else []), env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def events(self, process):
        out, err = process.communicate(timeout=5)
        self.assertEqual(err, b'')
        return [json.loads(line) for line in out.splitlines()]

    def test_fail_fast_and_cwd_and_merged_stderr(self):
        self.save([['pwd'], 'echo error >&2; exit 7', 'echo SHOULD_NOT_RUN'], cwd=str(self.root))
        p = self.launch()
        events = self.events(p)
        output = ''.join(e.get('text', '') for e in events)
        self.assertIn(str(self.root), output)
        self.assertIn('error\n', output)
        self.assertNotIn('SHOULD_NOT_RUN', output)
        self.assertEqual(events[-1]['code'], 7)
        self.assertFalse(events[-1]['ok'])

    def test_argv_is_literal(self):
        marker = self.root / 'must-not-exist'
        literal = f'$(touch {marker}) ; <img src="https://invalid">'
        self.save([['printf', '%s\n', literal]])
        events = self.events(self.launch())
        self.assertTrue(events[-1]['ok'])
        self.assertIn(literal, ''.join(e.get('text', '') for e in events))
        self.assertFalse(marker.exists())

    def test_ssh_quoting_and_noninteractive_options(self):
        item = self.save([['sudo', '-n', '-u', 'deploy', 'bin/build-storefront.sh']], host='shop-stage', cwd="/var/www/a b'c")
        cmd = backend.command_for(item)
        self.assertEqual(cmd[-2], 'shop-stage')
        self.assertIn('BatchMode=yes', cmd)
        # Execute exactly the remote shell string locally against synthetic commands.
        item['cwd'] = str(self.root)
        item['steps'] = [['printf', '%s\n', "literal ' ; $(false)"]]
        result = subprocess.run(['sh', '-c', backend.command_for(item)[-1]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith("literal ' ; $(false)\n"))

    def test_timeout_and_descendant_cleanup(self):
        self.save(['sleep 60 & echo $! > child.pid; wait'], cwd=str(self.root), timeout=1)
        events = self.events(self.launch())
        self.assertEqual(events[-1]['message'], 'Timed out')
        self.assertFalse(events[-1]['ok'])
        pid = int((self.root / 'child.pid').read_text())
        path = Path(f'/proc/{pid}/stat')
        self.assertTrue(not path.exists() or path.read_text().split()[2] == 'Z')

    def test_streaming_cancel_and_duplicate_lock(self):
        self.save(['printf early; sleep 60'])
        p = self.launch(panel=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        # Read through a selector so a broken streamer fails rather than hanging the suite.
        data = b''
        deadline = time.monotonic() + 3
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)
            while b'early"' not in data and time.monotonic() < deadline:
                for key, _ in selector.select(0.1):
                    data += os.read(key.fd, 4096)
        self.assertIn(b'early"', data)
        self.assertIsNone(p.poll())
        duplicate = self.events(self.launch())
        self.assertIn('already running', duplicate[-1]['message'])
        p.stdin.write(b'stop\n')
        p.stdin.flush()
        events = self.events(p)
        self.assertEqual(events[-1]['message'], 'Cancelled')

    def test_disconnect_cancels(self):
        self.save(['sleep 60'])
        p = self.launch(panel=True)
        events = self.events(p)  # communicate closes stdin, simulating a lost shell.
        self.assertEqual(events[-1]['message'], 'Cancelled')

    def test_invalid_config_and_host_options(self):
        for fields in [dict(host='-oProxyCommand=bad'), dict(timeout=True), dict(timeout=0)]:
            self.save(['true'], **fields)
            with self.assertRaises(ValueError):
                backend.load_config(self.path)
        self.save(['true'])
        data = json.loads(self.path.read_text())
        data['workflows'] *= 2
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            backend.load_config(self.path)

    def test_lock_symlink_refused(self):
        base = self.root / f'omarchy-workflows-{os.getuid()}'
        base.symlink_to(self.root, target_is_directory=True)
        with patch.dict(os.environ, self.env):
            with self.assertRaises(OSError):
                backend.acquire_lock('test')


if __name__ == '__main__':
    unittest.main()
