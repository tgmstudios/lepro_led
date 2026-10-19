"""Runtime hub: device registry, state store, command send with rollback, listeners."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.exceptions import HomeAssistantError

from .state import DeviceState
from .transport_mqtt import TransportError

_LOGGER = logging.getLogger(__name__)

STATE_KEYS = ["d1", "d2", "d3", "d4", "d5", "d30", "d50", "d52", "d60", "online"]
REPORT_TOPICS = {"rpt", "getr", "set"}  # "set" kept for legacy behaviour (device-side echo)


@dataclass
class DeviceCtx:
    did: str
    info: dict
    state: DeviceState = field(default_factory=DeviceState)
    listeners: list = field(default_factory=list)

    def __post_init__(self):
        seed = {k: self.info[k] for k in ("d2", "d50", "d52", "d60") if k in self.info}
        self.state.apply_report(seed)
        if self.info.get("switch") in (0, 1, True, False):
            self.state.on = bool(self.info["switch"])


class LeproRuntime:
    def __init__(self, devices: list[dict]):
        self.devices: dict[str, DeviceCtx] = {str(d["did"]): DeviceCtx(str(d["did"]), d) for d in devices}
        self.transport = None
        self._tasks: set[asyncio.Task] = set()

    # ---- listeners ---------------------------------------------------------
    def add_listener(self, did: str, cb: Callable[[], None]) -> Callable[[], None]:
        ctx = self.devices[did]
        ctx.listeners.append(cb)

        def remove():
            if cb in ctx.listeners:
                ctx.listeners.remove(cb)

        return remove

    def _notify(self, did: str) -> None:
        for cb in list(self.devices[did].listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("listener failed")

    # ---- availability ------------------------------------------------------
    def available(self, did: str) -> bool:
        ctx = self.devices.get(did)
        if ctx is None or self.transport is None or not self.transport.connected:
            return False
        return ctx.state.online is not False

    # ---- inbound -----------------------------------------------------------
    def handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "le" or parts[2] != "prp" or parts[3] not in REPORT_TOPICS:
            return
        ctx = self.devices.get(parts[1])
        if ctx is None:
            return
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            _LOGGER.debug("ignoring non-JSON payload on %s", topic)
            return
        data = body.get("d") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return
        if ctx.state.apply_report(data):
            self._notify(ctx.did)

    def handle_status(self, up: bool) -> None:
        if up:
            for did in self.devices:
                self._spawn(self.request_state(did))
        for did in self.devices:
            self._notify(did)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- outbound ----------------------------------------------------------
    async def _publish(self, did: str, suffix: str, body: dict) -> None:
        await self.transport.publish(f"le/{did}/prp/{suffix}", json.dumps(body))

    async def request_state(self, did: str) -> None:
        try:
            await self._publish(did, "get", {"d": STATE_KEYS})
        except TransportError as err:
            _LOGGER.debug("state request for %s failed: %s", did, err)

    async def send(self, did: str, build: Callable[[DeviceState], dict | None]) -> None:
        """Apply `build` to the device state, publish the DP map, roll back on failure."""
        ctx = self.devices[did]
        snapshot = copy.deepcopy(ctx.state)
        try:
            dps = build(ctx.state)
        except ValueError as err:
            ctx.state = snapshot
            raise HomeAssistantError(str(err)) from err
        try:
            if dps:
                await self._publish(
                    did, "set", {"id": random.randint(0, 1_000_000_000), "t": int(time.time()), "d": dps}
                )
        except TransportError as err:
            ctx.state = snapshot
            self._notify(did)
            raise HomeAssistantError(f"Lepro device {did} unreachable: {err}") from err
        self._notify(did)

    # ---- lifecycle -----------------------------------------------------------
    async def async_shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.transport is not None:
            await self.transport.stop() if hasattr(self.transport, "stop") else None
