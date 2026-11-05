import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
    id: root
    moduleName: "ianzano.workflows"
    ipcTarget: moduleName
    property var workflows: []
    property var outputs: Object.create(null)
    property var runs: Object.create(null)
    property var jobs: Object.create(null)
    property int runGeneration: 0
    property string warning: ""
    property double now: Date.now()
    readonly property string bridge: decodeURIComponent(Qt.resolvedUrl("backend.py").toString().replace(/^file:\/\//, ""))
    readonly property int runningCount: Object.keys(jobs).length
    readonly property bool anyRunning: Object.keys(runs).some(key => runs[key].status === "running")
    readonly property color success: "#8fbc8f"
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight
    Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

    function state(key) { return outputs[key] || {text: "", status: "idle", expanded: false, shown: true, started: 0} }
    function update(key, patch) {
        var next = Object.assign(Object.create(null), outputs)
        next[key] = Object.assign({}, state(key), patch)
        outputs = next
    }
    function updateRun(key, record) {
        if (!record) return
        var next = Object.assign(Object.create(null), runs)
        next[key] = record
        runs = next
    }
    function formatTime(value) {
        return new Date(value).toLocaleString(Qt.locale(Commons.I18n.formatLocale), "ddd d MMM yyyy, HH:mm:ss")
    }
    function clean(text) {
        return String(text).replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
            .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\r\n/g, "\n").replace(/\r/g, "\n")
            .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    }
    function preview(text) { return text.replace(/\s+$/, "").split("\n").slice(-6).join("\n") }
    function refresh() { if (!inventory.running) inventory.running = true }
    function run(key) {
        if (jobs[key]) return
        runGeneration++
        update(key, {text: "", status: "running", shown: true, expanded: false, started: Date.now(), notice: ""})
        var job = jobFactory.createObject(root, {workflowId: key})
        var next = Object.assign(Object.create(null), jobs)
        next[key] = job
        jobs = next
        job.running = true
    }
    function stop(key) { if (jobs[key]) jobs[key].write("stop\n") }
    Component.onCompleted: refresh()
    onOpenedChanged: if (opened) refresh()
    Component.onDestruction: {
        for (var key in jobs) jobs[key].write("stop\n")
    }
    Timer { interval: 1000; running: root.runningCount > 0 || root.anyRunning; repeat: true; onTriggered: root.now = Date.now() }
    Timer { interval: 5000; running: root.opened; repeat: true; onTriggered: root.refresh() }
    Process {
        id: inventory
        property int generation: 0
        onStarted: generation = root.runGeneration
        command: ["python3", root.bridge, "list"]
        stdout: StdioCollector {
            onStreamFinished: {
                try {
                    var data = JSON.parse(text)
                    root.warning = data.ok ? "" : data.message
                    if (data.ok) {
                        root.workflows = data.workflows
                        if (inventory.generation === root.runGeneration) {
                            for (var i = 0; i < data.workflows.length; i++) {
                                var item = data.workflows[i]
                                if (!root.jobs[item.id]) root.updateRun(item.id, item.lastRun)
                            }
                        }
                    }
                } catch (error) { root.warning = strings.t("protocolError") }
            }
        }
        stderr: StdioCollector { onStreamFinished: if (text.trim()) root.warning = text.trim().slice(-2000) }
    }
    Process { id: editor; command: ["python3", root.bridge, "edit"] }
    Component {
        id: jobFactory
        Process {
            id: job
            property string workflowId: ""
            property bool received: false
            command: ["python3", root.bridge, "run", workflowId, "--panel"]
            stdinEnabled: true
            stdout: SplitParser {
                onRead: function(line) {
                    try {
                        var data = JSON.parse(line)
                        if (data.event === "started") {
                            root.runGeneration++
                            root.updateRun(job.workflowId, data.lastRun)
                        } else if (data.event === "output") {
                            root.update(job.workflowId, {text: (root.state(job.workflowId).text + root.clean(data.text)).slice(-24000)})
                        } else if (data.event === "result") {
                            job.received = true
                            root.runGeneration++
                            root.updateRun(job.workflowId, data.lastRun)
                            var status = data.ok ? "completed" : data.message === "Cancelled" ? "cancelled" : data.message === "Timed out" ? "timeout" : "failed"
                            var previous = root.state(job.workflowId).text
                            root.update(job.workflowId, {status: status, shown: true, notice: data.warning || "",
                                expanded: !data.ok, text: data.ok ? previous : (previous + "\n" + root.clean(data.message)).slice(-24000)})
                        }
                    } catch (error) {
                        root.update(job.workflowId, {text: strings.t("protocolError"), status: "failed", shown: true})
                    }
                }
            }
            // Only Python diagnostics reach this pipe; workflow stderr is merged with stdout.
            stderr: SplitParser { onRead: function(line) {
                root.update(job.workflowId, {text: (root.state(job.workflowId).text + "\n" + root.clean(line)).slice(-24000)})
            } }
            onExited: function(code, exitStatus) {
                if (!received) root.update(workflowId, {status: "failed", shown: true,
                    text: (root.state(workflowId).text + "\n" + strings.t("processError")).slice(-24000)})
                var next = Object.assign(Object.create(null), root.jobs)
                delete next[workflowId]
                root.jobs = next
                job.destroy()
            }
        }
    }
    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "󱐋"
        tooltipText: strings.t("tooltip")
        active: root.runningCount > 0 || root.anyRunning
        activeColor: Commons.Color.accent
        onPressed: function(code) { if (code === Qt.MiddleButton) root.refresh(); else root.toggle() }
    }
    component Label: Text {
        textFormat: Text.PlainText
        color: Commons.Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        wrapMode: Text.Wrap
    }
    component Action: Button {
        focusable: true
        bordered: true
        fontSize: Style.font.caption
        implicitHeight: Style.space(32)
        verticalPadding: 0
        iconSize: Style.space(14)
        Layout.alignment: Qt.AlignVCenter
        opacity: enabled ? 1 : 0.4
    }
    component ColoredAction: Action {
        accent: foreground
        background: Util.alpha(foreground, 0.12)
        borderSpec: Border.flat(Util.alpha(foreground, hot || activeFocus ? 0.8 : 0.4), 1)
    }
    component IconAction: Action {
        implicitWidth: Style.space(32)
        horizontalPadding: 0
        iconSize: Style.space(18)
        Accessible.name: tooltipText
    }
    KeyboardPanel {
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: content
        padding: Style.space(12)
        contentWidth: fittedContentWidth(Style.space(560))
        contentHeight: fittedContentHeight(content.implicitHeight, Style.space(720))
        Flickable {
            anchors.fill: parent
            clip: true
            contentWidth: width
            contentHeight: content.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            Controls.ScrollBar.vertical: Controls.ScrollBar {}
            Column {
                id: content
                width: parent.width
                spacing: Style.space(10)
                focus: true
                Keys.onEscapePressed: root.close()
                RowLayout {
                    width: parent.width
                    Label { text: strings.t("title"); font.pixelSize: Style.font.caption; font.letterSpacing: 1.2; opacity: 0.6; Layout.fillWidth: true }
                    Action { text: strings.t("edit"); iconText: "󰏫"; onClicked: if (!editor.running) editor.running = true }
                    Action { text: strings.t("reload"); iconText: ""; enabled: !inventory.running; onClicked: root.refresh() }
                }
                Label { width: parent.width; visible: !!root.warning; text: root.warning; color: Commons.Color.urgent }
                Label { width: parent.width; visible: !root.workflows.length && !root.warning; text: strings.t("empty"); opacity: 0.6 }
                Repeater {
                    model: root.workflows
                    delegate: Rectangle {
                        id: card
                        required property var modelData
                        readonly property var output: root.state(modelData.id)
                        readonly property var lastRun: root.runs[modelData.id] || null
                        readonly property string runStatus: busy ? "running" : lastRun && lastRun.startedAt > output.started ? lastRun.status : output.status
                        readonly property bool running: runStatus === "running"
                        readonly property bool busy: !!root.jobs[modelData.id]
                        readonly property bool failed: ["failed", "timeout", "interrupted"].indexOf(runStatus) >= 0
                        readonly property color statusColor: failed ? Commons.Color.urgent : runStatus === "completed" ? root.success : Commons.Color.accent
                        width: content.width
                        implicitHeight: details.implicitHeight + Style.space(24)
                        radius: Style.space(8)
                        color: Util.alpha(card.statusColor, 0.045)
                        border.width: 1
                        border.color: Util.alpha(card.statusColor, 0.18)
                        Column {
                            id: details
                            anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(12) }
                            spacing: Style.space(8)
                            RowLayout {
                                width: parent.width
                                spacing: Style.space(8)
                                Label {
                                    text: "󰔟"; visible: card.running; color: card.statusColor
                                    RotationAnimation on rotation {
                                        from: 0; to: 360; loops: Animation.Infinite
                                        duration: Style.duration(900)
                                        running: card.running && !Style.reduceMotion
                                    }
                                }
                                Label { text: card.modelData.name; font.bold: true; Layout.fillWidth: true }
                                Label {
                                    objectName: "status-" + card.modelData.id
                                    text: card.running ? strings.t("running").arg(Commons.I18n.formatNumber(Math.max(0, Math.floor((root.now - (card.lastRun ? card.lastRun.startedAt : card.output.started)) / 1000)))) : strings.t(card.runStatus)
                                    color: card.statusColor
                                    font.pixelSize: Style.font.caption
                                }
                            }
                            GridLayout {
                                id: metadata
                                width: parent.width
                                columns: width >= Style.space(460) ? 2 : 1
                                columnSpacing: Style.space(12)
                                rowSpacing: Style.space(4)
                                Column {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.alignment: Qt.AlignTop
                                    Layout.preferredWidth: metadata.columns === 2 ? metadata.width * 0.46 : metadata.width
                                    spacing: Style.space(3)
                                    Label {
                                        width: parent.width
                                        visible: !!card.modelData.description
                                        text: card.modelData.description || ""
                                        opacity: 0.65
                                        font.pixelSize: Style.font.caption
                                    }
                                    Label {
                                        width: parent.width
                                        text: (card.modelData.host || "localhost") + (card.modelData.cwd ? " · " + card.modelData.cwd : "")
                                        opacity: 0.5
                                        font.pixelSize: Style.font.caption
                                    }
                                }
                                Column {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.alignment: Qt.AlignTop
                                    Layout.preferredWidth: metadata.columns === 2 ? metadata.width * 0.54 : metadata.width
                                    spacing: Style.space(3)
                                    Label {
                                        width: parent.width
                                        visible: !card.lastRun && !card.busy
                                        text: strings.t("noLastRun")
                                        horizontalAlignment: metadata.columns === 2 ? Text.AlignRight : Text.AlignLeft
                                        font.pixelSize: Style.font.caption
                                        opacity: 0.45
                                    }
                                    Label {
                                        objectName: "started-" + card.modelData.id
                                        width: parent.width
                                        visible: !!card.lastRun
                                        text: card.lastRun ? strings.t("startedAt").arg(root.formatTime(card.lastRun.startedAt)) : ""
                                        horizontalAlignment: metadata.columns === 2 ? Text.AlignRight : Text.AlignLeft
                                        font.pixelSize: Style.font.caption
                                        opacity: 0.6
                                    }
                                    Label {
                                        objectName: "finished-" + card.modelData.id
                                        width: parent.width
                                        visible: !!card.lastRun
                                        text: !card.lastRun ? "" : card.lastRun.completedAt
                                            ? strings.t("finishedAt").arg(root.formatTime(card.lastRun.completedAt))
                                            : strings.t(card.lastRun.status === "running" ? "finishPending" : "finishUnknown")
                                        horizontalAlignment: metadata.columns === 2 ? Text.AlignRight : Text.AlignLeft
                                        font.pixelSize: Style.font.caption
                                        opacity: 0.6
                                    }
                                }
                            }
                            Label {
                                width: parent.width
                                visible: !!card.output.notice
                                text: card.output.notice || ""
                                font.pixelSize: Style.font.caption
                                color: Commons.Color.urgent
                            }
                            RowLayout {
                                width: parent.width
                                spacing: Style.space(6)
                                ColoredAction { objectName: "run-" + card.modelData.id; text: strings.t("run"); iconText: ""; foreground: root.success; enabled: !card.running; onClicked: root.run(card.modelData.id) }
                                ColoredAction { objectName: "stop-" + card.modelData.id; text: strings.t("stop"); iconText: ""; foreground: Commons.Color.urgent; visible: card.busy; onClicked: root.stop(card.modelData.id) }
                                Item { Layout.fillWidth: true }
                                IconAction {
                                    objectName: "toggleConsole-" + card.modelData.id
                                    iconText: card.output.shown ? "󰅀" : "󰅃"
                                    tooltipText: strings.t(card.output.shown ? "hide" : "show")
                                    visible: card.output.status !== "idle"
                                    onClicked: root.update(card.modelData.id, {shown: !card.output.shown})
                                }
                            }
                            Column {
                                width: parent.width
                                spacing: Style.space(6)
                                visible: card.output.status !== "idle" && card.output.shown
                                Rectangle {
                                    width: parent.width
                                    implicitHeight: consoleText.implicitHeight + Style.space(16)
                                    radius: Style.space(6)
                                    color: Util.alpha(Commons.Color.background, 0.65)
                                    Controls.TextArea {
                                        id: consoleText
                                        objectName: "output-" + card.modelData.id
                                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(8) }
                                        padding: 0
                                        text: card.output.text ? (card.output.expanded ? card.output.text : root.preview(card.output.text)) : strings.t(card.busy ? "waiting" : "noOutput")
                                        readOnly: true
                                        selectByMouse: true
                                        textFormat: TextEdit.PlainText
                                        wrapMode: TextEdit.Wrap
                                        font.family: "monospace"
                                        font.pixelSize: Style.font.caption
                                        color: card.failed ? Commons.Color.urgent : Commons.Color.foreground
                                        background: null
                                    }
                                }
                                RowLayout {
                                    width: parent.width
                                    Label { text: strings.t(card.output.expanded ? "full" : "latest"); opacity: 0.45; font.pixelSize: Style.font.caption; Layout.fillWidth: true }
                                    Action { text: strings.t(card.output.expanded ? "less" : "more"); onClicked: root.update(card.modelData.id, {expanded: !card.output.expanded}) }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
