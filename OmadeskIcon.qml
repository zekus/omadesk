import QtQuick
import QtQuick.Effects

Item {
  id: root

  property color color: "#ffffff"
  property real iconSize: 16
  readonly property url source: Qt.resolvedUrl("assets/omadesk.svg")

  implicitWidth: iconSize
  implicitHeight: iconSize

  Image {
    id: glyph
    anchors.centerIn: parent
    width: root.iconSize
    height: root.iconSize
    source: root.source
    sourceSize.width: root.iconSize * 2
    sourceSize.height: root.iconSize * 2
    fillMode: Image.PreserveAspectFit
    visible: false
    layer.enabled: true
  }

  MultiEffect {
    anchors.fill: glyph
    source: glyph
    colorization: 1.0
    colorizationColor: root.color
  }
}
