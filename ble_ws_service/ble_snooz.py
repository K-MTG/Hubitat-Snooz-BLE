from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from pysnooz.advertisement import get_device_display_name, get_device_model, parse_firmware_flags
from pysnooz.const import MODEL_NAME_SNOOZ, SNOOZ_ADVERTISEMENT_LENGTH
from pysnooz.model import SnoozAdvertisementData, SnoozDeviceModel, SnoozDeviceState
from pysnooz.device import SnoozDevice


_LOGGER = logging.getLogger(__name__)


@dataclass
class DiscoveredSnooz:
    ble_device: BLEDevice
    advertisement_data: AdvertisementData
    snooz_adv: SnoozAdvertisementData
    display_name: str


def parse_snooz_advertisement_from_bleak(
    name: str,
    adv: AdvertisementData,
    password_hex: str,
) -> Optional[SnoozAdvertisementData]:
    """
    Build SnoozAdvertisementData using Bleak AdvertisementData.
    We *always* inject the configured password (even if device is not in pairing mode).

    pysnooz.parse_snooz_advertisement needs a home-assistant BluetoothServiceInfo and only
    returns the password while pairing, so we pick the payload ourselves but reuse pysnooz's
    firmware and model parsing to stay in sync with its supported devices.
    """
    mfg = adv.manufacturer_data or {}
    payload: Optional[bytes] = None

    # Snooz often uses 0xFFFF (65535). If not present, fall back to first entry.
    if 0xFFFF in mfg:
        payload = mfg[0xFFFF]
    elif len(mfg) > 0:
        payload = next(iter(mfg.values()))

    if not payload or len(payload) != SNOOZ_ADVERTISEMENT_LENGTH:
        return None

    flags = parse_firmware_flags(payload[0])
    if flags is None:
        return None

    # pysnooz recognizes names starting with "Snooz", "Szpro" or "Breez"
    model = get_device_model(name or "", flags.firmware_version)
    if model == SnoozDeviceModel.UNSUPPORTED:
        # The device matched a configured address/name and has a valid Snooz payload,
        # so trust the config and tell original/Pro apart by firmware, as for "Snooz".
        model = get_device_model(MODEL_NAME_SNOOZ, flags.firmware_version)

    return SnoozAdvertisementData(
        model=model,
        firmware_version=flags.firmware_version,
        password=password_hex,
    )


class BleSnooz:
    """
    Thin wrapper around pysnooz.SnoozDevice that also provides:
      - discovery binding (BLEDevice + adv parsing)
      - snapshot formatting for WS events
    """

    def __init__(self, device_name: str, address: str, password_hex: str, match_name: Optional[str] = None) -> None:
        self.device_name = device_name
        self.address = address.upper() if address else ""  # allow empty on macOS if using name matching
        self.match_name = match_name  # advertised name matching (best for macOS)
        self.password_hex = password_hex.lower()

        self._device: Optional[SnoozDevice] = None
        self._discovered: Optional[DiscoveredSnooz] = None
        self._state_callbacks: list[Callable[[dict], Any]] = []
        self._unsubscribe: Optional[Callable[[], None]] = None

        # coalesce rapid state changes
        self._event_task: Optional[asyncio.Task] = None
        self._event_lock = asyncio.Lock()

    def is_ready(self) -> bool:
        return self._device is not None and self._discovered is not None

    def is_connected(self) -> bool:
        return self._device is not None and self._device.is_connected

    def bind_discovery(self, ble_device: BLEDevice, adv: AdvertisementData) -> bool:
        name = adv.local_name or ble_device.name or "Snooz"
        snooz_adv = parse_snooz_advertisement_from_bleak(name=name, adv=adv, password_hex=self.password_hex)
        if snooz_adv is None:
            # log ids/lengths only: a pairing-mode payload contains the password
            mfg = adv.manufacturer_data or {}
            _LOGGER.warning(
                "[%s] Found %s (%s) but its advertisement isn't a recognized Snooz payload "
                "(manufacturer data: %s)",
                self.device_name, name, ble_device.address,
                ", ".join(f"0x{k:04X}={len(v)} bytes" for k, v in mfg.items()) or "none",
            )
            return False

        display_name = get_device_display_name(name, ble_device.address)

        self._discovered = DiscoveredSnooz(
            ble_device=ble_device,
            advertisement_data=adv,
            snooz_adv=snooz_adv,
            display_name=display_name,
        )
        self._device = SnoozDevice(ble_device, snooz_adv)
        _LOGGER.info("[%s] Bound discovery: %s (%s)", self.device_name, display_name, ble_device.address)

        # Always record the discovered address (on macOS this will be a UUID-like value)
        self.address = ble_device.address.upper()

        # subscribe to state changes for WS events
        self._unsubscribe = self._device.subscribe_to_state_change(self._on_state_change)
        return True

    def update_ble_device(self, ble_device: BLEDevice, adv: AdvertisementData) -> None:
        """
        Swap in a freshly scanned BLEDevice without rebuilding the SnoozDevice
        (keeps its state store and our state subscription).
        """
        if not self._device or not self._discovered:
            return
        self._device._device = ble_device
        self._discovered.ble_device = ble_device
        self._discovered.advertisement_data = adv

    async def start(self) -> None:
        """
        Ensure the device is connected enough to fetch info (also tends to “warm up” state).
        """
        if not self._device:
            raise RuntimeError(f"[{self.device_name}] Device not discovered/bound yet")

        try:
            info = await self._device.async_get_info()
            if info:
                # the device's own GATT strings; snapshot() reports pysnooz's advert-based model/firmware
                snooz_adv = self._discovered.snooz_adv if self._discovered else None
                _LOGGER.info(
                    "[%s] Connected: GATT model number=%s, firmware revision=%s (advertised as %s, firmware %s)",
                    self.device_name, info.model, info.firmware,
                    snooz_adv.model.name if snooz_adv else None,
                    snooz_adv.firmware_version.name if snooz_adv else None,
                )
            else:
                _LOGGER.info("[%s] Connected (info unavailable)", self.device_name)

            # pull current state immediately so "state <device>" is populated at startup
            await self.refresh_state()

        except Exception:
            _LOGGER.exception("[%s] Failed to start/connect", self.device_name)
            raise

    async def refresh_state(self) -> None:
        """
        Force a one-time state read so snapshot() is populated at startup,
        even before any commands are sent.

        This uses pysnooz internals:
          - self._device._api.async_read_state()
          - self._device._store.patch(...)
        """
        if not self._device or not self._device.is_connected:
            return

        api = getattr(self._device, "_api", None)
        store = getattr(self._device, "_store", None)

        if api is None or store is None:
            _LOGGER.debug("[%s] refresh_state: api/store not available", self.device_name)
            return

        # Read current state from device (GATT read)
        state = await api.async_read_state(use_cached=False)

        # Patch into store and fire normal WS event path
        if store.patch(state):
            self._on_state_change()

    async def stop(self) -> None:
        if self._event_task and not self._event_task.done():
            self._event_task.cancel()

        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None

        if self._device:
            try:
                await self._device.async_disconnect()
            except Exception:
                _LOGGER.debug("[%s] Error during disconnect", self.device_name, exc_info=True)

    def register_state_callback(self, cb: Callable[[dict], Any]) -> None:
        self._state_callbacks.append(cb)

    def snapshot(self) -> dict:
        disc = self._discovered
        dev = self._device
        state: SnoozDeviceState | None = dev.state if dev else None

        return {
            "device_name": self.device_name,
            "address": self.address,
            "display_name": disc.display_name if disc else None,
            "connected": bool(dev and dev.is_connected),
            "connection_status": dev.connection_status.name if dev else "UNKNOWN",
            "model": disc.snooz_adv.model.name if disc else None,
            "firmware_version": disc.snooz_adv.firmware_version.name if disc else None,
            "state": {
                "on": state.on if state else None,
                "volume": state.volume if state else None,
                "light_on": state.light_on if state else None,
                "light_brightness": state.light_brightness if state else None,
                "night_mode_enabled": state.night_mode_enabled if state else None,
            },
        }

    def _on_state_change(self) -> None:
        """
        pysnooz may fire multiple callbacks quickly; coalesce into one WS event.
        """
        async def _emit():
            async with self._event_lock:
                await asyncio.sleep(0.25)
                event = {"type": "device_state", "device_name": self.device_name, "state": self.snapshot()}
                for cb in self._state_callbacks:
                    try:
                        maybe_coro = cb(event)
                        if asyncio.iscoroutine(maybe_coro):
                            await maybe_coro
                    except Exception:
                        _LOGGER.warning("[%s] State callback error", self.device_name, exc_info=True)

        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
        self._event_task = asyncio.create_task(_emit())
