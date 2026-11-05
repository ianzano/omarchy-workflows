#!/usr/bin/env python3
"""Render actual panel content with synthetic output in an isolated Qt window."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

project = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='wf-preview-') as folder:
    root = Path(folder)
    for module in ('Ui', 'Commons'):
        (root / module).symlink_to(Path('/usr/share/omarchy/shell') / module, target_is_directory=True)
    (root / 'plugin').mkdir()
    (root / 'home').mkdir()
    (root / 'runtime').mkdir(mode=0o700)
    shutil.copytree(project / 'i18n', root / 'plugin/i18n')
    source = (project / 'Panel.qml').read_text()
    source = source.replace('    id: root\n', '''    id: root
    readonly property real previewHeight: content.implicitHeight
    component PreviewPanel: Item {
        property var anchorItem
        property var owner
        property var bar
        property bool open
        property Item focusTarget
        property real padding
        property real contentWidth
        property real contentHeight
        width: contentWidth; height: contentHeight
        function fittedContentWidth(value) { return value }
        function fittedContentHeight(value, maximum) { return value }
    }
''', 1)
    source = source.replace('    KeyboardPanel {', '    PreviewPanel {', 1)
    source = source.replace('function refresh() { if (!inventory.running) inventory.running = true }', 'function refresh() {}')
    source = source.replace('        id: button\n', '        id: button\n        visible: false\n', 1)
    (root / 'plugin/Panel.qml').write_text(source)
    (root / 'shell.qml').write_text('''
import QtQuick
import QtQuick.Window
import Quickshell
import qs.Commons
import qs.Commons as Commons
ShellRoot {
    Component.onCompleted: {
        Commons.Color.background = "#141b25"
        Commons.Color.foreground = "#e4eaf3"
        Commons.Color.accent = "#8ab8f4"
        Commons.Color.urgent = "#ed9292"
        Style.fontFamily = "JetBrainsMono Nerd Font"
        Style.fontBaseSize = 14
        Style.cornerRadius = 8
    }
    Window {
        id: window
        visible: true
        width: 600; height: plugin.item ? plugin.item.previewHeight + 48 : 600
        color: "#141b25"
        Rectangle { anchors.fill: parent; color: Commons.Color.background; z: -1 }
        Loader {
            id: plugin
            x: 20; y: 20
            source: "plugin/Panel.qml"
            onLoaded: {
                item.groups = [{id:"shopware", title:"Shopware", description:"Storefront builds and preview deployments", icon:"󰒓"}]
                item.workflows = [
                    {id:"stage", group:"shopware", icon:"󰏗", name:"Build storefront", description:"Compile assets on the staging server", host:"shop-stage", cwd:"/var/www/shopware"},
                    {id:"test", icon:"󰙨", name:"Run test suite", description:"Check the current project before pushing", cwd:"~/Work/storefront"},
                    {id:"deploy", group:"shopware", icon:"󰐕", name:"Deploy preview", description:"Publish the preview environment", host:"shop-preview", cwd:"/var/www/shopware"}
                ]
                item.update("stage", {status:"completed", text:"[1/1] $ sudo -n -u deploy bin/build-storefront.sh\\nBuilding storefront assets…\\n✓ 248 modules transformed\\n✓ storefront.js    184.2 kB\\n✓ storefront.css    32.1 kB\\nBuild completed in 8.4s"})
                item.update("test", {status:"failed", expanded:true, text:"[1/1] $ npm test\\nTests: 42 passed, 1 failed\\nFAIL checkout › applies discount code\\nExpected 89.00, received 99.00\\nFailed (exit 1)"})
                item.update("deploy", {status:"running", started:Date.now() - 12000, shown:false})
                item.updateRun("stage", {runId:"a", startedAt:Date.UTC(2026,10,5,13,42,0), completedAt:Date.UTC(2026,10,5,13,42,8), status:"completed", code:0})
                item.updateRun("test", {runId:"b", startedAt:Date.UTC(2026,10,5,13,45,0), completedAt:Date.UTC(2026,10,5,13,45,14), status:"failed", code:1})
                item.updateRun("deploy", {runId:"c", startedAt:Date.now() - 12000, completedAt:null, status:"running", code:null})
                item.jobs = {deploy: {write:function() {}}}
            }
        }
        Timer {
            interval: 800; running: true
            onTriggered: window.contentItem.grabToImage(function(result) {
                if (!result.saveToFile(Quickshell.env("WORKFLOWS_PREVIEW"))) throw new Error("save failed")
                console.log("PREVIEW_SAVED"); Qt.quit()
            })
        }
    }
}
''')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QPA_PLATFORMTHEME='', QT_QUICK_BACKEND='software',
               HOME=str(root / 'home'), XDG_CONFIG_HOME=str(root / 'home/config'),
               XDG_RUNTIME_DIR=str(root / 'runtime'), WORKFLOWS_PREVIEW=str(project / 'preview.png'))
    result = subprocess.run(['quickshell', '-p', str(root / 'shell.qml')], env=env,
                            capture_output=True, text=True, timeout=8)
    output = result.stdout + result.stderr
    if result.returncode or 'PREVIEW_SAVED' not in output or 'WARN scene:' in output:
        raise SystemExit(output)
    print('Rendered preview.png using synthetic workflows.')
