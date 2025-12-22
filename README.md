# Hubitat Snooz BLE

Local-only **Snooz / Breez white-noise machine** integration using a BLE-backed **WebSocket service** and **Hubitat parent/child drivers**.

This project lets Hubitat control Snooz devices on your LAN without relying on any cloud service.

```text
┌──────────────────────┐
│     Snooz / Breez    │
│   (Noise Machine)    │
│                      │
│   Bluetooth (BLE)    │
└───────────▲──────────┘
            │
            │ BLE
            │
┌───────────┴──────────┐
│  BLE Compute Device  │
│  (e.g. Raspberry Pi) │
│                      │
│ snooz_ble_ws_service │
└───────────▲──────────┘
            │
            │ WebSocket (LAN)
            │
┌───────────┴──────────┐
│     Hubitat Hub      │
│                      │
└──────────────────────┘
```

## What you get?

### Features

- ✅ On / Off control from Hubitat
- ✅ Volume control (0–100)
- ✅ Push updates over WebSocket (device state events) when available
- ✅ Multi-device support (one BLE host, multiple Snooz devices)
- ✅ Optional auth token between Hubitat and the service
- ✅ Docker + docker-compose ready for easy deployment
- ✅ Homebridge friendly via Hubitat MakerAPI (Fan device w/ 0–100% “speed” mapped to volume)

### Limitations / Notes

- BLE reliability depends heavily on distance, interference, and your host’s Bluetooth hardware.
- Some Snooz models / firmware may not report state until the first command is sent (depending on BLE behavior).
- This is LAN-local. Do not expose the WebSocket port to the public internet.
- **macOS support**: BLE addresses may appear as a UUID (not a MAC). On Mac, configure devices by name match (e.g. `Snooz-040F`) instead of address.

---

## Prerequisites 

### Hardware
 - Snooz / Breez device (Bluetooth-capable)
 - BLE-capable compute device
 - Hubitat Hub

### Software
 - BLE Compute Device
   - OS: Linux or MacOS
     - tested with Ubuntu 24.04 server
     - tested with macOS. Use device name matching instead of address (macOS often provides UUID-like addresses)
   - Docker & Docker Compose (Linux only)
     - Used to run `ble_ws_service`
   - BlueZ (Linux Bluetooth stack) + DBus
 - Python 3
   - Used to run setup `discover_snooz` script

### Network
 - Hubitat initiates a persistent WebSocket connection to the BLE host on port **8765**
 - Static DHCP reservation (or static IP) for the BLE host

### Credentials & Configuration
 - Snooz device pairing password (hex) per device (used by `pysnooz`)
 - Bluetooth device identifier:
     - Linux: MAC address (e.g. `AA:BB:CC:DD:EE:FF`)
     - macOS: often a UUID-like string; use name match instead (e.g. `Snooz-040F`)
 - WebSocket authentication token (Optional)
   - Shared secret between Hubitat and the BLE service
---

### Security Notes

- If your LAN contains untrusted devices, enable the auth token.
- Do not expose port **8765** to the internet.
- Treat pairing passwords like credentials. Store them safely.

---

## Getting Started

### 1. Obtain Snooz Device Info (Address/Name + Password)
This repo includes a `discover_snooz.py` helper script (in tools/) to help you:
- discover nearby Snooz devices
- identify the device name and address
- capture/save the pairing password required for the service config

_On macOS, you should plan to configure devices using match_name (the advertised name) rather than address._

### 2. Setup BLE Compute Device (Linux)

1. [Install Docker & Docker Compose](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository)
2. Install & Enable Bluetooth dependencies
```bash
sudo apt update
sudo apt install -y bluez dbus
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```
3. Clone repo
```bash
cd /opt
git clone https://github.com/K-MTG/Hubitat-Snooz-BLE.git
cd Hubitat-Snooz-BLE/snooz_ble_ws_service
```
4. Create `config.yaml` using `config_example.yaml` as reference. 
5. Start container `sudo docker compose up -d --build`
---

### Setup Hubitat Driver
1. In Hubitat, go to **Drivers Code**
2. Add both drivers (Import URL):
   - Parent: `https://raw.githubusercontent.com/K-MTG/hubitat-snooz-ble/main/drivers/snooz-ble-bridge-parent.groovy`
   - Child: `https://raw.githubusercontent.com/K-MTG/hubitat-snooz-ble/main/drivers/snooz-ble-device-child.groovy`
3. Create a virtual device using **Snooz BLE Bridge (Parent)**
4. Configure the WebSocket host, port, and token (optional) under Preferences
5. Click **Initialize**

Child Snooz devices will be created automatically.
---

## Components

### BLE WS Service

A Python service that bridges **Snooz BLE** to a **WebSocket API**, suitable for Hubitat or custom automation systems.

The service:

* 📡 Connects to Snooz devices over **Bluetooth Low Energy (BLE)** using `pysnooz`
* 🌐 Exposes a WebSocket API with optional authentication
* 🔊 Supports core controls:
  * `noise_on`
  * `noise_off`
  * `set_volume` (0–100)
  * `get_state`
* 🔐 Supports multiple devices
* 🐳 Docker + docker-compose ready

### Hubitat Driver (Parent + Child)

#### Parent Driver: “Snooz BLE Bridge (Parent)”
- Maintains the persistent WebSocket connection
- Discovers devices (`list_devices`) and auto-creates children
- Routes WS state events → child devices
- Routes child commands → WS service
- Tracks connection status via:
  - `is_connected`
  - `connection_status`
- Uses a periodic health loop (watchdog / heartbeat) to recover from:
  - silent WebSocket failures
  - stale connections after reboots
  - “pending request” growth when a connection is dead but appears open

#### Child Driver: “Snooz BLE Device (Child)”
- Exposes Hubitat capabilities:
  - `Switch` (on/off)
  - `FanControl` (speed mapped to volume)
  - `Refresh`
- Publishes:
  - `level` (0–100) for percent controls
  - `speed` and `volume` for display/debugging

---

## Credits & References

This project builds on excellent work:
- **pysnooz (Snooz/Breez BLE control)**  
  https://github.com/AustinBrunkhorst/pysnooz
- Home Assistant integrations (for patterns around BLE reliability + device state handling)
  https://github.com/home-assistant/core/tree/dev/homeassistant/components/snooz

