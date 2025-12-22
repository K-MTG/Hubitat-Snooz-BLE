/**
 * Snooz BLE Bridge (Parent)
 *
 * Filename: snooz-ble-bridge-parent.groovy
 * Version:  0.1.0
 *
 * Description:
 * - Maintains a persistent WebSocket connection to your Snooz BLE WS service
 * - Discovers devices via list_devices
 * - Creates/manages child devices
 * - Routes WS events/responses to children
 * - Routes child commands to WS (noise_on/noise_off/set_volume/get_state)
 */

import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import groovy.transform.Field

metadata {
    definition(
        name: "Snooz BLE Bridge (Parent)",
        namespace: "k-mtg",
        author: "K-MTG"
        // importUrl: "https://raw.githubusercontent.com/<you>/<repo>/main/drivers/snooz-ble-bridge-parent.groovy"
    ) {
        capability "Initialize"
        capability "Refresh"

        command "Connect"
        command "Disconnect"

        attribute "is_connected", "bool"
        attribute "connection_status", "string"
    }

    preferences {
        input name: "wsHost", type: "string", title: "WebSocket Host / IP", required: true
        input name: "wsPort", type: "number", title: "WebSocket Port", required: true, defaultValue: 8765
        input name: "apiToken", type: "password", title: "Optional API Token (Bearer)", required: false
        input name: "debugLogging", type: "bool", title: "Enable debug logging", defaultValue: false
    }
}

/* ================= Tunables ================= */

// Runs once per minute; heartbeat logic uses timestamps
@Field final Long CONNECT_STUCK_MS        = 90_000L
@Field final Long HEARTBEAT_EVERY_MS      = 55_000L
@Field final Long HEARTBEAT_TIMEOUT_MS    = 75_000L

/* ================= Lifecycle ================= */

def installed() {
    logInfo "Installed"
    ensureState(true)
    setupSchedules()
    runIn(2, "Connect")
}

def updated() {
    logInfo "Updated"
    ensureState(false)
    setupSchedules()
    // Do NOT force-connect here
}

def initialize() {
    logInfo "initialize()"
    ensureState(false)
    setupSchedules()
    Connect()
}

private void setupSchedules() {
    unschedule("healthTick")
    runEvery1Minute("healthTick")
}

private void ensureState(boolean resetUi = false) {
    if (state.pending == null) state.pending = [:]
    if (state.manualDisconnect == null) state.manualDisconnect = false
    if (state.connecting == null) state.connecting = false
    if (state.socketOpen == null) state.socketOpen = false
    if (state.connectingSince == null) state.connectingSince = 0L

    // Heartbeat tracking
    if (state.hbReqId == null) state.hbReqId = null
    if (state.hbSentAt == null) state.hbSentAt = 0L
    if (state.lastRxAt == null) state.lastRxAt = 0L

    if (resetUi) {
        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: "starting")
    } else {
        if (device.currentValue("is_connected") == null) sendEvent(name: "is_connected", value: false)
        if (!device.currentValue("connection_status")) sendEvent(name: "connection_status", value: "starting")
    }
}

/* ================= Health Loop (Watchdog + Heartbeat) ================= */

def healthTick() {
    ensureState(false)

    if (state.manualDisconnect) {
        logDebug "Health: manualDisconnect=true; skipping"
        return
    }

    long nowMs = now()

    // 1) stuck connecting?
    if (state.connecting && state.connectingSince) {
        long age = nowMs - (state.connectingSince as Long)
        if (age > CONNECT_STUCK_MS) {
            logWarn "Health: connecting stuck for ${(age/1000) as int}s; resetting"
            forceSocketReset("connecting_stuck")
        }
        return
    }

    // 2) socket open => heartbeat
    if (state.socketOpen) {
        if (state.hbReqId) {
            long hbAge = nowMs - ((state.hbSentAt ?: 0L) as Long)
            if (hbAge > HEARTBEAT_TIMEOUT_MS) {
                logWarn "Health: heartbeat timed out after ${(hbAge/1000) as int}s; resetting"
                forceSocketReset("heartbeat_timeout")
            } else {
                logDebug "Health: heartbeat pending age=${(hbAge/1000) as int}s"
            }
            return
        }

        long sinceRx = nowMs - ((state.lastRxAt ?: 0L) as Long)
        if (sinceRx >= HEARTBEAT_EVERY_MS) {
            sendHeartbeat()
        } else {
            logDebug "Health: ok (lastRx ${(sinceRx/1000) as int}s ago)"
        }
        return
    }

    // 3) not connected
    if (!state.connecting) {
        logWarn "Health: not connected; attempting Connect()"
        Connect()
    }
}

/* ================= WebSocket ================= */

def Connect() {
    ensureState(false)

    if (state.manualDisconnect) {
        logInfo "Connect(): clearing manualDisconnect"
        state.manualDisconnect = false
    }

    if (state.socketOpen) {
        logInfo "Connect() ignored; already open"
        return
    }
    if (state.connecting) {
        logInfo "Connect() ignored; already connecting"
        return
    }

    state.connecting = true
    state.connectingSince = now()
    sendEvent(name: "connection_status", value: "connecting")

    String uri = "ws://${wsHost}:${wsPort}"
    Map options = [ pingInterval: 30 ]

    if (apiToken?.trim()) {
        options.headers = ["Authorization": "Bearer ${apiToken.trim()}"]
    }

    logInfo "Connecting to ${uri}"
    try {
        interfaces.webSocket.connect(options, uri)
    } catch (e) {
        state.connecting = false
        state.socketOpen = false
        state.pending = [:]
        state.hbReqId = null
        state.hbSentAt = 0L

        sendEvent(name: "is_connected", value: false)
        sendEvent(name: "connection_status", value: "connect_exception")
        logWarn "connect() threw: ${e}"
    }
}

def Disconnect() {
    ensureState(false)

    logInfo "Disconnecting (manual)"
    state.manualDisconnect = true

    forceSocketReset("manual_disconnect")

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: "disconnected")
}

def webSocketStatus(String status) {
    ensureState(false)
    logDebug "WS status: ${status}"

    if (status?.contains("open")) {
        state.connecting = false
        state.socketOpen = true

        state.hbReqId = null
        state.hbSentAt = 0L
        state.lastRxAt = now()

        sendEvent(name: "is_connected", value: true)
        sendEvent(name: "connection_status", value: "connected")

        // Discover devices
        listDevices()
        return
    }

    // disconnected
    state.connecting = false
    state.socketOpen = false

    state.pending = [:]
    state.hbReqId = null
    state.hbSentAt = 0L

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: status ?: "disconnected")
}

/* ================= Parsing ================= */

def parse(String msg) {
    ensureState(false)
    state.lastRxAt = now()
    logDebug "WS recv: ${msg}"

    def json
    try {
        json = new JsonSlurper().parseText(msg)
    } catch (e) {
        logWarn "Invalid JSON: ${e}"
        return
    }

    // Events: {type:"event", event:"device_state", device_name:"...", state:{...snapshot...}}
    if (json?.type == "event" && json?.event == "device_state") {
        ensureChild(json.device_name)?.applySnapshot(json.state as Map)
        return
    }

    if (json?.type == "response") {
        handleResponse(json)
        return
    }
}

/* ================= Responses ================= */

private void handleResponse(resp) {
    ensureState(false)

    if (resp?.status == "error") {
        def err = resp?.error ?: "unknown_error"
        logWarn "WS error: request_id=${resp.request_id} error=${err}"
        state.pending?.remove(resp.request_id)

        if (resp?.request_id && resp.request_id == state.hbReqId) {
            state.hbReqId = null
            state.hbSentAt = 0L
        }
        return
    }

    def meta = state.pending?.remove(resp.request_id)
    if (!meta) {
        logDebug "Ignoring response for unknown request_id=${resp.request_id}"
        if (resp?.request_id && resp.request_id == state.hbReqId) {
            state.hbReqId = null
            state.hbSentAt = 0L
        }
        return
    }

    if (meta.kind == "heartbeat") {
        state.hbReqId = null
        state.hbSentAt = 0L
        return
    }

    if (meta.kind == "list_devices") {
        // expected: resp.data.devices = ["bedroom","office",...]
        resp.data?.devices?.each { ensureChild(it?.toString()) }
        refreshAll()
        return
    }

    if (meta.kind == "get_state") {
        getChildByDeviceName(meta.deviceName)?.applySnapshot(resp.data as Map)
        return
    }

    // For command acks (noise_on/noise_off/set_volume), we don't need to do anything.
}

/* ================= Parent Commands ================= */

def refresh() {
    refreshAll()
}

private void listDevices() {
    sendCmd("list_devices", null, "list_devices")
}

private void refreshAll() {
    getChildDevices().each { cd ->
        def dn = cd.getDataValue("device_name") ?: cd.label
        if (dn) {
            sendCmd("get_state", dn, "get_state", [deviceName: dn])
        }
    }
}

def childGetState(String deviceName) {
    sendCmd("get_state", deviceName, "get_state", [deviceName: deviceName])
}

def childNoiseOn(String deviceName, Integer volume = null) {
    Map args = [:]
    if (volume != null) args.volume = volume
    sendCmd("noise_on", deviceName, "noise_on", args)
}

def childNoiseOff(String deviceName) {
    sendCmd("noise_off", deviceName, "noise_off")
}

def childSetVolume(String deviceName, Integer volume) {
    sendCmd("set_volume", deviceName, "set_volume", [volume: volume])
}

private void sendHeartbeat() {
    if (state.hbReqId) return
    sendCmd("heartbeat", null, "heartbeat")
}

/**
 * Generic WS command sender.
 */
private void sendCmd(String command, String deviceName, String kind, Map extra = [:]) {
    ensureState(false)

    if (!state.socketOpen) {
        logWarn "WS not open; dropping ${command}"
        return
    }

    def reqId = UUID.randomUUID().toString()
    state.pending[reqId] = ([kind: kind] + (extra ?: [:]))

    if (kind == "heartbeat") {
        state.hbReqId = reqId
        state.hbSentAt = now()
    }

    def payload = [
        type: "command",
        request_id: reqId,
        command: command
    ]

    if (deviceName) payload.device_name = deviceName

    // Merge any command args (ex: volume)
    (extra ?: [:]).each { k, v ->
        // don't leak meta-only keys into payload
        if (k in ["kind", "deviceName"]) return
        payload[k] = v
    }

    def json = JsonOutput.toJson(payload)
    logDebug "WS send: ${json}"
    interfaces.webSocket.sendMessage(json)
}

/* ================= Helpers ================= */

private void forceSocketReset(String reason) {
    state.connecting = false
    state.connectingSince = 0L
    state.socketOpen = false
    state.pending = [:]
    state.hbReqId = null
    state.hbSentAt = 0L

    try {
        interfaces.webSocket.close()
    } catch (e) {
        logDebug "WS close ignored (${reason}): ${e}"
    }

    sendEvent(name: "is_connected", value: false)
    sendEvent(name: "connection_status", value: reason)
}

/* ================= Child Devices ================= */

private ensureChild(String deviceName) {
    if (!deviceName) return null

    def dni = "${device.id}:${deviceName}"
    def child = getChildDevice(dni)
    if (child) return child

    logInfo "Creating child device: ${deviceName}"
    child = addChildDevice(
        "k-mtg",
        "Snooz BLE Device (Child)",
        dni,
        [label: deviceName, isComponent: true]
    )
    child.updateDataValue("device_name", deviceName)
    return child
}

private getChildByDeviceName(String deviceName) {
    if (!deviceName) return null
    getChildDevice("${device.id}:${deviceName}")
}

/* ================= Logging ================= */

private logDebug(msg) { if (debugLogging) log.debug "${device.displayName}: ${msg}" }
private logInfo(msg)  { log.info  "${device.displayName}: ${msg}" }
private logWarn(msg)  { log.warn  "${device.displayName}: ${msg}" }
