import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import backend


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workflows.json'
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.root), XDG_STATE_HOME=str(self.root / 'state'))

    def config(self):
        return dict(version=1, groups=[dict(id='shop', title='Shopware', description='Stage tasks', icon='🛠')], workflows=[
            dict(id='build', name='Build', group='shop', icon='󰏗', steps=['printf grouped'], notifications=False),
            dict(id='check', name='Check', steps=['printf ungrouped'], notifications=False)])

    def save(self, data):
        self.path.write_text(json.dumps(data))
        return backend.read_config(self.path)

    def test_legacy_config_remains_compatible(self):
        data = self.config()
        del data['groups']
        del data['workflows'][0]['group']
        del data['workflows'][0]['icon']
        result = self.save(data)
        self.assertEqual(result['groups'], [])
        self.assertEqual(backend.load_config(self.path), data['workflows'])

    def test_group_metadata_and_optional_membership_round_trip(self):
        data = self.config()
        data['groups'].append(dict(id='unused', title='Empty group'))
        data['workflows'][1]['group'] = None
        self.assertEqual(self.save(data), data)
        result = subprocess.run([sys.executable, backend.__file__, '--config', str(self.path), 'list'], env=self.env, capture_output=True, text=True, check=True)
        result = json.loads(result.stdout)
        self.assertEqual(result['groups'], data['groups'])
        self.assertEqual(result['workflows'][0]['group'], 'shop')
        self.assertIsNone(result['workflows'][1]['group'])

    def test_grouped_and_ungrouped_execution(self):
        self.save(self.config())
        for key, expected in [('build', 'grouped'), ('check', 'ungrouped')]:
            result = subprocess.run([sys.executable, backend.__file__, '--config', str(self.path), 'run', key], env=self.env, capture_output=True, text=True, check=True)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertTrue(events[-1]['ok'])
            self.assertIn(expected, ''.join(event.get('text', '') for event in events))

    def test_invalid_groups_rejected(self):
        invalid = [None, {}, [None], [dict(title='Missing id')], [dict(id='x', title='X', extra=True)],
                   [dict(id='../x', title='X')], [dict(id='x', title='')],
                   [dict(id='x', title='X'), dict(id='x', title='Duplicate')],
                   [dict(id=str(i), title='Group') for i in range(101)]]
        for groups in invalid:
            data = self.config()
            data['groups'] = groups
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.save(data)

    def test_invalid_workflow_references_and_icons_rejected(self):
        for field, value in [('group', 'missing'), ('group', ''), ('group', []), ('icon', ''), ('icon', 'x' * 17), ('icon', 'a\nb'), ('icon', 12)]:
            data = self.config()
            data['workflows'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.save(data)
        for icon in ('a\nb', 'x' * 17, 12):
            data = self.config()
            data['groups'][0]['icon'] = icon
            with self.subTest(icon=icon), self.assertRaises(ValueError):
                self.save(data)

    def test_metadata_is_data_and_listing_never_executes_steps(self):
        data = self.config()
        data['groups'][0].update(title='<b>Shop</b>', description='<img src="https://invalid">', icon='<img src=x>')
        data['workflows'][0]['icon'] = '<img src=x>'
        marker = self.root / 'must-not-exist'
        data['workflows'][0]['steps'] = [['touch', str(marker)]]
        result = self.save(data)
        self.assertEqual(result['groups'][0]['icon'], '<img src=x>')
        subprocess.run([sys.executable, backend.__file__, '--config', str(self.path), 'list'], env=self.env, check=True, capture_output=True)
        self.assertFalse(marker.exists())


class ImageIconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'workflows.json'
        self.svg = self.root / 'icon with spaces.svg'
        self.svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><path d="M0 0h24v24H0z" fill="#fff"/></svg>')

    def test_local_icon_object_resolves_beside_config_and_encodes_url(self):
        item = dict(id='group', title='Example', icon={'file': self.svg.name})
        backend.validate_icon(item['icon'], 'icon')
        shown = backend.display_icon(item, self.config)
        self.assertEqual(shown['iconSource'], self.svg.as_uri())
        self.assertIn('%20', shown['iconSource'])
        self.assertNotIn('iconSource', item)

    def test_invalid_remote_and_nonimage_paths_rejected(self):
        for value in ({'file': 'https://example.invalid/icon.png'}, {'file': 'file:///tmp/icon.png'},
                      {'file': 'icon.txt'}, {'file': ''}, {'file': 'icon.png', 'url': 'x'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backend.validate_icon(value, 'icon')

    def test_svg_external_references_and_scripts_rejected(self):
        for body in ('<image href="https://example.invalid/icon.png"/>', '<use href="other.svg#id"/>',
                     '<script>bad()</script>', '<path fill="url(https://example.invalid/icon)"/>'):
            self.svg.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + body + '</svg>')
            with self.subTest(body=body), self.assertRaises(ValueError):
                backend.display_icon({'icon': {'file': str(self.svg)}}, self.config)

    def test_internal_svg_references_allowed(self):
        self.svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><path fill="url(#g)"/></svg>')
        self.assertEqual(backend.display_icon({'icon': {'file': str(self.svg)}}, self.config)['iconSource'], self.svg.as_uri())

    def test_fifo_and_oversized_icon_rejected_without_blocking(self):
        fifo = self.root / 'fifo.png'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            backend.display_icon({'icon': {'file': str(fifo)}}, self.config)
        self.svg.write_bytes(b'x' * 1048577)
        with self.assertRaises(ValueError):
            backend.display_icon({'icon': {'file': str(self.svg)}}, self.config)


if __name__ == '__main__':
    unittest.main()
