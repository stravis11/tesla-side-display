import QtQuick
import Quickshell
import Quickshell.Wayland

ShellRoot {
  PanelWindow {
    screen: Quickshell.screens.find(s => s.name === Quickshell.env("SIDE_DISPLAY_TEST_MONITOR"))
    visible: true
    color: "#163d30"
    anchors { top: true; bottom: true; left: true; right: true }
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "side-display-test"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.OnDemand
    TextInput {
      id: input
      anchors.fill: parent
      font.pixelSize: 60
      color: "#ffffff"
      text: ""
      focus: true
      onTextChanged: console.log("TEST_TEXT=" + text)
      MouseArea {
        anchors.fill: parent
        onClicked: input.forceActiveFocus()
      }
    }
    Text {
      anchors.bottom: parent.bottom
      text: "WEBRTC DISPLAY TEST"
      color: "#a8dfba"
      font.pixelSize: 40
    }
  }
}
