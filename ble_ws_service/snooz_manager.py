from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from enum import Enum

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from pysnooz.commands import SnoozCommandResult, SnoozCommandResultStatus
from pysnooz import (
    set_volume,
    turn_on,
    turn_off,
    turn_light_on,
    turn_light_off,
    set_light_brightness,
)

from ble_snooz import BleSnooz

_LOGGER = logging.getLogger(__name__)

EventListener = Callable[[dict], Awaitable[None]]


class SnoozManager:
    """
    Owns:
      - BleSnooz instances
      - scanning for configured devices
      - event listeners (e.g. WebSocket server)

    Uses a global BLE op semaphore to avoid overlapping BLE commands across devices,
    mirroring your August design. :contentReference[oaicite:3]{index=3}
    """

    RESCAN_INTERVAL_SECONDS = 30.0

    def __init__(self) -> None:
        self._devices: Dict[str, BleSnooz] = {}
        self._event_listeners: List[EventListener] = []

        self._ble_op_sem = asyncio.Semaphore(1)

        self._running = False
        self._rescan_task: Optional[asyncio.Task] = None

    # ----------------------------
    # registration
    # ----------------------------

    def add_device(self, dev: BleSnooz) -> None:
        if dev.device_name in self._devices:
            raise ValueError(f"Duplicate device_name: {dev.device_name}")
        self._devices[dev.device_name] = dev
        dev.register_state_callback(self._broadcast_event)

    def register_event_listener(self, listener: EventListener) -> None:
        self._event_listeners.append(listener)

    # ----------------------------
    # accessors
    # ----------------------------

    def get_device_names(self) -> List[str]:
        return list(self._devices.keys())

    def get_device(self, device_name: str) -> BleSnooz:
        if device_name not in self._devices:
            raise KeyError(f"Unknown device_name: {device_name}")
        return self._devices[device_name]

    # ----------------------------
    # lifecycle
    # ----------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        await self._initial_discovery_and_connect()

        self._rescan_task = asyncio.create_task(self._rescan_loop())

    async def stop(self) -> None:
        self._running = False

        if self._rescan_task and not self._rescan_task.done():
            self._rescan_task.cancel()

        for dev in self._devices.values():
            await dev.stop()

    async def _initial_discovery_and_connect(self) -> None:
        target_names = set(self._devices.keys())
        found = await self._scan_for_targets(target_names, timeout=12.0)

        for devname, (ble_dev, adv) in found.items():
            dev = self._devices[devname]
            if dev.bind_discovery(ble_dev, adv):
                try:
                    await dev.start()
                except Exception:
                    _LOGGER.exception("[%s] Start/connect failed", devname)

        missing = sorted(target_names - set(found.keys()))
        if missing:
            _LOGGER.warning("Some devices not discovered at startup: %s", missing)

    async def _rescan_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.RESCAN_INTERVAL_SECONDS)

                missing_names = {name for name, dev in self._devices.items() if not dev.is_ready()}
                if not missing_names:
                    continue

                _LOGGER.info("Rescanning for missing devices: %s", sorted(missing_names))
                found = await self._scan_for_targets(missing_names, timeout=8.0)

                for devname, (ble_dev, adv) in found.items():
                    dev = self._devices[devname]
                    if not dev.is_ready() and dev.bind_discovery(ble_dev, adv):
                        try:
                            await dev.start()
                        except Exception:
                            _LOGGER.exception("[%s] Start/connect failed after rescan", devname)

            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Rescan loop error")

    async def _scan_for_targets(
            self,
            device_names: Set[str],
            timeout: float,
    ) -> Dict[str, Tuple[BLEDevice, AdvertisementData]]:
        """
        Scan until we find all target devices (by address OR advertised name), or timeout.

        Matching rules per device:
          - if dev.address is set: match BLEDevice.address (Linux MAC or macOS UUID)
          - if dev.match_name is set: match AdvertisementData.local_name / BLEDevice.name
        """
        targets = set(device_names)
        found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}
        done = asyncio.Event()

        # Build lookup maps for quick matching
        addr_to_device_name: Dict[str, str] = {}
        name_to_device_name: Dict[str, str] = {}

        for target_device_name in targets:
            dev = self._devices[target_device_name]

            addr = getattr(dev, "address", "") or ""
            if addr:
                addr_to_device_name[addr.upper()] = target_device_name

            match_name = getattr(dev, "match_name", None)
            if match_name:
                name_to_device_name[match_name.strip().lower()] = target_device_name

        def cb(device: BLEDevice, adv: AdvertisementData) -> None:
            addr = (device.address or "").upper()
            adv_name = (adv.local_name or device.name or "").strip()
            adv_name_l = adv_name.lower()

            matched_device_name: Optional[str] = None

            if addr and addr in addr_to_device_name:
                matched_device_name = addr_to_device_name[addr]
            elif adv_name_l and adv_name_l in name_to_device_name:
                matched_device_name = name_to_device_name[adv_name_l]

            if matched_device_name and matched_device_name not in found:
                found[matched_device_name] = (device, adv)
                if targets.issubset(found.keys()):
                    done.set()

        scanner = BleakScanner(detection_callback=cb)
        await scanner.start()
        try:
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            await scanner.stop()

        return found

    # ----------------------------
    # broadcasting
    # ----------------------------

    async def _broadcast_event(self, event: dict) -> None:
        if not self._event_listeners:
            return
        coros = [listener(event) for listener in list(self._event_listeners)]
        await asyncio.gather(*coros, return_exceptions=True)

    # ----------------------------
    # BLE op serialization helper
    # ----------------------------

    async def _with_ble_lock(self, device_name: str, op_name: str, coro_factory):
        async with self._ble_op_sem:
            _LOGGER.debug("[%s] BLE op start: %s", device_name, op_name)
            try:
                return await coro_factory()
            finally:
                _LOGGER.debug("[%s] BLE op end: %s", device_name, op_name)

    # ----------------------------
    # commands (volume/noise/light)
    # ----------------------------

    async def cmd_get_state(self, device_name: str) -> dict:
        dev = self.get_device(device_name)
        return dev.snapshot()

    async def cmd_noise_on(self, device_name: str, volume: Optional[int] = None) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")

        async def _do():
            assert dev._device is not None
            return await dev._device.async_execute_command(turn_on(volume=volume))

        return await self._with_ble_lock(device_name, "noise_on", _do)

    async def cmd_noise_off(self, device_name: str, duration_s: Optional[float] = None) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")

        async def _do():
            assert dev._device is not None
            if duration_s is None:
                return await dev._device.async_execute_command(turn_off())
            return await dev._device.async_execute_command(turn_off(duration=self._sec_to_timedelta(duration_s)))

        return await self._with_ble_lock(device_name, "noise_off", _do)

    async def cmd_set_volume(self, device_name: str, volume: int) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")
        if not (0 <= volume <= 100):
            raise ValueError("volume must be 0..100")

        async def _do():
            assert dev._device is not None
            return await dev._device.async_execute_command(set_volume(volume))

        return await self._with_ble_lock(device_name, "set_volume", _do)

    async def cmd_light_on(self, device_name: str) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")

        async def _do():
            assert dev._device is not None
            return await dev._device.async_execute_command(turn_light_on())

        return await self._with_ble_lock(device_name, "light_on", _do)

    async def cmd_light_off(self, device_name: str) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")

        async def _do():
            assert dev._device is not None
            return await dev._device.async_execute_command(turn_light_off())

        return await self._with_ble_lock(device_name, "light_off", _do)

    async def cmd_set_light_brightness(self, device_name: str, brightness: int) -> SnoozCommandResult:
        dev = self.get_device(device_name)
        if not dev.is_ready():
            raise RuntimeError("device_unavailable")
        if not (0 <= brightness <= 100):
            raise ValueError("brightness must be 0..100")

        async def _do():
            assert dev._device is not None
            return await dev._device.async_execute_command(set_light_brightness(brightness))

        return await self._with_ble_lock(device_name, "set_light_brightness", _do)

    # ----------------------------
    # result formatting
    # ----------------------------

    @staticmethod
    def result_to_dict(result: SnoozCommandResult) -> dict:
        def to_jsonable(obj: Any) -> Any:
            if obj is None:
                return None
            if isinstance(obj, timedelta):
                return obj.total_seconds()
            if isinstance(obj, Enum):
                return obj.name
            if is_dataclass(obj):
                return {k: to_jsonable(v) for k, v in asdict(obj).items()}
            if isinstance(obj, dict):
                return {k: to_jsonable(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [to_jsonable(v) for v in obj]
            return obj

        duration = getattr(result, "duration", None)
        response = getattr(result, "response", None)

        return {
            "status": getattr(result.status, "name", str(result.status)),
            "duration_s": to_jsonable(duration),  # timedelta -> seconds
            "response": to_jsonable(response),  # recursively JSON-safe
        }

    @staticmethod
    def ensure_success(result: SnoozCommandResult) -> None:
        if result.status != SnoozCommandResultStatus.SUCCESSFUL:
            raise RuntimeError(f"command_failed: {result.status.name}")

    @staticmethod
    def _sec_to_timedelta(seconds: Optional[float]) -> Optional[timedelta]:
        return None if seconds is None else timedelta(seconds=float(seconds))