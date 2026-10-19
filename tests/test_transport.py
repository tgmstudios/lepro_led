"""CloudMqttTransport lifecycle: connect, resubscribe, reconnect, timeouts, disconnect."""
import asyncio

import pytest

from custom_components.lepro_led.transport_mqtt import (
    CloudMqttTransport,
    TransportError,
    TransportNotConnected,
)


class MqttError(Exception):
    pass


class FakeMessage:
    def __init__(self, topic, payload):
        class T:
            value = topic

        self.topic = T()
        self.payload = payload


class FakeBroker:
    """Hands out FakeClients; each connect can be scripted to fail or be dropped."""

    def __init__(self):
        self.clients = []
        self.fail_next = 0
        self.disconnects = 0

    def factory(self, **kw):
        if self.fail_next:
            self.fail_next -= 1
            raise MqttError("refused")
        c = FakeClient(self, kw)
        self.clients.append(c)
        return c


class FakeClient:
    def __init__(self, broker, kw):
        self.broker, self.kw = broker, kw
        self.subscribed, self.published = [], []
        self.q = asyncio.Queue()
        self.messages = self._iter()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.broker.disconnects += 1
        return False

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def _iter(self):
        while True:
            m = await self.q.get()
            if isinstance(m, Exception):
                raise m
            yield m


async def _wait(cond, n=200):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def make(broker, **kw):
    got, status = [], []
    t = CloudMqttTransport(
        host="h", port=8883, ssl_context=None, client_id="cid",
        topics=["le/1/prp/#", "le/2/prp/#"],
        on_message=lambda topic, payload: got.append((topic, payload)),
        on_status=status.append,
        client_factory=broker.factory, mqtt_error=MqttError,
        backoff=(0, 0), publish_timeout=0.05, **kw)
    return t, got, status


async def test_connect_subscribes_and_dispatches():
    b = FakeBroker()
    t, got, status = make(b)
    await t.start()
    await _wait(lambda: t.connected)
    c = b.clients[0]
    assert c.subscribed == ["le/1/prp/#", "le/2/prp/#"]
    assert c.kw["identifier"] == "cid"
    await c.q.put(FakeMessage("le/1/prp/rpt", b"{}"))
    await _wait(lambda: got)
    assert got == [("le/1/prp/rpt", b"{}")]
    assert status[-1] is True
    await t.stop()


async def test_reconnect_resubscribes_after_drop():
    b = FakeBroker()
    t, got, status = make(b)
    await t.start()
    await _wait(lambda: t.connected)
    await b.clients[0].q.put(MqttError("dropped"))
    await _wait(lambda: len(b.clients) == 2 and t.connected)
    assert b.clients[1].subscribed == ["le/1/prp/#", "le/2/prp/#"]
    assert False in status and status[-1] is True
    await t.stop()


async def test_connect_failures_are_retried():
    b = FakeBroker()
    b.fail_next = 3
    t, _, _ = make(b)
    await t.start()
    await _wait(lambda: t.connected)
    assert len(b.clients) == 1
    await t.stop()


async def test_publish_goes_to_live_client():
    b = FakeBroker()
    t, _, _ = make(b)
    await t.start()
    await _wait(lambda: t.connected)
    await t.publish("le/1/prp/set", "x")
    assert b.clients[0].published == [("le/1/prp/set", "x")]
    await t.stop()


async def test_publish_waits_for_reconnect_then_sends():
    b = FakeBroker()
    t, _, _ = make(b)
    t._publish_timeout = 1
    await t.start()
    await _wait(lambda: t.connected)
    b.fail_next = 2
    await b.clients[0].q.put(MqttError("dropped"))
    await _wait(lambda: not t.connected)
    await t.publish("le/1/prp/set", "queued")
    assert b.clients[-1].published == [("le/1/prp/set", "queued")]
    await t.stop()


async def test_publish_times_out_when_never_connected():
    b = FakeBroker()
    b.fail_next = 10**6
    t, _, _ = make(b)
    await t.start()
    with pytest.raises(TransportNotConnected):
        await t.publish("t", "p")
    await t.stop()


async def test_publish_failure_surfaces_as_transport_error():
    b = FakeBroker()
    t, _, _ = make(b)
    await t.start()
    await _wait(lambda: t.connected)

    async def boom(topic, payload):
        raise MqttError("x")

    b.clients[0].publish = boom
    with pytest.raises(TransportError):
        await t.publish("t", "p")
    await t.stop()


async def test_stop_disconnects_once_and_is_idempotent():
    b = FakeBroker()
    t, _, status = make(b)
    await t.start()
    await _wait(lambda: t.connected)
    await t.stop()
    await t.stop()
    assert b.disconnects == 1
    assert not t.connected
    assert status[-1] is False
    with pytest.raises(TransportNotConnected):
        await t.publish("t", "p")


async def test_callback_exception_does_not_kill_loop():
    b = FakeBroker()
    got = []

    def bad(topic, payload):
        got.append(topic)
        raise RuntimeError("boom")

    t = CloudMqttTransport("h", 1, None, "c", ["a"], bad, lambda s: None,
                           client_factory=b.factory, mqtt_error=MqttError, backoff=(0, 0))
    await t.start()
    await _wait(lambda: t.connected)
    await b.clients[0].q.put(FakeMessage("a/1", b""))
    await b.clients[0].q.put(FakeMessage("a/2", b""))
    await _wait(lambda: len(got) == 2)
    assert t.connected
    await t.stop()
