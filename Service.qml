import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || home + "/.config"
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || home + "/.local/state"
  readonly property string configPath: configHome + "/omarchy/omadesk/config.json"
  readonly property string socketPath: stateHome + "/omarchy/omadesk.sock"
  readonly property string pidPath: stateHome + "/omarchy/omadesk.pid"
  readonly property string daemonPath: decodeURIComponent(
    String(Qt.resolvedUrl("omadesk-daemon.py")).replace(/^file:\/\//, ""))

  property var settings: ({})

  property string state: "starting"
  property string message: "Starting Omadesk…"
  property bool connected: false
  property bool moving: false
  property real heightCm: 0
  property real speedMps: 0
  property real minCm: 62
  property real maxCm: 127
  property string deskName: "Omadesk"
  property string mac: ""
  property var presets: ({ sit: 73, stand: 110 })
  property string lastError: ""
  property var nearbyDesks: []
  property bool scanning: false

  property int requestSerial: 0
  property bool requestPending: false
  property var pendingById: ({})

  readonly property int pollIntervalMs: intSetting("pollIntervalSec", 2, 1, 30) * 1000
  readonly property bool socketLive: socketLoader.active && socketLoader.item
    && socketLoader.item.connected

  function intSetting(name, fallback, minimum, maximum) {
    var value = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(value)) value = fallback
    return Math.max(minimum, Math.min(maximum, value))
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function statusText() {
    if (state === "error") return lastError || "Omadesk unavailable"
    if (state === "starting") return message || "Starting…"
    if (!connected) return message || "Connecting to desk…"
    if (moving || Math.abs(speedMps) > 0.0001) return "Moving · " + formatHeight(heightCm)
    return formatHeight(heightCm)
  }

  function formatHeight(cm) {
    return Math.round(cm * 10) / 10 + " cm"
  }

  function applyStatus(payload) {
    if (!payload) return
    if (payload.connected !== undefined) connected = payload.connected === true
    if (payload.moving !== undefined) moving = payload.moving === true
    if (payload.height_cm !== undefined) heightCm = Number(payload.height_cm)
    if (payload.speed_mps !== undefined) speedMps = Number(payload.speed_mps)
    if (payload.min_cm !== undefined) minCm = Number(payload.min_cm)
    if (payload.max_cm !== undefined) maxCm = Number(payload.max_cm)
    if (payload.mac !== undefined && payload.mac !== "") mac = String(payload.mac)
    if (payload.name !== undefined && payload.name !== "") deskName = String(payload.name)
    if (payload.presets !== undefined) presets = payload.presets
    if (payload.devices !== undefined) {
      nearbyDesks = payload.devices
      scanning = false
    }
    if (payload.event === "scan" || payload.event === "select")
      scanning = false

    if (payload.event === "error" && payload.error) {
      state = connected ? "ready" : "starting"
      lastError = String(payload.error)
      message = String(payload.error)
      scanning = false
      return
    }

    if (payload.ok === false && payload.error) {
      state = connected ? "ready" : "starting"
      lastError = String(payload.error)
      message = String(payload.error)
      scanning = false
      return
    }

    if (connected || heightCm > 0 || payload.event === "height") {
      state = "ready"
      lastError = ""
      message = connected ? "" : "Connecting to desk…"
    }
  }

  function applyConfig(text) {
    try {
      var config = JSON.parse(String(text || "{}"))
      if (config.name) deskName = String(config.name)
      if (config.mac) mac = String(config.mac)
      if (config.presets) presets = config.presets
    } catch (error) {
      // keep defaults
    }
  }

  function activeSocket() {
    return socketLoader.item || null
  }

  function sendRequest(cmd, extra) {
    var socket = activeSocket()
    if (!socket || !socket.connected) {
      message = "Waiting for Omadesk…"
      state = "starting"
      scheduleSocketConnect()
      return false
    }
    requestSerial++
    var id = requestSerial
    var payload = { id: id, cmd: cmd }
    if (extra) {
      for (var key in extra) payload[key] = extra[key]
    }
    pendingById[id] = cmd
    requestPending = true
    requestTimeout.restart()
    socket.write(JSON.stringify(payload) + "\n")
    socket.flush()
    return true
  }

  function handleLine(line) {
    var text = String(line || "").trim()
    if (!text) return
    var payload
    try {
      payload = JSON.parse(text)
    } catch (error) {
      return
    }

    if (payload.id !== undefined) {
      delete pendingById[payload.id]
      if (Object.keys(pendingById).length === 0) {
        requestPending = false
        requestTimeout.stop()
      }
    }
    applyStatus(payload)
  }

  function refresh() { sendRequest("status") }

  function moveUp() { return sendRequest("up") }
  function moveDown() { return sendRequest("down") }
  function stop() { return sendRequest("stop") }
  function moveTo(heightCmValue) {
    return sendRequest("move", { height_cm: Number(heightCmValue) })
  }
  function gotoPreset(name) {
    return sendRequest("goto_preset", { name: String(name) })
  }
  function savePreset(name, heightCmValue) {
    return sendRequest("save_preset", {
      name: String(name),
      height_cm: Number(heightCmValue)
    })
  }
  function reloadPresets() { return sendRequest("presets") }
  function scan() {
    scanning = true
    if (!sendRequest("scan")) scanning = false
    return scanning
  }
  function selectDesk(macValue, nameValue) {
    return sendRequest("select", {
      mac: String(macValue || ""),
      name: String(nameValue || "")
    })
  }

  function scheduleSocketConnect() {
    if (socketLoader.active) return
    socketWaitTimer.restart()
  }

  function openSocket() {
    message = "Connecting to Omadesk…"
    socketLoader.active = false
    socketLoader.active = true
  }

  function ensureDaemon() {
    state = "starting"
    message = "Starting Omadesk…"
    pidCheckProc.running = true
  }

  Process {
    id: pidCheckProc
    command: ["bash", "-c",
      "pidfile='" + root.pidPath + "'; sock='" + root.socketPath + "'; " +
      "if [ -f \"$pidfile\" ] && kill -0 \"$(cat \"$pidfile\")\" 2>/dev/null && [ -S \"$sock\" ]; then exit 0; else exit 1; fi"]
    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.openSocket()
        return
      }
      if (!daemonProc.running) {
        root.scheduleSocketConnect()
        daemonProc.running = true
      }
    }
  }

  Process {
    id: daemonProc
    command: ["/usr/bin/python3", root.daemonPath, "--daemon"]
    onExited: function(exitCode) {
      root.connected = false
      root.state = "starting"
      root.message = "Omadesk stopped"
      if (exitCode !== 0)
        root.lastError = "Omadesk exited with code " + exitCode
      socketLoader.active = false
      Qt.callLater(function() {
        if (!daemonProc.running) daemonRestartTimer.start()
      })
    }
  }

  Timer {
    id: daemonRestartTimer
    interval: 2000
    repeat: false
    onTriggered: root.ensureDaemon()
  }

  Timer {
    id: socketWaitTimer
    interval: 500
    repeat: true
    running: false
    triggeredOnStart: true
    onTriggered: {
      if (socketLoader.active) {
        stop()
        return
      }
      socketCheckProc.running = true
    }
  }

  Process {
    id: socketCheckProc
    command: ["test", "-S", root.socketPath]
    onExited: function(exitCode) {
      if (exitCode === 0) {
        socketWaitTimer.stop()
        root.openSocket()
      }
    }
  }

  Timer {
    id: requestTimeout
    interval: 15000
    repeat: false
    onTriggered: {
      root.requestPending = false
      root.pendingById = ({})
      if (!root.connected) {
        root.state = "starting"
        root.message = "Omadesk is taking longer than usual to connect…"
      }
    }
  }

  Timer {
    id: pollTimer
    interval: root.pollIntervalMs
    repeat: true
    running: root.state === "ready" && root.socketLive && !root.requestPending
    onTriggered: root.refresh()
  }

  Loader {
    id: socketLoader
    active: false

    sourceComponent: Component {
      Socket {
        id: cmdSocket
        path: root.socketPath
        connected: true

        onConnectionStateChanged: {
          if (!connected) return
          root.message = "Connecting to desk…"
          root.refresh()
          root.reloadPresets()
          pollTimer.start()
        }

        onError: function(errorCode) {
          root.connected = false
          root.state = "starting"
          root.message = "Waiting for Omadesk…"
          socketLoader.active = false
          root.ensureDaemon()
        }

        parser: SplitParser {
          splitMarker: "\n"
          onRead: function(line) { root.handleLine(line) }
        }
      }
    }
  }

  FileView {
    id: configFile
    path: root.configPath
    watchChanges: true
    printErrors: false
    onLoaded: root.applyConfig(text())
    onLoadFailed: root.applyConfig("{}")
  }

  Component.onCompleted: {
    configFile.reload()
    ensureDaemon()
  }

  Component.onDestruction: {
    if (daemonProc.running) daemonProc.running = false
    socketLoader.active = false
  }
}
