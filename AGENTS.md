# AGENTS.md

Context for AI coding agents (and humans) working on this repo. Read this before changing code.

## What this is

Local-only control of SNOOZ / Breez white-noise machines from Hubitat, with no Snooz cloud involved.

```
Snooz (BLE) ──► ble_ws_service (Docker, Linux host, BlueZ) ──WebSocket :8765──► Hubitat parent driver ──► child devices
```

Two halves ship together and share a JSON protocol:

| Path | What |
|---|---|
| `ble_ws_service/` | Python 3.13 asyncio service in Docker (`python:3.13-slim`). Talks BLE via `pysnooz` + `bleak`, serves the WebSocket API. |
| `drivers/snooz-ble-bridge-parent.groovy` | Hubitat parent: holds the WS connection, health loop, discovers devices, routes commands/events. |
| `drivers/snooz-ble-device-child.groovy` | Hubitat child: `Switch`, `FanControl`, `Refresh` + custom `setLevel`; volume shown as `volume`/`level`/`speed`. |
| `tools/discover_snooz.py` | Scans for Snooz devices and reads the pairing password (device must be in pairing mode). |
| `tools/ble_service_client_cli.py` | Interactive WS client for testing the service by hand. |
| `dev/` | Local scratch, **gitignored**. Not part of the product. |

**Sibling repo:** `hubitat-august-ble` (August/Yale locks) has the same shape. This repo's `ws_server.py` and manager pattern were copied from it, so a bug in shared code is probably in both (e.g. the `_process_request` tuple bug, fixed in both on 2026-11-15). Keep the two AGENTS.md files structured alike.

## Ground rules

- **The repo is public.** Never commit or echo IPs, host names, WS tokens or pairing passwords. They live in gitignored files: `**/config.yaml` (token, pairing passwords), `.pi_hosts`, `.pi_password`. Leave the last two unopened unless the owner asks.
- **Reading a deployed host is fine; writing needs the owner's go-ahead for that specific change.** Don't restart Docker or recreate containers as a side effect. Host OS config and this service's deployment both live in the owner's separate fleet (Ansible) repo. The generic `ble_ws_service` role (shared with August) deploys it. **Don't deploy with ad-hoc docker compose, and never hand-edit `config.yaml` on a fleet host.** It's templated from host_vars plus Vault, and the next run would overwrite your change.
- **Say plainly when a change hasn't been tested on hardware.** Real BLE behavior can only be verified on a host with a Snooz in range.
- **Hold new features until the owner asks.** Candidates are listed under Deferred ideas.

## Service internals (`ble_ws_service/`)

- `main.py`: loads `config.yaml`, builds one `BleSnooz` per device, starts `SnoozManager` then `WebSocketServer`, handles SIGINT/SIGTERM.
- `config.py`: dataclasses + YAML loader (see `config_example.yaml`). Each device needs `device_name`, `password` (16 hex) and `address` and/or `name`.
- `ble_snooz.py` (`BleSnooz`): wraps one `pysnooz.SnoozDevice`.
  - Parses Bleak adverts: picks the manufacturer payload itself (0xFFFF, else the first entry) and **always injects the configured password**, but uses pysnooz's `parse_firmware_flags` / `get_device_model`. An unrecognized name that matched config is still accepted as Snooz-family.
  - Coalesces state callbacks (250 ms) into one `device_state` event. `snapshot()` builds the state dict sent to Hubitat.
- `snooz_manager.py` (`SnoozManager`):
  - Matches devices by address (Linux MAC) or advertised name (needed on macOS, where Bleak reports CoreBluetooth UUIDs).
  - Initial 12 s scan, then a 30 s rescan loop for devices that aren't bound yet.
  - Before each of those scans (and before rediscovery), `_close_stale_connections` disconnects any configured address that BlueZ still shows as connected while we don't own the link (0.1.5). A connected Snooz doesn't advertise, so a leftover link would otherwise hide it forever.
  - A global `asyncio.Semaphore(1)` serializes every BLE command across all devices. A transitioned command (`noise_off` with `duration_s`) holds it **for the whole fade**.
  - On `DEVICE_UNAVAILABLE` it scans 8 s for that device, swaps in the fresh `BLEDevice` and retries once (0.1.3; handles BlueZ forgetting devices after a `bluetoothd` restart).
  - `BleSnooz.start()` / `refresh_state()` run outside the semaphore (startup and rescan loop).
- `ws_server.py` (`WebSocketServer`): `websockets.asyncio.server`. Optional bearer-token auth in `_process_request`, which **must return a `Response` from `connection.respond(...)` or `None`**; a tuple breaks the handshake. Messages on one connection are handled **sequentially**, so a slow BLE op delays later commands and the heartbeat reply on that socket.
- `version.py`: service version, logged at startup. Bump it in release commits.
- Logging: each `device_state` broadcast is logged at INFO with its client count. Event-callback and listener errors are logged at WARNING; they used to be swallowed at DEBUG.
- The startup line "Connected: GATT model number=…, firmware revision=…" shows the device's own GATT strings. `snapshot()`'s `model` / `firmware_version` come from pysnooz's advertisement parsing. They differ by design (e.g. GATT "V4"/"4.0" vs advert ORIGINAL/V4).

## WebSocket protocol (contract with the Groovy drivers)

Changing any of this requires matching driver changes, plus `tools/ble_service_client_cli.py` and the README. Users must then re-import the drivers on their hub, so keep changes backward compatible where possible.

- Client → server: `{"type":"command","request_id":"…","command":"…","device_name":"…", ...args}`.
- Server → client response: `{"type":"response","request_id":…,"status":"ok","data":{…}}` or `{…,"status":"error","error":"<message>"}`.
- Server → client event: `{"type":"event","event":"device_state","device_name":"…","state":{snapshot}}`.
- Snapshot keys: `device_name, address, display_name, connected, connection_status, model, firmware_version, state{on, volume, light_on, light_brightness, night_mode_enabled}`.
- Auth: `Authorization: Bearer <token>` during the handshake. Missing token → 401, wrong token → 403.

| Command | Args | Used by Hubitat? |
|---|---|---|
| `heartbeat`, `list_devices` | — | yes |
| `get_state` | device_name | yes |
| `noise_on` | device_name, `volume?` | yes |
| `noise_off` | device_name, `duration_s?` | yes (never sends `duration_s`) |
| `set_volume` | device_name, `volume` 0–100 | yes |
| `light_on`, `light_off`, `set_light_brightness` | device_name, `brightness` 0–100 | no (CLI only) |

Driver behaviour to keep in mind:
- Parent runs `healthTick` every minute. It sends an app-level `heartbeat` after 55 s with no received message, resets the socket if a heartbeat goes 75 s without a reply or a connect is stuck for 90 s, and drops all pending requests whenever the socket isn't open. On open it calls `list_devices`, auto-creates children, then `get_state` for each.
- Child DNI is `<parentId>:<device_name>`, so **renaming a device in `config.yaml` creates a new child device**.
- The child updates `switch`/volume **optimistically** when a command is sent; real state arrives through `applySnapshot`. A failed command leaves the optimistic state in place.
- Volume is mirrored into `volume`, `level` and `speed`. Homebridge (via MakerAPI) treats the device as a fan, with speed % = volume.
- Driver header comments carry their own `Version:` (currently 0.1.0), separate from `version.py`.

## Dependencies: pysnooz notes (0.11)

- `ble_ws_service/requirements.txt` is pinned exactly: bleak 3.1.0, bleak-retry-connector 4.7.1 (imported directly since 0.1.5), pysnooz 0.11.0, websockets 17.4, PyYAML 6.0.3. `tools/requirements.txt` is unpinned. The other ~20 transitive deps on Linux (habluetooth, dbus-fast, cryptography, …) are **not** pinned. pysnooz caps `transitions<0.9` and `Events<0.5`.
- **We use pysnooz private internals.** On every bump, confirm these still exist:
  - `SnoozDevice._device`: swapped by `update_ble_device`; pysnooz reconnects via `ble_device_callback=lambda: self._device`.
  - `SnoozDevice._api.async_read_state(use_cached=False)` and `SnoozDevice._store.patch()`: used by `refresh_state`.
- Since 0.10.1, names starting with `Snooz`, `Szpro` (some Pros advertise as "Szpro EF22") or `Breez` are recognized, every firmware version is accepted, and most GATT characteristics are optional.
- Since 0.11.0:
  - A newer command cleanly supersedes a running one; the old caller gets `CANCELLED` instead of a raised `CancelledError`.
  - `toggle()` exists and reverses a running fade.
  - Our global semaphore means commands never overlap today.
- `async_execute_command` for a transitioned command (duration) returns only after the fade finishes.
- bleak-retry-connector: we import `get_device`, `close_stale_connections` and `get_connected_devices`. The last one isn't in its `__all__`, so confirm it still exists on bumps.
- bleak 3.1.0 detects a dead D-Bus connection and resets its state instead of reporting a stale "Connected" (relevant because the container mounts the host's `/var/run/dbus`).
- pysnooz went three years without a release (0.10.0 in 2023 → 0.10.1/0.11.0 in Nov 2026). Diff the sdists of `device.py`, `commands.py`, `advertisement.py`, `api.py` and `store.py` rather than trusting release notes.

## Working on it

```bash
# verify against pinned deps (no automated test suite yet)
python3.13 -m venv <venv> && <venv>/bin/pip install -r ble_ws_service/requirements.txt
cd ble_ws_service && PYTHONPATH=. <venv>/bin/python -W error::DeprecationWarning \
  -c "import main, config, ble_snooz, snooz_manager, ws_server"

# confirm Linux wheels exist for the image (python:3.13-slim has no compiler; events is pure-Python, fine)
uv pip compile requirements.txt --python-version 3.13 --python-platform aarch64-unknown-linux-gnu
uv pip compile requirements.txt --python-version 3.13 --python-platform x86_64-unknown-linux-gnu

# release to the owner's fleet — in the fleet repo, owner-approved:
#   1. merge here and note the merge commit
#   2. set ble_snooz_version: <commit> in inventory/group_vars/all/versions.yml
#   3. ansible-playbook playbooks/ble_ws_service.yml --tags snooz --check --diff   # dry run
#   4. ansible-playbook playbooks/ble_ws_service.yml --tags snooz [--limit <host>]
# rollback = set the previous commit and re-run (it rebuilds)

# outside the fleet (README users): cd <checkout>/ble_ws_service && sudo docker compose up -d --build
```

- Offline checks: use fake `BLEDevice` / `AdvertisementData` for advert parsing and binding, and a stub manager for `WebSocketServer` (connect with `websockets.asyncio.client` and test missing, wrong and correct tokens).
- Dependency bumps: PyPI JSON (`https://pypi.org/pypi/<pkg>/json`) works from the dev machine; GitHub raw/API may time out, so diff the PyPI sdists instead.
- Fleet role details:
  - Images carry the `org.opencontainers.image.revision` label; the role rebuilds when it doesn't match the pin.
  - Low-memory hosts get an image built on a bigger Pi and copied in.
  - `config.yaml` is owned by uid/gid 1000 (the container's `appuser`), mode 0600, and a change triggers a container restart.
  - Config keys: `service.websocket.{host,port,auth_token}`, `service.devices[]{device_name,password,address?,name?}`. Quote passwords and MACs in vars: unquoted `0000…`/octal-looking passwords and all-digit MACs parse as ints.
- Hardware check after a release:
  - every device connected, with on/volume unchanged by the restart;
  - for devices that are OFF, a silent set_volume +1 then back, expecting an event and a `device_state -> N client(s)` log line with N including Hubitat;
  - audible on/off only with the owner's OK;
  - optionally `docker kill` plus a restart to exercise the stale-link cleanup.
- Branches: `fix/…`, `chore/…`, `feat/…`. PR into `main`. Release commits are titled `0.1.x - <summary>`, with `version.py` bumped in the same change.
- Style: `from __future__ import annotations`, dataclasses, `_LOGGER` messages prefixed `[device_name]`, async throughout. Comments explain why, not what. Keep the README feature/limitation lists in sync.

## Gotchas

- **Keep the explicit compose project `name: snooz-ble-ws-service`.** Both BLE repos used to default to the directory name `ble_ws_service`. On a host running both, `docker compose down` or `--remove-orphans` in one checkout could remove the other's container (found 2026-11-15 in a fleet review). Both now set explicit names (August: `hubitat-august-ble`), added in Snooz 0.1.4, and every host was migrated on 2026-11-15.
- **Never SIGKILL the container (`docker rm -f`, `docker kill`); stop it gracefully.** On SIGTERM the service disconnects every Snooz. A kill skips that, so bluetoothd keeps the GATT link, the Snooz stops advertising, and the service can't find it again. That cost the 2026-11-15 canary about 4 minutes, until the link was dropped with Device1.Disconnect over D-Bus. Since 0.1.5 the service clears such links itself before scanning, and compose sets `stop_grace_period: 30s` so several devices can disconnect before Docker gives up. Verified 2026-11-15 on 0.1.5: after `docker kill`, the restarted container dropped the stale link at +1.6 s and the device was connected again at +6.5 s.
- **On hosts shared with the August service, this one listens on 8766.** August keeps 8765 and both use host networking. Set `service.websocket.port` in `config.yaml` and the Hubitat parent's port to match. The two containers also share one Bluetooth adapter and can't coordinate scans or connects, so intermittent connect failures there may be contention.
- **Don't build the image on a low-memory host.** A 1 GB Pi with no swap hangs when it runs out of memory (ping works, SSH doesn't) until it's power-cycled. That happened during unattended-upgrades on 2026-11-06. Build elsewhere and `docker load`, or check free memory first.
- **`bluetoothd` goes away and comes back; the service must survive it.**
  - Most restarts are BlueZ 5.72 (Ubuntu 24.04) segfaults: every 1–3 days on a sibling August host, and one Snooz host runs the same version. systemd restarts it.
  - Unattended upgrades (daily around 06:10–07:00, no automatic reboot) are a lesser cause; the libc6 upgrades on 2026-11-13 didn't restart it.
  - Recovery today: the `DEVICE_UNAVAILABLE` rediscovery above, plus bleak's D-Bus reset.
  - There is **no scanner watchdog** (the August service has one). While nothing sends commands, a device can sit disconnected and its Hubitat state goes stale.
- **At boot the container can start before `bluetoothd` is ready.** Docker live-restore is off, so Docker upgrades restart the container too. By code reading (not observed), an exception from the initial scan propagates out of `SnoozManager.start()`, the process exits, and `restart: unless-stopped` retries. Don't swallow that error without adding a retry.
- **A WS test client is not proof that events are missing; check Hubitat.** On 2026-11-15 a test client reported no `device_state` events after commands. Hubitat's Events tab showed every change arriving from the bridge parent, so the test listener was wrong: it read the event's `state` at the top level, but the on/volume values are at `state.state`. Since 0.1.5 each broadcast is also logged at INFO (`device_state -> N client(s)`).
- **Hosts run different BlueZ versions** (5.72–5.85, on Debian 13 and Ubuntu 24.04/26.04, Pi 4 and x86 VM). Don't assume one BlueZ behavior.

## Deferred ideas (discussed, intentionally not done yet)

1. Handle WS messages concurrently (a task per message) so heartbeats aren't starved by slow BLE commands. This is the prerequisite for #2.
2. Fade / sleep timer: run transitioned commands outside the global semaphore as per-device background tasks, ack immediately, and let a newer command supersede the fade (pysnooz ≥ 0.11.0). Add a child command such as `fadeOff(minutes)`; `toggle()` becomes useful then.
3. Hubitat: on an error response for a device command, re-request `get_state` so the child's optimistic state is corrected. The parent's `pending` metadata needs `deviceName` for that.
4. Expose the Snooz Pro night light (the service already supports the light commands) in the child driver.
5. Pin transitive deps with a Linux lock/constraints file for reproducible Docker builds.
