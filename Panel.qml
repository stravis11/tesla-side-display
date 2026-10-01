import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
  id: root
  moduleName: "st87.side-display"
  ipcTarget: moduleName
  manageIpc: false
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  property var state: ({ running: false, urls: [], displays: [] })
  property string errorText: ""
  property string actionText: ""
  property int actionGeneration: 0
  property int statusGeneration: 0
  readonly property bool busy: control.running
  readonly property string helper: decodeURIComponent(Qt.resolvedUrl("side-display").toString().replace(/^file:\/\//, ""))
  readonly property bool active: state.running === true
  readonly property string connectionCode: state.pairingCode || (state.urls && state.urls.length ? String(state.urls[0]).split("#")[1] || "" : "")

  function run(action, extra) {
    if (action === "status") { refreshStatus(); return }
    if (control.running) return
    actionGeneration += 1
    if (action !== "status") errorText = ""
    var command = ["bash", helper, action]
    if (action === "start") {
      command = command.concat(["--host", String(setting("host", "0.0.0.0")),
        "--port", String(setting("port", 8876)), "--fps", String(setting("fps", 30)),
        "--max-displays", String(setting("maxDisplays", 3))])
    }
    if (action === "hotspot") {
      command.push(extra)
      command = command.concat(["--port", String(setting("port", 8876))])
      extra = null
    }
    if (extra) command.push(extra)
    control.command = command
    control.running = true
  }

  function copyLink(link) {
    Quickshell.execDetached(["wl-copy", "--", link])
    actionText = "Connection link copied"
    notice.restart()
  }

  IpcHandler {
    target: root.ipcTarget
    function status(): string {
      return JSON.stringify({ running: root.active, busy: root.busy,
        position: root.state.position || "left",
        polling: statusProcess.running, error: root.errorText || root.state.error || "" })
    }
    function position(value: string): void { root.run("position", value) }
  }

  Process {
    id: control
    stdout: StdioCollector { id: output; waitForEnd: true }
    stderr: StdioCollector { id: errors; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0) {
        root.errorText = String(errors.text || output.text || "Command failed").trim()
        return
      }
      try {
        var parsed = JSON.parse(output.text)
        if (parsed.running !== undefined) root.state = parsed
        else Qt.callLater(root.refreshStatus)
      } catch (e) { root.errorText = "Could not read Tesla Side Display status" }
    }
  }

  function refreshStatus() {
    if (statusProcess.running || control.running) return
    statusGeneration = actionGeneration
    statusProcess.running = true
  }

  Process {
    id: statusProcess
    command: ["bash", root.helper, "status"]
    stdout: StdioCollector { id: statusOutput; waitForEnd: true }
    stderr: StdioCollector { id: statusErrors; waitForEnd: true }
    onExited: function(code) {
      if (root.statusGeneration !== root.actionGeneration) return
      if (code !== 0) {
        root.errorText = String(statusErrors.text || "Could not read Tesla Side Display status").trim()
        return
      }
      try { root.state = JSON.parse(statusOutput.text) }
      catch (e) { root.errorText = "Could not read Tesla Side Display status" }
    }
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refreshStatus()
  }
  Timer { id: notice; interval: 2500; onTriggered: root.actionText = "" }

  Ui.BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    iconComponent: Component {
      Canvas {
        property color ink: root.active ? Color.accent : Color.foreground
        onInkChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          ctx.scale(width / 24, height / 24)
          ctx.strokeStyle = ink
          ctx.fillStyle = ink
          ctx.lineWidth = 1.7
          ctx.lineJoin = "round"
          ctx.lineCap = "round"
          // Monitor frame and stand.
          ctx.strokeRect(2.5, 3, 19, 14)
          ctx.beginPath()
          ctx.moveTo(12, 17); ctx.lineTo(12, 21)
          ctx.moveTo(8, 21); ctx.lineTo(16, 21)
          ctx.stroke()
          // Curved Tesla-style T, sized to remain legible in the bar.
          ctx.beginPath()
          ctx.moveTo(7, 7)
          ctx.quadraticCurveTo(12, 4.5, 17, 7)
          ctx.lineTo(16.5, 8)
          ctx.quadraticCurveTo(12, 6, 7.5, 8)
          ctx.closePath(); ctx.fill()
          ctx.beginPath()
          ctx.moveTo(8, 8.6)
          ctx.quadraticCurveTo(12, 6.8, 16, 8.6)
          ctx.lineTo(14.8, 10)
          ctx.quadraticCurveTo(13.5, 9.3, 13, 9.3)
          ctx.lineTo(12, 15)
          ctx.lineTo(11, 9.3)
          ctx.quadraticCurveTo(10.5, 9.3, 9.2, 10)
          ctx.closePath(); ctx.fill()
        }
      }
    }
    active: root.active
    activeColor: Color.accent
    tooltipText: "Tesla Side Display · " + (root.active ? String((root.state.displays || []).length) + " displays" : "Stopped")
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) root.run("status")
      else root.toggle()
    }
  }

  Ui.KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: popup.fittedContentWidth(Style.space(420))
    contentHeight: popup.fittedContentHeight(column.implicitHeight, Style.space(650))
    focusTarget: content

    FocusScope {
      id: content
      anchors.fill: parent
      Keys.onEscapePressed: root.close()

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
          id: column
          width: parent.width
          spacing: Style.space(12)

          Text {
            text: "Tesla Side Display"
            font.family: Style.font.family
            font.pixelSize: Style.space(22)
            color: Color.foreground
          }
          Text {
            Layout.fillWidth: true
            text: root.active ? "WebRTC · " + String((root.state.displays || []).length) + " / " + String(root.state.maxDisplays || 3) + " displays" : "Extend your desktop into a browser."
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            color: Color.foreground
            wrapMode: Text.WordWrap
          }
          Ui.Button {
            Layout.fillWidth: true
            text: root.busy ? "Working…" : (root.active ? "Stop sharing" : "Start sharing")
            bordered: true
            focusable: true
            enabled: !root.busy
            onClicked: root.run(root.active ? "stop" : "start")
          }
          Ui.Button {
            Layout.fillWidth: true
            text: root.state.hotspot && root.state.hotspot.active ? "Stop Tesla hotspot" : "Start Tesla hotspot (IPv4)"
            bordered: true
            focusable: true
            enabled: !root.busy
            onClicked: root.run("hotspot", root.state.hotspot && root.state.hotspot.active ? "stop" : "start")
          }
          Text {
            text: "Screen location"
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            color: Color.foreground
          }
          RowLayout {
            Layout.fillWidth: true
            Ui.Button {
              Layout.fillWidth: true
              text: "← Left"
              selected: (root.state.position || "left") === "left"
              bordered: true
              focusable: true
              enabled: !root.busy
              onClicked: root.run("position", "left")
            }
            Ui.Button {
              Layout.fillWidth: true
              text: "Right →"
              selected: root.state.position === "right"
              bordered: true
              focusable: true
              enabled: !root.busy
              onClicked: root.run("position", "right")
            }
          }
          Text {
            Layout.fillWidth: true
            visible: !!root.state.hotspot && root.state.hotspot.active === true
            text: root.state.hotspot ? "Tesla Wi-Fi: " + root.state.hotspot.ssid + "\nPassword: " + root.state.hotspot.password + "\nOpen: http://" + root.state.hotspot.address + ":" + String(root.setting("port", 8876)) : ""
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            color: Color.accent
            wrapMode: Text.WordWrap
          }
          Text {
            Layout.fillWidth: true
            text: root.active ? "Open a connection link on the same Wi-Fi or hotspot. Browser displays appear on the " + (root.state.position || "left") + " of your desktop." : "Start sharing to get a private connection link."
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            color: Qt.darker(Color.foreground, 1.3)
            wrapMode: Text.WordWrap
          }
          Text {
            visible: root.active && root.connectionCode !== ""
            text: "Connection code"
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            color: Color.foreground
          }
          Ui.TextField {
            Layout.fillWidth: true
            visible: root.active && root.connectionCode !== ""
            readOnly: true
            text: root.connectionCode
            selectByMouse: true
          }
          Repeater {
            model: root.state.urls || []
            delegate: ColumnLayout {
              required property string modelData
              Layout.fillWidth: true
              spacing: Style.space(4)
              Text {
                Layout.fillWidth: true
                text: modelData.split("/#")[0]
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                color: Color.foreground
                elide: Text.ElideMiddle
              }
              RowLayout {
                Layout.fillWidth: true
                Ui.Button {
                  text: "Copy link"
                  bordered: true
                  focusable: true
                  onClicked: root.copyLink(modelData)
                }
                Ui.Button {
                  text: "Preview"
                  bordered: true
                  focusable: true
                  onClicked: Qt.openUrlExternally(modelData)
                }
              }
            }
          }
          Repeater {
            model: root.state.displays || []
            delegate: RowLayout {
              required property var modelData
              Layout.fillWidth: true
              Text {
                Layout.fillWidth: true
                text: modelData.name + " · " + modelData.state
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                color: Color.foreground
                elide: Text.ElideMiddle
              }
              Ui.Button {
                text: "Disconnect"
                focusable: true
                enabled: !root.busy
                onClicked: root.run("disconnect", modelData.id)
              }
            }
          }
          Text {
            Layout.fillWidth: true
            visible: root.actionText !== ""
            text: root.actionText
            color: Color.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
          Text {
            Layout.fillWidth: true
            visible: root.errorText !== "" || !!root.state.error
            text: root.errorText || root.state.error || ""
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            wrapMode: Text.WrapAnywhere
          }
          Text {
            Layout.fillWidth: true
            text: "Anyone with the link can control your desktop. Use a trusted hotspot: the initial connection page uses HTTP. WebRTC video and input are encrypted. Stop sharing to expire the link."
            color: Qt.darker(Color.foreground, 1.5)
            font.family: Style.font.family
            font.pixelSize: Style.space(11)
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
