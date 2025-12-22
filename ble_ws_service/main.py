from __future__ import annotations

import asyncio
import logging
import signal

from version import __version__
from config import load_config
from ble_snooz import BleSnooz
from snooz_manager import SnoozManager
from ws_server import WebSocketServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("snooz_ble_ws_service")


async def main() -> None:
    cfg = load_config("config.yaml")

    manager = SnoozManager()

    for dc in cfg.devices:
        dev = BleSnooz(
            device_name=dc.device_name,
            address=dc.address or "",
            password_hex=dc.password,
            match_name=dc.name,
        )
        manager.add_device(dev)

    ws = WebSocketServer(
        manager=manager,
        host=cfg.websocket.host,
        port=cfg.websocket.port,
        auth_token=cfg.websocket.auth_token,
    )

    await manager.start()
    await ws.start()

    stop_event = asyncio.Event()

    def _handle_signal(signame):
        _LOGGER.info("Received signal %s: shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            pass

    await stop_event.wait()

    await ws.stop()
    await manager.stop()


if __name__ == "__main__":
    _LOGGER.info("Starting Snooz BLE WS Service version %s", __version__)
    asyncio.run(main())
