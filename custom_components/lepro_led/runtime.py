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

from .api import CloudScene, LeproApiError, LeproAuthError
from .state import DeviceState
from .transport_mqtt import TransportError

_LOGGER = logging.getLogger(__name__)

STATE_KEYS = ["d1", "d2", "d3", "d4", "d5", "d30", "d50", "d52", "d60", "online"]
# Only topics that carry device-originated state. "set" is our own outgoing command topic (the broker
# would echo it back to us) and "setr" acks have no verified format, so neither confirms anything.
SCENE_FETCH_CONCURRENCY = 2
REPORT_TOPICS = frozenset({"rpt", "getr"})


@dataclass
class DeviceCtx:
    did: str
    info: dict
    state: DeviceState = field(default_factory=DeviceState)
    listeners: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialises commands per device
    report_seq: int = 0  # bumped for every applied device report
    field_seq: dict = field(default_factory=dict)  # state attribute -> report_seq that last wrote it
    pending: set = field(default_factory=set)  # published to broker, not yet reported back by the device

    def __post_init__(self):
        seed = {k: self.info[k] for k in ("d2", "d50", "d52", "d60") if k in self.info}
        self.state.apply_report(seed)
        if self.info.get("switch") in (0, 1, True, False):
            self.state.on = bool(self.info["switch"])


class LeproRuntime:
    def __init__(self, devices: list[dict]):
        self.devices: dict[str, DeviceCtx] = {str(d["did"]): DeviceCtx(str(d["did"]), d) for d in devices}
        self.transport = None
        self.api = None  # LeproApi, set at setup; used only for read-only scene GETs
        self.token: str | None = None
        self.scenes: dict[str, dict[str, CloudScene]] = {did: {} for did in self.devices}  # did -> scene_id -> scene
        self._tasks: set[asyncio.Task] = set()
        self._refreshing: dict[str, asyncio.Task] = {}
        self._auth_lock = asyncio.Lock()
        self._scene_sem = asyncio.Semaphore(SCENE_FETCH_CONCURRENCY)
        self._closed = False

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
        if len(parts) != 4 or parts[0] != "le" or parts[2] != "prp" or parts[3] not in REPORT_TOPICS:
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
            ctx.report_seq += 1
            for name in ctx.state.touched:
                ctx.field_seq[name] = ctx.report_seq
            ctx.pending -= ctx.state.touched  # the device itself reported these fields
            self._notify(ctx.did)

    def pending_confirmation(self, did: str) -> frozenset:
        """State fields published to the broker but not yet reported back by the device."""
        return frozenset(self.devices[did].pending)

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
        """Build a DP map from a copy of the state, publish it, then commit the local desired values.

        The authoritative state is never mutated before the publish, so a failed publish needs no
        rollback and cannot discard device reports that arrive meanwhile. After a successful publish
        only fields the command changed *and* no device report touched during the publish are
        committed; those are marked pending until the device reports them. Success means
        "published to the broker", not "device applied it".
        """
        ctx = self.devices[did]
        async with ctx.lock:
            desired = copy.deepcopy(ctx.state)
            try:
                dps = build(desired)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            current = vars(ctx.state)
            changed = {k for k, v in vars(desired).items() if k != "touched" and current.get(k) != v}
            if dps and "d50" in dps:
                changed.add("d50_str")  # a d50 command is always a claim, even if the cached value already matches
            seq = ctx.report_seq
            if dps:
                try:
                    await self._publish(
                        did, "set", {"id": random.randint(0, 1_000_000_000), "t": int(time.time()), "d": dps}
                    )
                except TransportError as err:
                    raise HomeAssistantError(f"Lepro device {did} unreachable: {err}") from err
            committed = {k for k in changed if ctx.field_seq.get(k, 0) <= seq}
            for k in committed:
                setattr(ctx.state, k, getattr(desired, k))
            if dps:
                ctx.pending |= committed
        self._notify(did)

    # ---- cloud scene catalog ---------------------------------------------------
    def set_scenes(self, did: str, scenes: list[CloudScene]) -> None:
        ctx = self.devices[did]
        self.scenes[did] = {sc.scene_id: sc for sc in scenes}
        ctx.state.cloud_scenes = {sc.d50: sc.label for sc in scenes}
        ctx.state.reevaluate_cloud()
        self._notify(did)

    async def refresh_scenes(self, did: str) -> int:
        """Read the strip's cloud scenes (GET only). Raises LeproApiError; keeps the old catalog on failure.

        Calls for the same strip share one in-flight fetch. The fetch runs as a tracked task so
        closing the runtime cancels it; waiting callers then get a LeproApiError.
        """
        if self._closed:
            raise LeproApiError("cloud scene catalog unavailable")
        task = self._refreshing.get(did)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._refresh_scenes(did))
            self._refreshing[did] = task
            self._tasks.add(task)

            def done(t, did=did):
                self._tasks.discard(t)
                if self._refreshing.get(did) is t:
                    del self._refreshing[did]
                if not t.cancelled():
                    t.exception()  # consumed here; callers re-raise from their own await

            task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():  # the fetch was cancelled with the runtime, not this caller
                raise LeproApiError("cloud scene catalog unavailable") from None
            raise

    async def refresh_all_scenes(self) -> dict:
        """Refresh every strip (at most SCENE_FETCH_CONCURRENCY at a time). did -> count or LeproApiError."""
        dids = list(self.devices)
        out = await asyncio.gather(*(self.refresh_scenes(d) for d in dids), return_exceptions=True)
        for r in out:
            if isinstance(r, BaseException) and not isinstance(r, LeproApiError):
                raise r
        return dict(zip(dids, out))

    async def _refresh_scenes(self, did: str) -> int:
        fid = self.devices[did].info.get("fid")
        if self.api is None or self.token is None or fid is None:
            raise LeproApiError("cloud scene catalog unavailable")
        async with self._scene_sem:
            token = self.token
            try:
                scenes = await self.api.fetch_scenes(token, fid, did)
            except LeproAuthError:
                token = await self._renew_token(token)  # one re-login (shared), then one retry
                scenes = await self.api.fetch_scenes(token, fid, did)
        self.set_scenes(did, scenes)
        return len(scenes)

    async def _renew_token(self, stale: str) -> str:
        """Single-flight re-login: if another caller already replaced the rejected token, reuse theirs."""
        async with self._auth_lock:
            if self.token is not None and self.token != stale:
                return self.token
            new = await self.api.login()
            self.token = new
            return new

    # ---- lifecycle -----------------------------------------------------------
    async def async_shutdown(self) -> None:
        self._closed = True
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.transport is not None:
            await self.transport.stop()
