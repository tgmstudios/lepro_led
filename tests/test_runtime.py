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
        self.stopped = 0

    async def stop(self):
        self.stopped += 1

    async def publish(self, topic, payload):
        if self.fail:
            raise self.fail
        self.sent.append((topic, json.loads(payload)))


def make():
    rt = LeproRuntime([{"did": 111, "name": "A", "series": "S1-5"}, {"did": 222, "name": "B"}])
    t = FakeTransport()
    rt.transport = t
    return rt, t


class GatedTransport(FakeTransport):
    """publish() blocks until released, optionally failing afterwards."""

    def __init__(self):
        super().__init__()
        self.gates = []
        self.entered = []

    async def publish(self, topic, payload):
        gate = asyncio.Event()
        self.gates.append(gate)
        self.entered.append(asyncio.Event())
        self.entered[-1].set()
        await gate.wait()
        await super().publish(topic, payload)


async def _spin(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


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


# ---- racing device reports vs. command rollback / commit ------------------------------------

async def test_failing_publish_preserves_newer_report_and_unrelated_fields():
    rt = LeproRuntime([{"did": 111}])
    started, release = asyncio.Event(), asyncio.Event()

    class T:
        connected = True

        async def publish(self, topic, payload):
            started.set()
            await release.wait()
            raise TransportNotConnected("down")

    rt.transport = T()
    msg(rt, 111, "rpt", {"d1": 1, "d2": 2, "d50": p.build_d50([(1, 2, 3)] * 25, "gradient", 70), "d52": 300})
    st = rt.devices["111"].state
    speed_before, colors_before = st.speed, list(st.colors)
    task = asyncio.create_task(rt.send("111", lambda s: s.command_turn_on(rgb=RED, brightness=255)))
    await started.wait()
    msg(rt, 111, "rpt", {"d52": 600, "d1": 0})  # newer device-side state during the failing publish
    release.set()
    with pytest.raises(HomeAssistantError):
        await task
    st = rt.devices["111"].state
    assert st.brightness == p.d52_to_ha(600) and st.on is False  # racing report kept
    assert st.colors == colors_before and st.speed == speed_before and st.d50_effect == "gradient"  # unrelated fields kept
    assert rt.pending_confirmation("111") == frozenset()  # nothing was committed


async def test_successful_publish_keeps_newer_report_over_local_desired_value():
    rt = LeproRuntime([{"did": 111}])
    t = GatedTransport()
    rt.transport = t
    msg(rt, 111, "rpt", {"d1": 1, "d2": 2, "d50": p.build_d50([(1, 2, 3)] * 25, "gradient", 70), "d52": 300})
    task = asyncio.create_task(rt.send("111", lambda s: s.command_turn_on(rgb=RED, brightness=255)))
    await _spin()
    msg(rt, 111, "rpt", {"d52": 600})
    t.gates[0].set()
    await task
    st = rt.devices["111"].state
    assert st.brightness == p.d52_to_ha(600)  # device report wins for the field it touched
    assert st.colors[0] == RED and abs(st.speed - 70) < 1  # command's own, unreported fields committed
    assert "colors" in rt.pending_confirmation("111") and "brightness" not in rt.pending_confirmation("111")


async def test_concurrent_commands_are_serialised_and_second_builds_on_first():
    rt = LeproRuntime([{"did": 111}, {"did": 222}])
    t = GatedTransport()
    rt.transport = t
    msg(rt, 111, "rpt", {"d1": 1, "d2": 2, "d50": p.build_d50([(1, 2, 3)] * 25, "gradient", 70), "d52": 300})
    first = asyncio.create_task(rt.send("111", lambda s: s.command_turn_on(rgb=RED)))
    await _spin()
    second = asyncio.create_task(rt.send("111", lambda s: s.command_speed(20)))
    other = asyncio.create_task(rt.send("222", lambda s: s.command_turn_on()))  # other device is not blocked
    await _spin()
    assert len(t.gates) == 2  # first(111) + 222; second(111) is waiting on the per-device lock
    msg(rt, 111, "rpt", {"d52": 900})
    t.gates[0].set()
    await _spin()
    assert len(t.gates) == 3
    t.gates[1].set()
    t.gates[2].set()
    await asyncio.gather(first, second, other)
    pubs = [b for topic, b in t.sent if topic == "le/111/prp/set"]
    assert len(pubs) == 2
    assert pubs[1]["d"]["d50"] == p.build_d50([RED] * 25, "gradient", 20)  # sees first command's colours
    st = rt.devices["111"].state
    assert st.brightness == p.d52_to_ha(900) and st.colors[0] == RED and st.speed == 20


async def test_failed_command_does_not_block_next_command():
    rt, t = make()
    t.fail = TransportNotConnected("down")
    with pytest.raises(HomeAssistantError):
        await rt.send("111", lambda s: s.command_turn_on())
    t.fail = None
    await rt.send("111", lambda s: s.command_turn_on())
    assert rt.devices["111"].state.on is True


async def test_build_value_error_leaves_state_untouched_and_reports_intact():
    rt, t = make()
    msg(rt, 111, "rpt", {"d52": 500})
    before = rt.devices["111"].state.brightness
    with pytest.raises(HomeAssistantError):
        await rt.send("111", lambda s: (setattr(s, "brightness", 1), s.command_turn_on(effect="bogus"))[1])
    assert rt.devices["111"].state.brightness == before


# ---- broker publish vs. device confirmation ------------------------------------------------

async def test_own_set_echo_is_not_a_device_report():
    rt, t = make()
    hits = []
    rt.add_listener("111", lambda: hits.append(1))
    await rt.send("111", lambda s: s.command_turn_on(brightness=255))
    hits.clear()
    assert rt.pending_confirmation("111") == frozenset({"on", "brightness"})
    msg(rt, 111, "set", {"d1": 0, "d52": 10})  # broker echo of a command topic
    msg(rt, 111, "setr", {"d1": 0, "d52": 10})  # ack format unverified -> not trusted
    st = rt.devices["111"].state
    assert st.on is True and st.brightness == 255 and hits == []
    assert rt.pending_confirmation("111") == frozenset({"on", "brightness"})


async def test_device_report_confirms_only_the_reported_fields():
    rt, t = make()
    await rt.send("111", lambda s: s.command_turn_on(brightness=255))
    msg(rt, 111, "rpt", {"d52": 1000})
    assert rt.pending_confirmation("111") == frozenset({"on"})
    msg(rt, 111, "getr", {"d1": 1})
    assert rt.pending_confirmation("111") == frozenset()


@pytest.mark.parametrize("topic", ["le/111/prp/rpt/extra", "le/111/prp", "x/111/prp/rpt", "le/111/act/rpt"])
def test_report_topic_must_match_exactly(topic):
    rt, _ = make()
    rt.handle_message(topic, b'{"d":{"d1":1}}')
    assert rt.devices["111"].state.on is None
