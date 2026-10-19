"""Cloud MQTT transport with reconnect, resubscribe and clean shutdown.

TLS settings are supplied by the caller (ssl_context) and are not altered here.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)


class TransportError(Exception):
    """Publishing failed."""


class TransportNotConnected(TransportError):
    """No broker connection became available in time."""


class CloudMqttTransport:
    def __init__(
        self,
        host,
        port,
        ssl_context,
        client_id,
        topics,
        on_message: Callable[[str, bytes], None],
        on_status: Callable[[bool], None],
        *,
        client_factory=None,
        mqtt_error=None,
        backoff=(1, 60),
        publish_timeout: float = 10,
    ):
        if client_factory is None or mqtt_error is None:
            import aiomqtt

            client_factory = client_factory or aiomqtt.Client
            mqtt_error = mqtt_error or aiomqtt.MqttError
        self._factory = client_factory
        self._mqtt_error = mqtt_error
        self._host, self._port, self._ssl, self._client_id = host, port, ssl_context, client_id
        self._topics = list(topics)
        self._on_message, self._on_status = on_message, on_status
        self._backoff = backoff
        self._publish_timeout = publish_timeout
        self._client = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._stopped = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    def _status(self, up: bool) -> None:
        try:
            self._on_status(up)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("status callback failed")

    async def _run(self) -> None:
        delay = self._backoff[0]
        while not self._stopped:
            try:
                async with self._factory(
                    hostname=self._host,
                    port=self._port,
                    identifier=self._client_id,
                    tls_context=self._ssl,
                    clean_session=True,
                ) as client:
                    # clean_session: subscriptions do not survive, so always resubscribe
                    for topic in self._topics:
                        await client.subscribe(topic)
                    self._client = client
                    self._connected.set()
                    delay = self._backoff[0]
                    self._status(True)
                    async for message in client.messages:
                        try:
                            self._on_message(message.topic.value, message.payload)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("message handler failed")
            except asyncio.CancelledError:
                raise
            except self._mqtt_error as err:
                _LOGGER.warning("MQTT connection lost/failed: %s", err)
            finally:
                was_up = self._connected.is_set()
                self._client = None
                self._connected.clear()
                if was_up:
                    self._status(False)
            if self._stopped:
                break
            await asyncio.sleep(delay)
            delay = min(max(delay * 2, 1), self._backoff[1]) if self._backoff[1] else 0

    async def publish(self, topic: str, payload: str) -> None:
        if self._stopped or self._task is None:
            raise TransportNotConnected("transport stopped")
        try:
            await asyncio.wait_for(self._connected.wait(), self._publish_timeout)
        except TimeoutError as err:
            raise TransportNotConnected("MQTT broker not connected") from err
        client = self._client
        if client is None:
            raise TransportNotConnected("MQTT broker not connected")
        try:
            await asyncio.wait_for(client.publish(topic, payload), self._publish_timeout)
        except (self._mqtt_error, TimeoutError) as err:
            raise TransportError(f"publish failed: {err}") from err

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._connected.clear()
