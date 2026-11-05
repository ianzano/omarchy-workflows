#!/usr/bin/env python3
"""Exercise real QML, real runner and synthetic local workflows headlessly."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

project = Path(__file__).resolve().parent
def main():
    with tempfile.TemporaryDirectory(prefix='workflows-ui-') as directory:
        root = Path(directory)
        for module in ('Ui', 'Commons'):
            (root / module).symlink_to(Path('/usr/share/omarchy/shell') / module, target_is_directory=True)
        shutil.copytree(project, root / 'plugin', ignore=shutil.ignore_patterns('__pycache__', '.git'))
        source = (project / 'Panel.qml').read_text().replace('    id: root\n', '''    id: root
        function testFind(name, node) {
            node = node || content
            if (node.objectName === name) return node
            for (var i = 0; i < node.children.length; i++) {
                var found = testFind(name, node.children[i])
                if (found) return found
            }
            return null
        }
    ''', 1)
        (root / 'plugin/Panel.qml').write_text(source)
        config = root / 'config/omarchy'
        config.mkdir(parents=True)
        (config / 'local-icon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="#8ab8f4"/></svg>')
        (config / 'workflows.json').write_text(json.dumps(dict(version=1, groups=[
            dict(id='tools', title='<b>Tools</b>', description='<img src="https://example.invalid/tracker">', icon='🛠'),
            dict(id='empty', title='Hidden empty group')
        ], workflows=[
            dict(notifications=False, id='good', group='tools', icon={'file': 'local-icon.svg'}, name='<img src="https://example.invalid/tracker">', steps=['printf \'<b>early</b>\\n\'; sleep 0.5; printf done']),
            dict(notifications=False, id='bad', group='tools', icon='<img src=x>', name='Failure', steps=['echo broken >&2; exit 9']),
            dict(notifications=False, id='slow', name='Stop me', steps=['echo started; sleep 60'])
        ])))
        (root / 'shell.qml').write_text('''
    import QtQuick
    import Quickshell
    ShellRoot {
        id: test
        property int stage: 0
        property string savedRunId: ""
        Loader { id: plugin; source: "plugin/Panel.qml" }
        Timer {
            interval: 50; repeat: true; running: true
            onTriggered: {
                if (test.stage === 5) {
                    plugin.active = true
                    test.stage = 6
                    return
                }
                var p = plugin.item
                if (!p || p.workflows.length !== 3) return
                if (p.warning) throw new Error(p.warning)
                if (test.stage === 0) {
                    if (p.sections.length !== 2 || p.sections[0].group !== null || p.sections[0].items[0].id !== "slow") throw new Error("standalone workflows")
                    if (p.sections[1].group.id !== "tools" || p.sections[1].items.length !== 2) throw new Error("group membership")
                    if (p.testFind("group-empty")) throw new Error("empty group displayed")
                    if (p.testFind("group-title-tools").text !== "<b>Tools</b>" || p.testFind("group-title-tools").textFormat !== Text.PlainText) throw new Error("unsafe group title")
                    if (p.testFind("group-description-tools").textFormat !== Text.PlainText || p.testFind("group-icon-tools").glyph !== "🛠") throw new Error("group metadata")
                    if (p.testFind("workflow-icon-bad-text").text !== "<img src=x>" || p.testFind("workflow-icon-bad-text").textFormat !== Text.PlainText) throw new Error("unsafe workflow icon")
                    if (p.testFind("workflow-icon-slow").glyph !== "") throw new Error("optional workflow icon")
                    if (p.testFind("workflow-icon-good-image").status !== Image.Ready) return
                    var consoleItem = p.testFind("output-good")
                    if (consoleItem.textFormat !== TextEdit.PlainText) throw new Error("unsafe output format")
                    p.testFind("run-good").clicked()
                    p.testFind("run-bad").clicked()
                    if (p.runningCount !== 2) throw new Error("concurrent runs")
                    p.run("good")
                    if (p.runningCount !== 2) throw new Error("duplicate run")
                    test.stage = 1
                } else if (test.stage === 1) {
                    if (p.state("good").text.indexOf("<b>early</b>") < 0) return
                    if (!p.jobs.good) throw new Error("output did not stream before completion")
                    if (p.testFind("output-good").text.indexOf("<b>early</b>") < 0) throw new Error("console binding")
                    test.stage = 2
                } else if (test.stage === 2) {
                    if (p.runningCount) return
                    if (p.state("good").status !== "completed" || p.state("bad").status !== "failed") throw new Error("exit status")
                    if (!p.state("bad").expanded || p.state("bad").text.indexOf("broken") < 0) throw new Error("failure console")
                    p.run("slow")
                    test.stage = 3
                } else if (test.stage === 3) {
                    if (p.state("slow").text.indexOf("started") < 0) return
                    p.stop("slow")
                    test.stage = 4
                } else if (test.stage === 4) {
                    if (p.runningCount) return
                    if (p.state("slow").status !== "cancelled") throw new Error("cancel status")
                    if (p.preview("1\\n2\\n3\\n4\\n5\\n6\\n7\\n") !== "2\\n3\\n4\\n5\\n6\\n7") throw new Error("six line preview")
                    p.update("good", {expanded: true})
                    if (p.testFind("output-good").text !== p.state("good").text) throw new Error("expanded binding")
                    var record = p.runs.good
                    if (!record || !record.startedAt || !record.completedAt || record.status !== "completed") throw new Error("run summary")
                    if (p.testFind("started-good").text.indexOf(p.formatTime(record.startedAt)) < 0) throw new Error("start timestamp")
                    if (p.testFind("finished-good").text.indexOf(p.formatTime(record.completedAt)) < 0) throw new Error("completion timestamp")
                    test.savedRunId = record.runId
                    plugin.active = false
                    test.stage = 5
                } else if (test.stage === 6) {
                    if (!p.runs.good) return
                    if (p.runs.good.runId !== test.savedRunId || p.testFind("status-good").text !== "Completed") throw new Error("summary did not survive reload")
                    if (p.state("good").text !== "" || p.runningCount !== 0) throw new Error("reload reran workflow or persisted output")
                    console.log("WORKFLOWS_UI_PASSED")
                    Qt.quit()
                }
            }
        }
        Timer { interval: 8000; running: true; onTriggered: { console.error("test timed out"); Qt.quit() } }
    }
    ''')
        display = os.environ.get('WAYLAND_DISPLAY', 'wayland-1')
        if not display.startswith('/'):
            display = str(Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / display)
        env = dict(os.environ, WAYLAND_DISPLAY=display, QT_QPA_PLATFORM='wayland', QT_QPA_PLATFORMTHEME='',
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_RUNTIME_DIR=str(root), XDG_STATE_HOME=str(root / 'state'))
        result = subprocess.run(['quickshell', '-p', str(root / 'shell.qml')], env=env,
                                capture_output=True, text=True, timeout=12)
        output = result.stdout + result.stderr
        if result.returncode or 'WORKFLOWS_UI_PASSED' not in output or 'WARN scene:' in output:
            raise SystemExit(output)
        print('Quickshell passed: optional groups/icons, literal group markup, concurrent runs, streaming, literal markup, failure expansion, stop, six-line preview, start/finish timestamps and summary recovery after reload.')


if __name__ == "__main__":
    main()
