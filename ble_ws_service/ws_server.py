from __future__ import annotations

import asyncio
import json
import logging
import time
from http import HTTPStatus
from typing import Dict, Optional, Set

from websockets.asyncio.server import serve, Server, ServerConnection
from websockets.exceptions import ConnectionClosed
from websockets.server import Request

from snooz_manager import SnoozManager

_LOGGER = logging.getLogger(__name__)


class WebSocketServer:
    """
    WebSocket API for Snooz devices.
    Mirrors your August WS server patterns (auth + protocol shapes). :contentReference[oaicite:4]{index=4}

    Auth:
      - If configured, require: Authorization: Bearer <token>

    Protocol (JSON):

    From client:
      {
        "type": "command",
        "request_id": "abc123",
        "command": "...",
        "device_name": "bedroom",
        ... command args ...
      }

    Commands:
      - heartbeat
      - list_devices
      - get_state (device_name required)
      - noise_on (device_name, volume? int)
      - noise_off (device_name, duration_s? float)
      - set_volume (device_name, volume int)
      - light_on/light_off (device_name)
      - set_light_brightness (device_name, brightness int)

    Responses:
      {
        "type": "response",
        "request_id": "abc123",
        "status": "ok" | "error",
        "data": {...},
        "error": "message"
      }

    Events (broadcast):
      {
        "type": "event",
        "event": "device_state",
        "device_name": "bedroom",
        "state": {...snapshot...}
      }
    """

    def __init__(self, manager: SnoozManager, host: str, port: int, auth_token: Optional[str] = None) -> None:
        self._manager = manager
        self._host = host
        self._port = port
        self._auth_token = auth_token

        self._clients: Set[ServerConnection] = set()
        self._server: Optional[Server] = None

        self._manager.register_event_listener(self._handle_manager_event)

    async def _process_request(self, _connection: ServerConnection, request: Request):
        if not self._auth_token:
            return None

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return (
                HTTPStatus.UNAUTHORIZED,
                [("WWW-Authenticate", "Bearer")],
                b"Missing Authorization header\n",
            )

        token = auth_header.removeprefix("Bearer ").strip()
        if token != self._auth_token:
            return (HTTPStatus.FORBIDDEN, [], b"Invalid auth token\n")

        return None

    async def start(self) -> None:
        self._server = await serve(
            self._handler,
            self._host,
            self._port,
            process_request=self._process_request,
            ping_interval=30,
            ping_timeout=10,
        )
        _LOGGER.info("WebSocket server listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        coros = [self._safe_close(ws) for ws in list(self._clients)]
        await asyncio.gather(*coros, return_exceptions=True)

    async def _handler(self, websocket: ServerConnection):
        self._clients.add(websocket)
        _LOGGER.info("Client connected (%d total)", len(self._clients))

        try:
            async for raw in websocket:
                await self._handle_message(websocket, raw)
        except ConnectionClosed:
            pass
        except Exception:
            _LOGGER.exception("Client handler error")
        finally:
            self._clients.discard(websocket)
            _LOGGER.info("Client disconnected (%d total)", len(self._clients))

    async def _handle_message(self, websocket: ServerConnection, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error(websocket, request_id=None, error="invalid_json")
            return

        msg_type = msg.get("type")
        request_id = msg.get("request_id")

        if msg_type != "command":
            await self._send_error(websocket, request_id=request_id, error="type_must_be_command")
            return

        command = msg.get("command")
        device_name = msg.get("device_name")

        try:
            if command == "heartbeat":
                await self._send_ok(websocket, request_id=request_id, data={"server_time": time.time()})
                return

            if command == "list_devices":
                await self._send_ok(websocket, request_id=request_id, data={"devices": self._manager.get_device_names()})
                return

            if command in (
                "get_state",
                "noise_on",
                "noise_off",
                "set_volume",
                "light_on",
                "light_off",
                "set_light_brightness",
            ) and not device_name:
                raise ValueError("device_name is required")

            # --- get_state
            if command == "get_state":
                snap = await self._manager.cmd_get_state(device_name)
                await self._send_ok(websocket, request_id=request_id, data=snap)
                return

            # --- noise/volume
            if command == "noise_on":
                volume = msg.get("volume")
                result = await self._manager.cmd_noise_on(device_name, volume=volume)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            if command == "noise_off":
                duration_s = msg.get("duration_s")
                result = await self._manager.cmd_noise_off(device_name, duration_s=duration_s)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            if command == "set_volume":
                volume = int(msg["volume"])
                result = await self._manager.cmd_set_volume(device_name, volume=volume)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            # --- light
            if command == "light_on":
                result = await self._manager.cmd_light_on(device_name)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            if command == "light_off":
                result = await self._manager.cmd_light_off(device_name)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            if command == "set_light_brightness":
                brightness = int(msg["brightness"])
                result = await self._manager.cmd_set_light_brightness(device_name, brightness=brightness)
                self._manager.ensure_success(result)
                await self._send_ok(websocket, request_id=request_id, data=self._manager.result_to_dict(result))
                return

            raise ValueError(f"unknown_command: {command}")

        except Exception as exc:
            _LOGGER.exception("Error handling command: %s", msg)
            await self._send_error(websocket, request_id=request_id, error=str(exc))

    async def _handle_manager_event(self, event: dict) -> None:
        """
        Manager event shape: {"type":"device_state","device_name":"...","state":{...}}
        Broadcast to all clients, same pattern as your August code. :contentReference[oaicite:5]{index=5}
        """
        if not self._clients:
            return

        payload = {
            "type": "event",
            "event": "device_state",
            "device_name": event["device_name"],
            "state": event["state"],
        }
        msg = json.dumps(payload)
        coros = [self._safe_send(ws, msg) for ws in list(self._clients)]
        await asyncio.gather(*coros, return_exceptions=True)

    async def _send_ok(self, websocket: ServerConnection, request_id: Optional[str], data: Dict) -> None:
        response = {"type": "response", "request_id": request_id, "status": "ok", "data": data}
        await self._safe_send(websocket, json.dumps(response))

    async def _send_error(self, websocket: ServerConnection, request_id: Optional[str], error: str) -> None:
        response = {"type": "response", "request_id": request_id, "status": "error", "error": error}
        await self._safe_send(websocket, json.dumps(response))

    async def _safe_send(self, ws: ServerConnection, msg: str) -> None:
        try:
            await ws.send(msg)
        except ConnectionClosed:
            self._clients.discard(ws)
        except Exception:
            _LOGGER.debug("Error sending to client", exc_info=True)
            self._clients.discard(ws)

    async def _safe_close(self, ws: ServerConnection, code: int = 1001, reason: str = "Server shutting down") -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
