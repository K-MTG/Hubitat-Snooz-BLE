/**
 * Snooz BLE Device (Child)
 *
 * Filename: snooz-ble-device-child.groovy
 * Version:  0.1.0
 *
 * Description:
 * - Presents as Fan (Switch + FanControl)
 * - Uses setLevel(0-100) for volume percent
 */


metadata {
    definition(
        name: "Snooz BLE Device (Child)",
        namespace: "k-mtg",
        author: "K-MTG",
        importUrl: "https://raw.githubusercontent.com/K-MTG/Hubitat-Snooz-BLE/refs/heads/main/drivers/snooz-ble-device-child.groovy"
    ) {
        capability "Switch"
        capability "FanControl"
        capability "Refresh"

        attribute "volume", "number"
        attribute "level", "number"
        attribute "is_connected", "bool"
        attribute "connection_status", "string"
        attribute "model", "string"
        attribute "firmware_version", "string"
        attribute "display_name", "string"
        attribute "address", "string"

        // Implemented even without SwitchLevel capability (Homebridge fine control)
        command "setLevel", [[name: "level*", type: "NUMBER", description: "0-100"]]
    }

    preferences {
        input name: "defaultOnVolume", type: "number", title: "Default volume for on()", defaultValue: 50
        input name: "cycleSteps", type: "string",
              title: "cycleSpeed() steps (comma-separated 0-100)",
              defaultValue: "0,25,50,75,100",
              required: true
        input name: "debugLogging", type: "bool", title: "Enable debug logging", defaultValue: false
    }
}

def installed() { logInfo "Installed" }
def updated()   { logInfo "Updated" }

def refresh() {
    parent.childGetState(getDeviceName())
}

def on() {
    Integer v = currentVolumeOrDefault()
    if (v <= 0) v = clamp0_100((defaultOnVolume ?: 50) as Integer)

    logDebug "on() -> noise_on(volume=${v})"
    parent.childNoiseOn(getDeviceName(), v)

    sendEvent(name: "switch", value: "on")
    applyVolumeToAttributes(v)
}

def off() {
    logDebug "off() -> noise_off()"
    parent.childNoiseOff(getDeviceName())

    sendEvent(name: "switch", value: "off")
    applyVolumeToAttributes(0)
}

def setSpeed(speed) {
    Integer v = coerceSpeedToVolume(speed)
    if (v == null) {
        logWarn "setSpeed(${speed}) ignored: could not parse"
        return
    }
    _applyDesiredVolume(v)
}

/**
 * setLevel for fan speed percent.
 */
def setLevel(level, duration = null) {
    Integer v = clamp0_100(safeInt(level, 0))
    logDebug "setLevel(${level}) -> volume=${v}"
    _applyDesiredVolume(v)
}

def cycleSpeed() {
    List<Integer> steps = getCycleSteps()
    Integer cur = safeInt(device.currentValue("level"), safeInt(device.currentValue("volume"), 0))

    Integer next = steps.find { it > cur }
    if (next == null) next = steps[0]

    logDebug "cycleSpeed(): ${cur} -> ${next}"
    _applyDesiredVolume(next)
}

/* ===== Parent -> Child Updates ===== */

def applySnapshot(Map snap) {
    if (snap == null) return

    Map st = (snap.state instanceof Map) ? (snap.state as Map) : snap

    if (snap.containsKey("connected")) sendEvent(name: "is_connected", value: !!snap.connected)
    if (snap.connection_status) sendEvent(name: "connection_status", value: snap.connection_status.toString())
    if (snap.model) sendEvent(name: "model", value: snap.model.toString())
    if (snap.firmware_version) sendEvent(name: "firmware_version", value: snap.firmware_version.toString())
    if (snap.display_name) sendEvent(name: "display_name", value: snap.display_name.toString())
    if (snap.address) sendEvent(name: "address", value: snap.address.toString())

    if (st.on != null) {
        sendEvent(name: "switch", value: (st.on ? "on" : "off"))
    }

    if (st.volume != null) {
        Integer v
        try {
            v = clamp0_100((st.volume as Number).intValue())
        } catch (e) {
            logWarn "Invalid volume '${st.volume}'"
            v = null
        }

        if (v != null) {
            applyVolumeToAttributes(v)
            if (st.on == null) {
                sendEvent(name: "switch", value: (v > 0 ? "on" : "off"))
            }
        }
    }
}

/* ===== Internal volume application ===== */

private void _applyDesiredVolume(Integer v) {
    Integer vv = clamp0_100(v)

    if (vv <= 0) {
        off()
        return
    }

    def sw = device.currentValue("switch")
    if (sw == null || sw == "off") {
        parent.childNoiseOn(getDeviceName(), vv)
    } else {
        parent.childSetVolume(getDeviceName(), vv)
    }

    sendEvent(name: "switch", value: "on")
    applyVolumeToAttributes(vv)
}

private void applyVolumeToAttributes(Integer v) {
    Integer vv = clamp0_100(v)
    sendEvent(name: "volume", value: vv)
    sendEvent(name: "level", value: vv, unit: "%")
    sendEvent(name: "speed", value: vv.toString()) // FanControl attribute
}

/* ===== Helpers ===== */

private Integer currentVolumeOrDefault() {
    def v = device.currentValue("volume")
    if (v != null) {
        try { return clamp0_100(safeInt(v, 50)) } catch (e) { }
    }
    try {
        return clamp0_100((defaultOnVolume ?: 50) as Integer)
    } catch (e) {
        return 50
    }
}

private List<Integer> getCycleSteps() {
    String raw = (cycleSteps ?: "0,25,50,75,100").toString()
    List<Integer> vals = raw.split(",")
        .collect { it.trim() }
        .findAll { it?.isInteger() }
        .collect { clamp0_100(it.toInteger()) }
        .unique()
        .sort()
    if (!vals) vals = [0, 25, 50, 75, 100]
    return vals
}

private Integer coerceSpeedToVolume(def speed) {
    if (speed == null) return null
    try {
        if (speed instanceof Number) return clamp0_100((speed as Number).intValue())
        def s = speed.toString().trim()
        if (s.isInteger()) return clamp0_100(s.toInteger())
    } catch (e) { }

    def s2 = speed.toString().trim().toLowerCase()
    switch (s2) {
        case "off":    return 0
        case "on":     return 100
        case "low":    return 25
        case "medium": return 50
        case "high":   return 75
        default:
            return null
    }
}

private Integer safeInt(def v, Integer fallback = 0) {
    try {
        if (v == null) return fallback
        if (v instanceof Number) return (v as Number).intValue()
        String s = v.toString().trim()
        if (!s) return fallback
        if (s.isInteger()) return s.toInteger()
    } catch (e) { }
    return fallback
}

private Integer clamp0_100(Integer v) {
    if (v == null) return 0
    if (v < 0) return 0
    if (v > 100) return 100
    return v
}

private String getDeviceName() {
    device.getDataValue("device_name") ?: device.label
}

/* ===== Logging ===== */

private logDebug(msg) { if (debugLogging) log.debug "${device.displayName}: ${msg}" }
private logInfo(msg)  { log.info  "${device.displayName}: ${msg}" }
private logWarn(msg)  { log.warn  "${device.displayName}: ${msg}" }
