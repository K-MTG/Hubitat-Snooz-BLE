from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from pysnooz.const import FIRMWARE_PAIRING_FLAGS, FIRMWARE_VERSION_BY_FLAGS, SNOOZ_ADVERTISEMENT_LENGTH
from pysnooz.model import SnoozAdvertisementData, SnoozDeviceModel, SnoozFirmwareVersion, SnoozDeviceState
from pysnooz.device import SnoozDevice
from pysnooz import get_device_display_name  # exported by pysnooz/__init__.py


_LOGGER = logging.getLogger(__name__)


@dataclass
class DiscoveredSnooz:
    ble_device: BLEDevice
    advertisement_data: AdvertisementData
    snooz_adv: SnoozAdvertisementData
    display_name: str


def _parse_firmware_flags(flags: int) -> tuple[Optional[SnoozFirmwareVersion], bool]:
    """
    Mirrors pysnooz.advertisement.parse_firmware_flags but without home_assistant_bluetooth dependency.
    """
    is_pairing = (FIRMWARE_PAIRING_FLAGS & flags) == FIRMWARE_PAIRING_FLAGS
    flags_without_pairing = flags & ~FIRMWARE_PAIRING_FLAGS

    fw = FIRMWARE_VERSION_BY_FLAGS.get(flags_without_pairing)
    return fw, is_pairing


def parse_snooz_advertisement_from_bleak(
    name: str,
    adv: AdvertisementData,
    password_hex: str,
) -> Optional[SnoozAdvertisementData]:
    """
    Build SnoozAdvertisementData using Bleak AdvertisementData.
    We *always* inject the configured password (even if device is not in pairing mode).
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

    fw, _is_pairing = _parse_firmware_flags(payload[0])
    if fw is None:
        return None

    # Model inference: pysnooz uses name + firmware. Keep it simple:
    # - Breez advertising name usually starts with "Breez"
    # - otherwise treat as Snooz/Pro-family
    lower = (name or "").lower()
    if lower.startswith("breez"):
        model = SnoozDeviceModel.BREEZ
    elif lower.startswith("snooz"):
        # newer Snooz (v6+) usually reported as PRO in pysnooz; older = ORIGINAL
        if fw in (
            SnoOZ_FIRMWARE_VERSIONS := (
                SnoozFirmwareVersion.V2,
                SnoozFirmwareVersion.V3,
                SnoozFirmwareVersion.V4,
                SnoozFirmwareVersion.V5,
            )
        ):
            model = SnoozDeviceModel.ORIGINAL
        else:
            model = SnoozDeviceModel.PRO
    else:
        # unknown name; best-effort
        model = SnoozDeviceModel.PRO if fw == SnoozFirmwareVersion.V6 else SnoozDeviceModel.UNSUPPORTED

    if model == SnoozDeviceModel.UNSUPPORTED:
        return None

    return SnoozAdvertisementData(
        model=model,
        firmware_version=fw,
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

    def bind_discovery(self, ble_device: BLEDevice, adv: AdvertisementData) -> bool:
        name = adv.local_name or ble_device.name or "Snooz"
        snooz_adv = parse_snooz_advertisement_from_bleak(name=name, adv=adv, password_hex=self.password_hex)
        if snooz_adv is None:
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

    async def start(self) -> None:
        """
        Ensure the device is connected enough to fetch info (also tends to “warm up” state).
        """
        if not self._device:
            raise RuntimeError(f"[{self.device_name}] Device not discovered/bound yet")

        try:
            info = await self._device.async_get_info()
            if info:
                _LOGGER.info("[%s] Connected: model=%s firmware=%s", self.device_name, info.model, info.firmware)
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
                        _LOGGER.debug("[%s] state callback error", self.device_name, exc_info=True)

        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
        self._event_task = asyncio.create_task(_emit())
