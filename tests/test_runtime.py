import asyncio
import json

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.lepro_led import protocol as p
from custom_components.lepro_led.runtime import LeproRuntime
from custom_components.lepro_led.transport_mqtt import TransportNotConnected

RED = (255, 0, 0)


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.connected = True
        self.fail = None

    async def publish(self, topic, payload):
        if self.fail:
            raise self.fail
        self.sent.append((topic, json.loads(payload)))


def make():
    rt = LeproRuntime([{"did": 111, "name": "A", "series": "S1-5"}, {"did": 222, "name": "B"}])
    t = FakeTransport()
    rt.transport = t
    return rt, t


def msg(rt, did, kind, d):
    rt.handle_message(f"le/{did}/prp/{kind}", json.dumps({"d": d}).encode())


def test_device_ids_are_strings():
    rt, _ = make()
    assert set(rt.devices) == {"111", "222"}


def test_reports_update_state_and_notify_only_that_device():
    rt, _ = make()
    hits = []
    rt.add_listener("111", lambda: hits.append("111"))
    rt.add_listener("222", lambda: hits.append("222"))
    msg(rt, 111, "rpt", {"d1": 1, "d52": 1000})
    assert hits == ["111"]
    assert rt.devices["111"].state.on is True


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"d": 5}', b'{"d": {"zzz": 1}}'])
def test_garbage_payloads_ignored(raw):
    rt, _ = make()
    hits = []
    rt.add_listener("111", lambda: hits.append(1))
    rt.handle_message("le/111/prp/rpt", raw)
    assert hits == [] and rt.devices["111"].state.on is None


def test_unknown_device_and_topics_ignored():
    rt, _ = make()
    rt.handle_message("le/999/prp/rpt", b'{"d":{"d1":1}}')
    rt.handle_message("foo", b"{}")
    rt.handle_message("le/111/act/exe", b'{"d":{"d1":1}}')
    assert rt.devices["111"].state.on is None


def test_availability_follows_transport_and_online_report():
    rt, t = make()
    assert rt.available("111")
    msg(rt, 111, "rpt", {"online": 0})
    assert not rt.available("111")
    assert rt.available("222")
    t.connected = False
    assert not rt.available("222")
    assert not rt.available("nope")


async def test_send_publishes_envelope_and_updates_state():
    rt, t = make()
    await rt.send("111", lambda s: s.command_turn_on(brightness=255))
    topic, body = t.sent[0]
    assert topic == "le/111/prp/set"
    assert body["d"] == {"d1": 1, "d52": 1000}
    assert isinstance(body["id"], int) and isinstance(body["t"], int)
    assert rt.devices["111"].state.on is True


async def test_send_failure_rolls_back_and_raises():
    rt, t = make()
    t.fail = TransportNotConnected("down")
    with pytest.raises(HomeAssistantError):
        await rt.send("111", lambda s: s.command_turn_on(rgb=RED))
    st = rt.devices["111"].state
    assert st.on is None and st.colors[0] == (255, 255, 255)


async def test_send_invalid_input_raises_ha_error_without_publish():
    rt, t = make()
    with pytest.raises(HomeAssistantError):
        await rt.send("111", lambda s: s.command_turn_on(effect="bogus"))
    assert t.sent == []


async def test_send_noop_command_publishes_nothing_but_notifies():
    rt, t = make()
    hits = []
    rt.add_listener("111", lambda: hits.append(1))
    await rt.send("111", lambda s: s.command_sensitivity(10))
    assert t.sent == [] and hits == [1]
    assert rt.devices["111"].state.sensitivity == 10


async def test_unsubscribe_listener():
    rt, _ = make()
    hits = []
    off = rt.add_listener("111", lambda: hits.append(1))
    off()
    msg(rt, 111, "rpt", {"d1": 1})
    assert hits == []


async def test_reconnect_requests_state_for_every_device():
    rt, t = make()
    rt.handle_status(True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    topics = sorted(topic for topic, _ in t.sent)
    assert topics == ["le/111/prp/get", "le/222/prp/get"]
    assert t.sent[0][1]["d"] == ["d1", "d2", "d3", "d4", "d5", "d30", "d50", "d52", "d60", "online"]
    await rt.async_shutdown()


async def test_status_change_notifies_all_listeners():
    rt, _ = make()
    hits = []
    rt.add_listener("111", lambda: hits.append("a"))
    rt.add_listener("222", lambda: hits.append("b"))
    rt.handle_status(False)
    assert sorted(hits) == ["a", "b"]


def test_initial_state_seeded_from_device_list_like_legacy():
    d50 = p.build_d50([RED] * 25, "gradient", 30)
    rt = LeproRuntime([{"did": 5, "switch": 1, "d2": 2, "d50": d50, "d52": 500, "d60": p.build_d60("wave_1", 40)}])
    st = rt.devices["5"].state
    assert st.on is True and st.colors[0] == RED and st.d50_effect == "gradient"
    assert st.brightness == p.d52_to_ha(500) and abs(st.sensitivity - 40) <= 1


def test_initial_state_tolerates_missing_and_garbage():
    rt = LeproRuntime([{"did": 5, "d50": "junk", "d52": None}])
    assert rt.devices["5"].state.on is None
