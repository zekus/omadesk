import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "omadesk"

  readonly property var desk: bar && bar.shell
    ? bar.shell.serviceFor(root.moduleName) : null
  readonly property bool deskConnected: desk !== null && desk.connected
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property bool showHeight: String(root.setting("showHeightInBar", true)) !== "false"
    && String(root.setting("showHeightInBar", true)) !== "Off"
  readonly property string heightLabel: desk && desk.connected
    ? String(Math.round(desk.heightCm)) : ""

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.anchorItem = button
    target.hostWidget = root
    target.service = root.desk
  }

  function syncSettings() {
    if (desk && typeof desk.settings !== "undefined") desk.settings = settings
    if (bar && bar.shell && typeof bar.shell.ensureService === "function")
      bar.shell.ensureService(root.moduleName)
  }

  onBarChanged: {
    injectPanel()
    syncSettings()
  }
  onDeskChanged: injectPanel()
  onSettingsChanged: syncSettings()
  Component.onCompleted: syncSettings()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    dimmed: !root.deskConnected
    horizontalMargin: 8.75
    tooltipText: desk ? desk.statusText() : "Omadesk"
    fixedWidth: root.vertical ? -1 : contentRow.implicitWidth + button.scaledHorizontalMargin * 2
    onPressed: root.toggle()

    Row {
      id: contentRow
      anchors.centerIn: parent
      spacing: Style.space(5)

      OmadeskIcon {
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        iconSize: Style.bar.iconFont
        color: button.foreground
      }

      Text {
        visible: root.showHeight && root.heightLabel !== "" && !root.vertical
        anchors.verticalCenter: parent.verticalCenter
        text: root.heightLabel
        color: button.foreground
        font.family: button.fontFamily
        font.pixelSize: Style.font.caption
        opacity: desk && desk.moving ? 0.75 : 1
      }
    }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: root.moduleName

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function up(): string {
      if (!root.desk) return "service not ready"
      return root.desk.moveUp() ? "moving up" : root.desk.lastError
    }
    function down(): string {
      if (!root.desk) return "service not ready"
      return root.desk.moveDown() ? "moving down" : root.desk.lastError
    }
    function stop(): string {
      if (!root.desk) return "service not ready"
      return root.desk.stop() ? "stopped" : root.desk.lastError
    }
    function goto(preset: string): string {
      if (!root.desk) return "service not ready"
      return root.desk.gotoPreset(preset) ? "moving to " + preset : root.desk.lastError
    }
    function status(): string {
      if (!root.desk) return "service not ready"
      return JSON.stringify({
        state: root.desk.state,
        connected: root.desk.connected,
        height_cm: root.desk.heightCm,
        moving: root.desk.moving,
        mac: root.desk.mac,
        name: root.desk.deskName,
        presets: root.desk.presets
      })
    }
    function scan(): string {
      if (!root.desk) return "service not ready"
      return root.desk.scan() ? "scanning" : root.desk.lastError
    }
    function select(mac: string): string {
      if (!root.desk) return "service not ready"
      return root.desk.selectDesk(mac, "") ? "selecting " + mac : root.desk.lastError
    }
  }
}
