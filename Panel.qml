import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "omadesk"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  readonly property var barIdentity: hostWidget || root
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property real heightPercent: service && service.maxCm > service.minCm
    ? Math.max(0, Math.min(1, (service.heightCm - service.minCm) / (service.maxCm - service.minCm)))
    : 0

  function open() { controller.show() }
  function close() {
    if (root.service) root.service.stop()
    controller.hide()
  }
  function toggle() { opened ? close() : open() }

  function presetLabel(name) {
    return name.charAt(0).toUpperCase() + name.slice(1)
  }

  function hold(direction) {
    if (!root.service) return
    if (direction === "up") root.service.moveUp()
    else if (direction === "down") root.service.moveDown()
  }

  function release() {
    if (root.service) root.service.stop()
  }

  onOpenedChanged: {
    if (!opened) {
      root.release()
      return
    }
    if (root.service && !root.service.connected)
      root.service.scan()
  }

  component HoldArrow: Item {
    id: holdRoot
    property string iconText
    property string tooltipText
    property string direction
    property bool available: false

    implicitWidth: arrowButton.implicitWidth
    implicitHeight: arrowButton.implicitHeight

    Button {
      id: arrowButton
      anchors.fill: parent
      iconText: holdRoot.iconText
      foreground: root.foreground
      tooltipText: holdRoot.tooltipText
      enabled: holdRoot.available
    }

    MouseArea {
      anchors.fill: parent
      enabled: holdRoot.available
      hoverEnabled: true
      preventStealing: true
      onPressed: root.hold(holdRoot.direction)
      onReleased: root.release()
      onCanceled: root.release()
      onExited: if (pressed) root.release()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(320))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: content
        anchors.fill: parent
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: root.service ? root.service.deskName : "Omadesk"
          meta: root.service ? root.service.statusText() : "Service unavailable"
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            OmadeskIcon {
              implicitWidth: Style.font.display
              implicitHeight: Style.font.display
              iconSize: Style.font.display
              color: root.foreground
            }
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          text: root.service && root.service.connected
            ? Math.round(root.service.heightCm * 10) / 10 + " cm"
            : "—"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.displayLarge
          font.bold: true
        }

        Item {
          width: parent.width
          height: Style.space(10)
          Rectangle {
            anchors.fill: parent
            radius: height / 2
            color: Style.normalFillFor(root.foreground, Color.accent)
            opacity: 0.35
          }
          Rectangle {
            width: parent.width * root.heightPercent
            height: parent.height
            radius: height / 2
            color: Style.selectedFillFor(root.foreground, Color.accent)
          }
        }

        Column {
          anchors.horizontalCenter: parent.horizontalCenter
          spacing: Style.space(6)

          HoldArrow {
            iconText: "󰁝"
            tooltipText: "Hold to raise"
            direction: "up"
            available: root.service && root.service.connected
          }
          HoldArrow {
            iconText: "󰁅"
            tooltipText: "Hold to lower"
            direction: "down"
            available: root.service && root.service.connected
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        PanelSectionHeader {
          width: parent.width
          text: "DESK"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Text {
          width: parent.width
          visible: root.service && root.service.mac !== ""
          text: root.service
            ? (root.service.deskName || "Omadesk") + " · " + root.service.mac
            : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Button {
          width: parent.width
          text: root.service && root.service.scanning ? "Scanning…" : "Scan for desks"
          iconText: "󰂯"
          foreground: root.foreground
          enabled: root.service && !root.service.scanning
          onClicked: if (root.service) root.service.scan()
        }

        Repeater {
          model: root.service ? root.service.nearbyDesks : []

          delegate: Button {
            required property var modelData
            width: parent.width
            text: (modelData.name || "Desk") + " · " + (modelData.mac || "")
            iconText: root.service && root.service.mac === modelData.mac ? "󰄬" : "󰂱"
            foreground: root.foreground
            enabled: root.service && !root.service.scanning
            onClicked: if (root.service)
              root.service.selectDesk(modelData.mac, modelData.name)
          }
        }

        Text {
          width: parent.width
          visible: root.service && !root.service.connected
            && (!root.service.nearbyDesks || root.service.nearbyDesks.length === 0)
            && !root.service.scanning
          text: "Pair the desk in Bluetooth settings, then scan. If several desks are nearby, pick yours here."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        PanelSectionHeader {
          width: parent.width
          text: "PRESETS"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Repeater {
          model: root.service ? Object.keys(root.service.presets) : []

          delegate: Row {
            required property string modelData
            width: parent.width
            spacing: Style.space(6)

            Button {
              width: parent.width - saveBtn.width - parent.spacing
              text: root.presetLabel(modelData) + " · "
                + (root.service ? root.service.presets[modelData] : "?") + " cm"
              iconText: "󰓎"
              foreground: root.foreground
              enabled: root.service && root.service.connected
              onClicked: if (root.service) root.service.gotoPreset(modelData)
            }

            Button {
              id: saveBtn
              iconText: "󰆓"
              foreground: root.foreground
              tooltipText: "Save current height to " + modelData
              enabled: root.service && root.service.connected
              onClicked: if (root.service)
                root.service.savePreset(modelData, root.service.heightCm)
            }
          }
        }

        Text {
          width: parent.width
          visible: root.service && root.service.lastError !== ""
          text: root.service ? root.service.lastError : ""
          color: root.bar ? root.bar.urgent : Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
