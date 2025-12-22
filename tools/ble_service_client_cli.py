#!/usr/bin/env python3
"""
Interactive CLI client for Snooz BLE WebSocket API.

Example:
  python client_cli.py ws://10.0.3.13:8765 --token ws-shared-secret
"""

import argparse
import asyncio
import json
import logging
import shlex
import uuid
from pprint import pprint
from typing import Any, Callable, Dict, Optional

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

_LOGGER = logging.getLogger(__name__)


class SnoozWebSocketClient:
    def __init__(
        self,
        url: str,
        event_callback: Optional[Callable[[dict], Any]] = None,
        reconnect_delay: float = 5.0,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.url = url
        self.event_callback = event_callback
        self.reconnect_delay = reconnect_delay
        self.headers = headers or {}

        self._pending: Dict[str, asyncio.Future] = {}
        self._ws = None
        self._listener_task = None
        self._running = False
        self._connected_event = asyncio.Event()

    async def start(self):
        if self._running:
            return

        self._running = True
        self._listener_task = asyncio.create_task(self._run_forever())

        print("Connecting to WebSocket server...")
        await self._connected_event.wait()
        print("Connected!\n")

    async def stop(self):
        self._running = False
        self._connected_event.clear()

        if self._ws:
            await self._ws.close()

        if self._listener_task:
            await self._listener_task

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()

    # ---- API calls ----

    async def list_devices(self):
        return await self._send_command("list_devices")

    async def get_state(self, device_name: str):
        return await self._send_command("get_state", device_name=device_name)

    async def noise_on(self, device_name: str, volume: Optional[int] = None):
        data = {"volume": volume} if volume is not None else {}
        return await self._send_command("noise_on", device_name=device_name, **data)

    async def noise_off(self, device_name: str, duration_s: Optional[float] = None):
        data = {"duration_s": duration_s} if duration_s is not None else {}
        return await self._send_command("noise_off", device_name=device_name, **data)

    async def set_volume(self, device_name: str, volume: int):
        data = {"volume": volume}
        return await self._send_command("set_volume", device_name=device_name, **data)

    async def light_on(self, device_name: str):
        return await self._send_command("light_on", device_name=device_name)

    async def light_off(self, device_name: str):
        return await self._send_command("light_off", device_name=device_name)

    async def set_light_brightness(self, device_name: str, brightness: int):
        return await self._send_command("set_light_brightness", device_name=device_name, brightness=brightness)

    # ---- connection loop ----

    async def _run_forever(self):
        while self._running:
            try:
                _LOGGER.info("Connecting to %s", self.url)
                async with connect(self.url, additional_headers=self.headers) as ws:
                    self._ws = ws
                    _LOGGER.info("Connected")
                    self._connected_event.set()
                    await self._listen()
            except Exception:
                _LOGGER.exception("WebSocket connection error")

            self._connected_event.clear()

            if self._running:
                _LOGGER.warning("Reconnecting in %.1f seconds...", self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

        _LOGGER.info("Client fully stopped")

    async def _listen(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "response":
                    req_id = msg.get("request_id")
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        if msg["status"] == "ok":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(Exception(msg.get("error")))
                    continue

                if msg_type == "event":
                    if self.event_callback:
                        asyncio.create_task(self.event_callback(msg))
                    continue

                _LOGGER.warning("Unknown message type received: %s", msg)

        except ConnectionClosed:
            _LOGGER.warning("WebSocket disconnected")
        except Exception:
            _LOGGER.exception("Listener failure")

    async def _send_command(self, command: str, device_name: Optional[str] = None, **kwargs):
        await self._connected_event.wait()

        request_id = uuid.uuid4().hex
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        payload = {
            "type": "command",
            "request_id": request_id,
            "command": command,
        }
        if device_name:
            payload["device_name"] = device_name
        payload.update(kwargs)

        await self._ws.send(json.dumps(payload))
        return await future


BANNER = r"""
=========================================================
   Snooz BLE WebSocket Interactive Shell
   Type "help" for commands. Press Ctrl+C to quit.
=========================================================
"""

HELP_TEXT = """
Available commands:

  list
  state <device_name>

  on <device_name> [volume]
  off <device_name> [duration_s]
  volume <device_name> <0-100>

  light on <device_name>
  light off <device_name>
  light brightness <device_name> <0-100>
Other:
  help
  quit / exit
"""


class InteractiveShell:
    def __init__(self, url: str, auth_token: Optional[str] = None):
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self.client = SnoozWebSocketClient(
            url=url,
            event_callback=self._on_event,
            headers=headers,
        )
        self._running = True

    async def start(self):
        print(BANNER)
        await self.client.start()
        await self._repl()

    async def _on_event(self, event: dict):
        print("\n🔔 EVENT RECEIVED:")
        pprint(event)
        print("> ", end="", flush=True)

    async def _repl(self):
        while self._running:
            try:
                line = await asyncio.to_thread(input, "> ")
                line = line.strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting...")
                break

            if not line:
                continue

            try:
                parts = shlex.split(line)
            except ValueError as e:
                print(f"❌ Parse error: {e}")
                continue

            cmd = parts[0].lower()

            try:
                if cmd in ("quit", "exit"):
                    print("Shutting down...")
                    await self.client.stop()
                    break

                if cmd == "help":
                    print(HELP_TEXT)
                    continue

                if cmd == "list":
                    pprint(await self.client.list_devices())
                    continue

                if cmd == "state":
                    pprint(await self.client.get_state(parts[1]))
                    continue

                if cmd == "on":
                    device = parts[1]
                    volume = int(parts[2]) if len(parts) >= 3 else None
                    pprint(await self.client.noise_on(device, volume=volume))
                    continue

                if cmd == "off":
                    device = parts[1]
                    dur = float(parts[2]) if len(parts) >= 3 else None
                    pprint(await self.client.noise_off(device, duration_s=dur))
                    continue

                if cmd == "volume":
                    device = parts[1]
                    vol = int(parts[2])
                    pprint(await self.client.set_volume(device, vol))
                    continue

                if cmd == "light":
                    sub = parts[1].lower()
                    device = parts[2]
                    if sub == "on":
                        pprint(await self.client.light_on(device))
                        continue
                    if sub == "off":
                        pprint(await self.client.light_off(device))
                        continue
                    if sub == "brightness":
                        brightness = int(parts[3])
                        pprint(await self.client.set_light_brightness(device, brightness))
                        continue
                    print("Usage: light on|off|brightness <device> [value]")
                    continue

                print("Unknown command. Type 'help'.")

            except Exception as e:
                print(f"❌ Error: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Snooz BLE WS CLI client")
    parser.add_argument("url", help="WebSocket URL (e.g. ws://host:8765)")
    parser.add_argument("--token", "-t", dest="auth_token", default=None, help="Bearer token for WS auth")
    return parser.parse_args()


async def main():
    args = parse_args()
    shell = InteractiveShell(url=args.url, auth_token=args.auth_token)
    await shell.start()


if __name__ == "__main__":
    asyncio.run(main())
