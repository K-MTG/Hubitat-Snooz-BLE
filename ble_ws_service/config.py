from __future__ import annotations

import yaml

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class SnoozDeviceConfig:
    device_name: str                  # unique identifier used in WS commands
    password: str                     # 16 hex chars
    address: Optional[str] = None     # Linux MAC or macOS UUID
    name: Optional[str] = None        # advertised name (recommended on macOS)


@dataclass
class WebSocketConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    auth_token: Optional[str] = None  # optional bearer token


@dataclass
class ServiceConfig:
    websocket: WebSocketConfig
    devices: List[SnoozDeviceConfig]


def _normalize_hex_password(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip().lower() if ch in "0123456789abcdef")
    if len(cleaned) != 16:
        raise ValueError(f"Invalid Snooz password hex length: expected 16 hex chars, got {len(cleaned)}")
    return cleaned


def load_config(path: str | Path) -> ServiceConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    service = raw.get("service", {})

    ws_raw = service.get("websocket", {})
    websocket = WebSocketConfig(
        host=ws_raw.get("host", "0.0.0.0"),
        port=int(ws_raw.get("port", 8765)),
        auth_token=ws_raw.get("auth_token"),
    )

    devices: List[SnoozDeviceConfig] = []
    for entry in service.get("devices", []):
        devices.append(
            SnoozDeviceConfig(
                device_name=str(entry["device_name"]),
                address=str(entry.get("address")).upper() if entry.get("address") else None,
                name=str(entry.get("name")) if entry.get("name") else None,
                password=_normalize_hex_password(str(entry["password"])),
            )
        )

    if not devices:
        raise ValueError("No devices configured (service.devices is empty)")

    # Ensure each device has at least one matcher
    for d in devices:
        if not d.address and not d.name:
            raise ValueError(f"Device '{d.device_name}' must specify either 'address' or 'name'")

    # Ensure unique names
    names = [d.device_name for d in devices]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate device_name found in config")

    return ServiceConfig(websocket=websocket, devices=devices)
