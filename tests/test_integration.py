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
        self.connected = False
        self.sent, self.stopped = [], 0
        FakeTransport.instances.append(self)

    async def async_wait_connected(self, timeout):
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
    assert len(uids) == 2 * (1 + 25 + 2 + 1 + 1)
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


@pytest.mark.parametrize("body,state", [
    ([1], ConfigEntryState.SETUP_RETRY),
    ({"code": 0, "data": [1]}, ConfigEntryState.SETUP_RETRY),
    ({"code": 1, "msg": "no"}, ConfigEntryState.SETUP_ERROR),
])
async def test_malformed_login_maps_to_not_ready_and_rejection_to_auth(hass, aioclient_mock, body, state):
    e = entry()
    e.add_to_hass(hass)
    aioclient_mock.post("https://api-na-iot.lepro.com/user/login", json=body)
    assert not await hass.config_entries.async_setup(e.entry_id)
    assert e.state is state


async def test_config_flow_malformed_login_is_cannot_connect(hass, aioclient_mock):
    aioclient_mock.post("https://api-na-iot.lepro.com/user/login", json=[1])
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"account": "m@x.y", "password": "w", "region": "na", "language": "en"})
    assert r["type"] == "form" and r["errors"] == {"base": "cannot_connect"}


async def test_migration_sets_canonical_unique_id_for_legacy_entry(hass, fake_cloud):
    e = MockConfigEntry(domain=DOMAIN, data={"account": "A@B.c", "password": "pw"}, version=1, minor_version=1, unique_id=None)
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.unique_id == "a@b.c_na" and e.data["region"] == "na"


async def test_migration_keeps_existing_unique_id(hass, fake_cloud):
    e = MockConfigEntry(domain=DOMAIN, data={"account": "a@b.c", "password": "pw"}, version=1, minor_version=1, unique_id="custom")
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.unique_id == "custom"


async def test_migration_collision_does_not_merge_or_steal_unique_id(hass, fake_cloud):
    owner = MockConfigEntry(domain=DOMAIN, data={"account": "a@b.c", "password": "OWNER", "region": "na"}, version=1, minor_version=2, unique_id="a@b.c_na")
    owner.add_to_hass(hass)
    legacy = MockConfigEntry(domain=DOMAIN, data={"account": "A@b.c", "password": "LEGACY"}, version=1, minor_version=1, unique_id=None)
    legacy.add_to_hass(hass)
    from custom_components.lepro_led import async_migrate_entry
    assert await async_migrate_entry(hass, legacy)
    assert owner.unique_id == "a@b.c_na" and owner.data["password"] == "OWNER"
    assert legacy.unique_id is None and legacy.data["password"] == "LEGACY"
    assert legacy.data["region"] == "na" and legacy.minor_version == 2


async def test_config_flow_aborts_for_existing_legacy_entry_without_unique_id(hass):
    legacy = MockConfigEntry(domain=DOMAIN, data={"account": "N@x.y", "password": "pw"}, version=1, minor_version=2, unique_id=None)
    legacy.add_to_hass(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"account": "n@x.y", "password": "w", "region": "na", "language": "en"})
    assert r["type"] == "abort" and r["reason"] == "already_configured"


async def test_config_flow_same_account_other_region_allowed(hass):
    async def ok(self):
        return "tok"

    e = MockConfigEntry(domain=DOMAIN, data={"account": "n@x.y", "password": "pw", "region": "eu"}, version=1, minor_version=2, unique_id="n@x.y_eu")
    e.add_to_hass(hass)
    with patch("custom_components.lepro_led.config_flow.LeproApi.login", ok):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"account": "n@x.y", "password": "w", "region": "na", "language": "en"})
    assert r["type"] == "create_entry"


def test_hacs_minimum_covers_reauth_api():
    import json, pathlib
    v = json.loads((pathlib.Path(__file__).parents[1] / "hacs.json").read_text())["homeassistant"]
    assert tuple(int(x) for x in v.split(".")) >= (2024, 11, 0)  # _get_reauth_entry / async_update_reload_and_abort


# ---- set_scene ---------------------------------------------------------------
async def _setup(hass, fake_cloud):
    e = entry()
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e, fake_cloud.instances[0], er.async_get(hass)


def _sets(t):
    return [b for topic, b in t.sent if topic.endswith("/set")]


async def test_set_scene_single_atomic_publish_and_state(hass, fake_cloud):
    _, t, reg = await _setup(hass, fake_cloud)
    eid = reg.async_get_entity_id("light", DOMAIN, "1000001")
    n = len(_sets(t))
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [eid], "effect": "gradient", "colors": [[255, 0, 0], [0, 0, 255]], "speed": 70, "brightness_pct": 50}, blocking=True)
    sent = _sets(t)
    assert len(sent) == n + 1
    cols = [(255, 0, 0)] * 13 + [(0, 0, 255)] * 12
    assert sent[-1]["d"] == {"d1": 1, "d2": 2, "d50": p.build_d50(cols, "gradient", 70), "d52": p.ha_to_d52(128)}
    st = hass.states.get(eid)
    assert st.attributes["effect"] == "gradient" and st.attributes["palette"] == [[255, 0, 0], [0, 0, 255]]
    assert st.attributes["effect_speed"] == 70 and st.attributes["direction"] is None
    assert "1000001" not in json.dumps(dict(st.attributes))


async def test_set_scene_segment_alias_and_strip_collapse_to_one_send(hass, fake_cloud):
    _, t, reg = await _setup(hass, fake_cloud)
    strip = reg.async_get_entity_id("light", DOMAIN, "1000001")
    seg = reg.async_get_entity_id("light", DOMAIN, "1000001_segment_05")
    n = len(_sets(t))
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [strip, seg], "colors": [[1, 2, 3]]}, blocking=True)
    assert len(_sets(t)) == n + 1


async def test_set_scene_multiple_devices_preserves_each_and_other_device(hass, fake_cloud):
    _, t, reg = await _setup(hass, fake_cloud)
    a = reg.async_get_entity_id("light", DOMAIN, "1000001")
    b = reg.async_get_entity_id("light", DOMAIN, "1000002")
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d2": 2, "d50": p.build_d50([(9, 9, 9)] * 25, "breath", 20)}}).encode())
    t.on_message("le/1000002/prp/rpt", json.dumps({"d": {"d2": 2, "d50": p.build_d50([(8, 8, 8)] * 25, "circular", 60), "d52": 700}}).encode())
    await hass.async_block_till_done()
    n = len(_sets(t))
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [a], "speed": 90}, blocking=True)
    assert len(_sets(t)) == n + 1
    assert _sets(t)[-1]["d"]["d50"] == p.build_d50([(9, 9, 9)] * 25, "breath", 90)
    assert hass.states.get(b).attributes["effect_speed"] == 60
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [a, b], "brightness": 10}, blocking=True)
    new = _sets(t)[n + 1:]
    assert len(new) == 2 and all(set(x["d"]) == {"d52"} for x in new)


async def test_set_scene_direction_inversion(hass, fake_cloud):
    _, t, reg = await _setup(hass, fake_cloud)
    eid = reg.async_get_entity_id("light", DOMAIN, "1000001")
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d2": 2, "d50": p.build_d50([(9, 9, 9)] * 25, "clockwise", 20)}}).encode())
    await hass.async_block_till_done()
    assert hass.states.get(eid).attributes["direction"] == "clockwise"
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [eid], "direction": "counterclockwise"}, blocking=True)
    assert _sets(t)[-1]["d"]["d50"] == p.build_d50([(9, 9, 9)] * 25, "counterclockwise", 20)


async def test_set_scene_rejections_publish_nothing(hass, fake_cloud):
    from homeassistant.exceptions import HomeAssistantError
    import voluptuous as vol

    _, t, reg = await _setup(hass, fake_cloud)
    a = reg.async_get_entity_id("light", DOMAIN, "1000001")
    n = len(t.sent)
    bad = [
        {"entity_id": [a]},
        {"entity_id": [a], "effect": "flash"},
        {"entity_id": [a], "effect": "wave_1"},
        {"entity_id": [a], "brightness": 5, "brightness_pct": 5},
        {"entity_id": [a], "colors": [[1, 2, 999]]},
        {"entity_id": [a], "colors": [[1, 2]]},
        {"entity_id": [a], "colors": [[True, 0, 0]]},
        {"entity_id": [a], "colors": [[1.5, 0, 0]]},
        {"entity_id": [a], "colors": []},
        {"entity_id": [a], "colors": [[1, 1, 1]] * 26},
        {"entity_id": [a], "speed": float("nan")},
        {"entity_id": [a], "speed": True},
        {"entity_id": [a], "speed": 101},
        {"entity_id": [a], "brightness": True},
        {"entity_id": [a], "brightness": 0},
        {"entity_id": [a], "brightness_pct": float("inf")},
        {"entity_id": [a], "direction": "left"},
        {"entity_id": [a], "direction": "clockwise"},  # solid is not directional
        {"entity_id": ["light.does_not_exist"], "speed": 5},
    ]
    for data in bad:
        with pytest.raises((vol.Invalid, HomeAssistantError)):
            await hass.services.async_call(DOMAIN, "set_scene", data, blocking=True)
    assert len(t.sent) == n


async def test_set_scene_invalid_second_device_blocks_all(hass, fake_cloud):
    from homeassistant.exceptions import HomeAssistantError

    _, t, reg = await _setup(hass, fake_cloud)
    a = reg.async_get_entity_id("light", DOMAIN, "1000001")
    b = reg.async_get_entity_id("light", DOMAIN, "1000002")
    t.on_message("le/1000002/prp/rpt", json.dumps({"d": {"d2": 3}}).encode())  # B in music mode
    await hass.async_block_till_done()
    n = len(t.sent)
    with pytest.raises(HomeAssistantError, match="music"):
        await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [a, b], "colors": [[1, 2, 3]]}, blocking=True)
    assert len(t.sent) == n


async def test_set_scene_failed_publish_does_not_change_state(hass, fake_cloud):
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.lepro_led.transport_mqtt import TransportNotConnected

    _, t, reg = await _setup(hass, fake_cloud)
    eid = reg.async_get_entity_id("light", DOMAIN, "1000001")

    async def boom(topic, payload):
        raise TransportNotConnected("down")

    t.publish = boom
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [eid], "colors": [[1, 2, 3]]}, blocking=True)
    assert hass.states.get(eid).attributes["palette"] == [[255, 255, 255]]


async def test_set_scene_marks_pending_until_device_reports(hass, fake_cloud):
    e, t, reg = await _setup(hass, fake_cloud)
    eid = reg.async_get_entity_id("light", DOMAIN, "1000001")
    await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [eid], "effect": "breath", "speed": 40, "brightness": 100}, blocking=True)
    rt = e.runtime_data
    assert {"d50_effect", "speed", "brightness"} <= rt.pending_confirmation("1000001")
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d52": 300, "d50": p.build_d50([(255, 255, 255)] * 25, "breath", 40)}}).encode())
    await hass.async_block_till_done()
    assert not {"brightness", "d50_effect", "speed"} & rt.pending_confirmation("1000001")


@pytest.mark.parametrize("first", ["a", "b"])
async def test_services_survive_until_last_entry_unloaded(hass, fake_cloud, first):
    from custom_components.lepro_led import LeproApi  # noqa: F401

    devs = {"a@x.y": [DEVICES[0]], "b@x.y": [DEVICES[1]]}

    captured = {}
    orig_init = LeproApi.__init__

    def init(self, session, region, account, *a, **k):
        orig_init(self, session, region, account, *a, **k)
        captured[id(self)] = account

    async def fetch(self):
        return Session("tok", "u", MqttInfo("h", 8883, "r", "c"), devs[captured[id(self)]], {}), b"r", b"c"

    with patch("custom_components.lepro_led.LeproApi.__init__", init), patch("custom_components.lepro_led.LeproApi.fetch_session", fetch):
        ea = entry(account="a@x.y")
        eb = MockConfigEntry(domain=DOMAIN, data={**ea.data, "account": "b@x.y"}, version=1, minor_version=2, unique_id="b@x.y_na")
        ea.add_to_hass(hass)
        eb.add_to_hass(hass)
        assert await hass.config_entries.async_setup(ea.entry_id)
        await hass.async_block_till_done()
        assert eb.state is ConfigEntryState.LOADED
        one, two = (ea, eb) if first == "a" else (eb, ea)
        assert await hass.config_entries.async_unload(one.entry_id)
        await hass.async_block_till_done()
        for svc in ("set_segments", "set_scene"):
            assert hass.services.has_service(DOMAIN, svc)
        # remaining entry still works
        did = "1000002" if two is eb else "1000001"
        eid = er.async_get(hass).async_get_entity_id("light", DOMAIN, did)
        t = [i for i in fake_cloud.instances if i.stopped == 0][0]
        n = len(_sets(t))
        await hass.services.async_call(DOMAIN, "set_scene", {"entity_id": [eid], "brightness": 20}, blocking=True)
        assert len(_sets(t)) == n + 1
        assert await hass.config_entries.async_unload(two.entry_id)
        await hass.async_block_till_done()
        for svc in ("set_segments", "set_scene"):
            assert not hass.services.has_service(DOMAIN, svc)
