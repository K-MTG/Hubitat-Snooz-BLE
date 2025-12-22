#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from typing import Dict, Optional, Tuple

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from pysnooz.const import FIRMWARE_PAIRING_FLAGS, FIRMWARE_VERSION_BY_FLAGS, SNOOZ_ADVERTISEMENT_LENGTH


def parse_password_if_pairing(adv: AdvertisementData) -> Optional[str]:
    mfg = adv.manufacturer_data or {}
    payload: Optional[bytes] = None
    if 0xFFFF in mfg:
        payload = mfg[0xFFFF]
    elif len(mfg) > 0:
        payload = next(iter(mfg.values()))

    if not payload or len(payload) != SNOOZ_ADVERTISEMENT_LENGTH:
        return None

    flags = payload[0]
    is_pairing = (FIRMWARE_PAIRING_FLAGS & flags) == FIRMWARE_PAIRING_FLAGS
    if not is_pairing:
        return None

    # password is the remaining bytes
    return payload[1:].hex()


def parse_fw(adv: AdvertisementData) -> Optional[str]:
    mfg = adv.manufacturer_data or {}
    payload: Optional[bytes] = None
    if 0xFFFF in mfg:
        payload = mfg[0xFFFF]
    elif len(mfg) > 0:
        payload = next(iter(mfg.values()))
    if not payload or len(payload) != SNOOZ_ADVERTISEMENT_LENGTH:
        return None
    flags_without_pairing = payload[0] & ~FIRMWARE_PAIRING_FLAGS
    fw = FIRMWARE_VERSION_BY_FLAGS.get(flags_without_pairing)
    return fw.name if fw else None


async def scan(timeout: float, match_mac: Optional[str]) -> Dict[str, Tuple[BLEDevice, AdvertisementData]]:
    found: Dict[str, Tuple[BLEDevice, AdvertisementData]] = {}

    def cb(device: BLEDevice, adv: AdvertisementData) -> None:
        addr = device.address.upper()
        if match_mac and addr != match_mac:
            return

        name = adv.local_name or device.name or ""
        # filter lightly: if it has the right manufacturer payload, keep it
        pw = parse_password_if_pairing(adv)
        fw = parse_fw(adv)

        if pw or fw or name.lower().startswith(("snooz", "breez")):
            found[addr] = (device, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    return found


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Discover Snooz/Breez devices + pairing password")
    p.add_argument("--timeout", type=float, default=10.0, help="scan seconds (default 10)")
    p.add_argument("--mac", type=str, default=None, help="filter to a specific MAC")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    mac = args.mac.upper() if args.mac else None

    results = await scan(timeout=args.timeout, match_mac=mac)
    if not results:
        print("No matching Snooz/Breez devices found.")
        print("Tip: put the device into pairing mode to see the password in advertisements.")
        return

    for addr, (dev, adv) in results.items():
        name = adv.local_name or dev.name or "(unknown)"
        rssi = getattr(adv, "rssi", None) or getattr(dev, "rssi", None)
        fw = parse_fw(adv)
        pw = parse_password_if_pairing(adv)

        print("------------------------------------------------------------")
        print(f"Name: {name}")
        print(f"MAC : {addr}")
        print(f"RSSI: {rssi}")
        print(f"FW  : {fw}")
        print(f"Pairing password (hex): {pw or '(not in pairing mode)'}")
        print()
        if pw:
            print("config.yaml snippet:")
            print(f"  - device_name: {name.lower().replace(' ', '_')}")
            print(f"    address: \"{addr}\"")
            print(f"    password: \"{pw}\"")

    print("------------------------------------------------------------")


if __name__ == "__main__":
    asyncio.run(main())
