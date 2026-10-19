"""HA integration tests with a faked cloud API and MQTT transport."""
import json
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lepro_led import protocol as p
from custom_components.lepro_led.api import LeproApiError, LeproAuthError, MqttInfo, Session
from custom_components.lepro_led.const import DOMAIN

DEVICES = [
    {"did": 1000001, "name": "Strip A", "series": "S1-5", "fwVersion": "2.1.26", "hwVersion": "1.0.0", "switch": 0},
    {"did": 1000002, "name": "Strip B", "series": "S1-5"},
]


class FakeTransport:
    instances = []

    def __init__(self, host, port, ssl_ctx, client_id, topics, on_message, on_status, **kw):
        self.topics, self.on_message, self.on_status = topics, on_message, on_status
        self.client_id = client_id
        self._connected = type("E", (), {"wait": staticmethod(self._w)})()
        self.connected = False
        self.sent, self.stopped = [], 0
        FakeTransport.instances.append(self)

    async def _w(self):
        return True

    async def start(self):
        self.connected = True
        self.on_status(True)

    async def stop(self):
        self.stopped += 1
        self.connected = False

    async def publish(self, topic, payload):
        self.sent.append((topic, json.loads(payload)))


def entry(**over):
    data = {"account": "u@example.com", "password": "pw", "language": "en", "region": "na",
            "persistent_mac": "02:aa:bb:cc:dd:ee", "fcm_token": "x"}
    data.update(over)
    return MockConfigEntry(domain=DOMAIN, data=data, version=1, minor_version=2, unique_id="u@example.com_na")


@pytest.fixture
def fake_cloud():
    FakeTransport.instances = []
    sess = Session("tok", "u1", MqttInfo("h", 8883, "r", "c"), DEVICES, {})

    async def fetch(self):
        return sess, b"root", b"cert"

    with patch("custom_components.lepro_led.LeproApi.fetch_session", fetch), \
         patch("custom_components.lepro_led._write_and_build_ssl", return_value=None), \
         patch("custom_components.lepro_led.CloudMqttTransport", FakeTransport):
        yield FakeTransport


async def test_setup_preserves_unique_ids_and_creates_expected_entities(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    uids = {x.unique_id for x in reg.entities.values() if x.config_entry_id == e.entry_id}
    for did in ("1000001", "1000002"):
        assert did in uids and f"{did}_speed" in uids and f"{did}_sensitivity" in uids
        assert f"{did}_online" in uids
        assert {f"{did}_segment_{i:02d}" for i in range(1, 26)} <= uids
    assert len(uids) == 2 * (1 + 25 + 2 + 1)
    t = fake_cloud.instances[0]
    assert "le/1000001/prp/#" in t.topics and t.client_id.startswith("lepro-app-")
    # reconnect-triggered state requests were sent for both devices
    assert {"le/1000001/prp/get", "le/1000002/prp/get"} <= {x[0] for x in t.sent}


async def test_light_turn_on_publishes_and_state_follows_report(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    t = fake_cloud.instances[0]
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("light", DOMAIN, "1000001")
    await hass.services.async_call("light", "turn_on", {"entity_id": eid, "rgb_color": [255, 0, 0], "brightness": 255, "effect": "breath"}, blocking=True)
    topic, body = t.sent[-1]
    assert topic == "le/1000001/prp/set"
    assert body["d"]["d52"] == 1000 and body["d"]["d2"] == 2
    assert body["d"]["d50"] == p.build_d50([(255, 0, 0)] * 25, "breath", 50)
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d1": 1, "d52": 550}}).encode())
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "on" and st.attributes["effect"] == "breath"
    assert st.attributes["brightness"] == p.d52_to_ha(550)


async def test_availability_follows_transport_and_online_report(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    t = fake_cloud.instances[0]
    eid = er.async_get(hass).async_get_entity_id("light", DOMAIN, "1000001")
    t.on_message("le/1000001/prp/rpt", b'{"d":{"online":0}}')
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "unavailable"
    t.on_message("le/1000001/prp/rpt", b'{"d":{"online":1}}')
    await hass.async_block_till_done()
    assert hass.states.get(eid).state != "unavailable"
    t.connected = False
    t.on_status(False)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "unavailable"


async def test_failed_send_surfaces_error_and_state_not_optimistic(hass, fake_cloud):
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.lepro_led.transport_mqtt import TransportNotConnected

    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    t = fake_cloud.instances[0]

    async def boom(topic, payload):
        raise TransportNotConnected("down")

    t.publish = boom
    eid = er.async_get(hass).async_get_entity_id("light", DOMAIN, "1000001")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("light", "turn_on", {"entity_id": eid}, blocking=True)
    assert hass.states.get(eid).state != "on"


async def test_speed_and_sensitivity_numbers_validate_and_send(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    t = fake_cloud.instances[0]
    reg = er.async_get(hass)
    sp = reg.async_get_entity_id("number", DOMAIN, "1000001_speed")
    # solid effect: value stored, nothing sent
    n = len(t.sent)
    await hass.services.async_call("number", "set_value", {"entity_id": sp, "value": 70}, blocking=True)
    assert len(t.sent) == n and float(hass.states.get(sp).state) == 70
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d2": 2, "d50": p.build_d50([(1, 2, 3)] * 25, "gradient", 10)}}).encode())
    await hass.async_block_till_done()
    await hass.services.async_call("number", "set_value", {"entity_id": sp, "value": 60}, blocking=True)
    assert t.sent[-1][1]["d"]["d50"] == p.build_d50([(1, 2, 3)] * 25, "gradient", 60)
    with pytest.raises(Exception):
        await hass.services.async_call("number", "set_value", {"entity_id": sp, "value": 101}, blocking=True)


async def test_set_segments_service(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    t = fake_cloud.instances[0]
    eid = er.async_get(hass).async_get_entity_id("light", DOMAIN, "1000001")
    await hass.services.async_call(DOMAIN, "set_segments", {"entity_id": [eid], "colors": [[255, 0, 0], [0, 0, 255]]}, blocking=True)
    assert t.sent[-1][1]["d"]["d50"] == p.build_d50([(255, 0, 0)] * 13 + [(0, 0, 255)] * 12, "solid", 50)
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "set_segments", {"entity_id": [eid], "colors": [[1, 2, 999]]}, blocking=True)


async def test_unload_stops_transport_once_and_removes_service(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(e.entry_id)
    assert fake_cloud.instances[0].stopped == 1
    assert e.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "set_segments")
    # reload creates a fresh transport, old one not reused
    await hass.config_entries.async_setup(e.entry_id)
    assert len(fake_cloud.instances) == 2


@pytest.mark.parametrize("exc,state", [(LeproAuthError("bad"), ConfigEntryState.SETUP_ERROR), (LeproApiError("down"), ConfigEntryState.SETUP_RETRY)])
async def test_setup_errors_map_to_ha_states(hass, exc, state):
    async def fetch(self):
        raise exc

    e = entry()
    e.add_to_hass(hass)
    with patch("custom_components.lepro_led.LeproApi.fetch_session", fetch):
        assert not await hass.config_entries.async_setup(e.entry_id)
    assert e.state is state
    if isinstance(exc, LeproAuthError):
        assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_no_devices_is_retry(hass):
    async def fetch(self):
        return Session("t", "u", MqttInfo("h", 1, "r", "c"), [], {}), b"", b""

    e = entry()
    e.add_to_hass(hass)
    with patch("custom_components.lepro_led.LeproApi.fetch_session", fetch):
        await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_RETRY


async def test_migration_adds_region_na_and_generates_per_install_fcm(hass, fake_cloud):
    e = MockConfigEntry(domain=DOMAIN, data={"account": "a@b.c", "password": "pw", "language": "it"}, version=1, minor_version=1)
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.data["region"] == "na" and e.minor_version == 2
    assert e.data["fcm_token"] and not e.data["fcm_token"].startswith("dfi8s76")
    assert e.data["persistent_mac"].startswith("02:")


async def test_config_flow_validates_and_dedupes(hass):
    async def ok(self):
        return "tok"

    async def bad(self):
        raise LeproAuthError("no")

    with patch("custom_components.lepro_led.config_flow.LeproApi.login", bad):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"account": "n@x.y", "password": "w", "region": "na", "language": "en"})
        assert r["errors"] == {"base": "invalid_auth"}
    with patch("custom_components.lepro_led.config_flow.LeproApi.login", ok):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"account": "n@x.y", "password": "w", "region": "na", "language": "en"})
        assert r["type"] == "create_entry" and r["data"]["region"] == "na"
        r2 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r2 = await hass.config_entries.flow.async_configure(r2["flow_id"], {"account": "N@x.y", "password": "w", "region": "na", "language": "en"})
        assert r2["type"] == "abort"
