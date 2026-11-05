"""Run real workflows against a notification service on an isolated D-Bus."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from gi.repository import Gio, GLib

XML = '''<node><interface name="org.freedesktop.Notifications"><method name="Notify">
<arg type="s" direction="in"/><arg type="u" direction="in"/>
<arg type="s" direction="in"/><arg type="s" direction="in"/>
<arg type="s" direction="in"/><arg type="as" direction="in"/>
<arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
<arg type="u" direction="out"/></method></interface></node>'''
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', ('org.freedesktop.Notifications', 0)), None, Gio.DBusCallFlags.NONE, 1000, None)
received = []

def notify(conn, sender, path, interface, method, params, invocation):
    received.append(params.unpack())
    invocation.return_value(GLib.Variant('(u)', (len(received),)))

connection.register_object('/org/freedesktop/Notifications', Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], notify, None, None)
project = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='wf-notify-') as directory:
    root = Path(directory)
    config = root / 'workflows.json'
    items = [dict(id=key, name='Build <img src="https://invalid"> & 雪', steps=steps, notifications=enabled)
             for key, steps, enabled in [('success', ['true'], True), ('failure', ['exit 4'], True), ('disabled', ['true'], False)]]
    config.write_text(json.dumps(dict(version=1, workflows=items)))
    env = dict(os.environ, XDG_RUNTIME_DIR=str(root), XDG_STATE_HOME=str(root / 'state'), XDG_CONFIG_HOME=str(root / 'config'))
    for key in ('success', 'failure', 'disabled'):
        helper = subprocess.Popen([sys.executable, str(project / 'backend.py'), '--config', str(config), 'run', key], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        loop = GLib.MainLoop()
        def check_done():
            if helper.poll() is None:
                return True
            loop.quit()
            return False
        GLib.timeout_add(20, check_done)
        timer = GLib.timeout_add_seconds(6, lambda: (helper.kill(), loop.quit(), False)[-1])
        loop.run()
        GLib.source_remove(timer)
        out, err = helper.communicate(timeout=2)
        assert not err, err
        events = [json.loads(line) for line in out.splitlines()]
        result = events[-1]
        assert result['ok'] == (key != 'failure'), result
        assert not result['warning'], result
    assert len(received) == 2, received
    assert received[0][3] == 'Workflow completed'
    assert received[0][4] == 'Build &lt;img src="https://invalid"&gt; &amp; 雪'
    assert received[0][6]['urgency'] == 1
    assert received[1][3] == 'Workflow failed' and received[1][6]['urgency'] == 2
    # Browsing/reloading saved summaries must not replay either notification.
    subprocess.run([sys.executable, str(project / 'backend.py'), '--config', str(config), 'list'], env=env, check=True, capture_output=True)
    assert len(received) == 2
print('NOTIFICATION_BUS_CHECKS_PASSED')
