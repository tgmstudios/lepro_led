"""Cloud scene catalog + apply. All ids, labels and strings here are synthetic."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lepro_led import protocol as p
from custom_components.lepro_led.api import (
    CloudScene,
    LeproApiError,
    LeproApi,
    Session,
    MqttInfo,
    _parse_scenes,
    make_mac,
)
from custom_components.lepro_led.const import DOMAIN
from custom_components.lepro_led.state import DeviceState
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .test_integration import FakeTransport, entry, fake_cloud  # noqa: F401

RES_A = "N01:P1000212345678ABCDEFF2100021234ABCDU3VFAKE0001;"  # synthetic, tail not understood by parse_d50
RES_B = "N01:P10001AA00BBF210001FFFFU3VFAKE0002;"
FDEV = [{"did": 1000001, "fid": 77, "name": "Strip A", "series": "S1-5"}, {"did": 1000002, "fid": 77, "name": "Strip B", "series": "S1-5"}]


def row(cid=1, name=None, res=RES_A, **kw):
    r = {"cid": cid, "sid": 0, "striplight": {"command_type": 1, "dp": "d50", "res": res}}
    if name is not None:
        r["groupName"] = name
    r.update(kw)
    return r


def resp(rows, code=0):
    return {"code": code, "data": {"list": rows}}


# ---- API parsing -----------------------------------------------------------
def test_parse_labels_and_stable_ids():
    sc = _parse_scenes(resp([row(5, "Sunset"), row(6, None, RES_B)]), "77", "1000001")
    assert [(s.scene_id, s.label, s.d50) for s in sc] == [("cid-5", "Sunset", RES_A), ("cid-6", "Scene 6", RES_B)]


def test_duplicate_labels_and_ids_disambiguated():
    sc = _parse_scenes(resp([row(5, "Same"), row(6, "Same", RES_B), row(5, "Dup id", RES_B)]), "77", "1000001")
    assert [s.label for s in sc] == ["Same (cid-5)", "Same (cid-6)"]
    assert len({s.label for s in sc}) == 2


def test_sid_fallback_and_zero_ids_skipped():
    r = row(0, None)
    r["sid"] = 42
    sc = _parse_scenes(resp([r, row(0, "x")]), "77", "1000001")
    assert [(s.scene_id, s.label) for s in sc] == [("sid-42", "Scene 42")]


@pytest.mark.parametrize("bad", [
    {"dp": "d51", "res": RES_A}, {"dp": "d50", "res": ""}, {"dp": "d50", "res": 5},
    {"dp": "d50", "res": "N01;\n{\"x\":1}"}, {"dp": "d50", "res": "A" * 5000}, "x", None,
])
def test_invalid_striplight_rows_skipped(bad):
    r = row(3, "ok")
    r["striplight"] = bad
    assert _parse_scenes(resp([r]), "77", "1000001") == []


def test_foreign_identity_rows_skipped():
    rows = [row(1, "a", did=999), row(2, "b", fid=5), row(3, "c", did=1000001, fid=77)]
    assert [s.scene_id for s in _parse_scenes(resp(rows), "77", "1000001")] == ["cid-3"]


@pytest.mark.parametrize("bad", [[], "x", {"code": 1, "data": {"list": []}}, {"code": 0, "data": []}, {"code": 0, "data": {"list": "no"}}, {"code": True, "data": {"list": []}}])
def test_malformed_responses_raise(bad):
    with pytest.raises(LeproApiError):
        _parse_scenes(bad, "77", "1000001")


def test_empty_list_ok():
    assert _parse_scenes({"code": 0, "data": {}}, "77", "1") == []


async def test_fetch_scenes_request_shape(hass, aioclient_mock):
    aioclient_mock.get(__import__("re").compile(r"https://api-na-iot\.lepro\.com/v3/scene/list/.*"), json=resp([row(1, "S")]))
    api = LeproApi(async_get_clientsession(hass), "na", "u", "pw", "en", make_mac("u"), "f")
    sc = await api.fetch_scenes("TOK", 77, 1000001)
    method, url, _, headers = aioclient_mock.mock_calls[0]
    import re
    assert method == "GET" and re.fullmatch(r"https://api-na-iot\.lepro\.com/v3/scene/list/fid/77/did/1000001/cid/0/limit/50/timestamp/\d{10}", str(url))
    assert headers["Authorization"] == "Bearer TOK" and len(sc) == 1
    assert len(aioclient_mock.mock_calls) == 1


async def test_fetch_scenes_rejects_unsafe_ids(hass, aioclient_mock):
    api = LeproApi(async_get_clientsession(hass), "na", "u", "pw", "en", make_mac("u"), "f")
    for fid, did in (("1/../x", 1), (1, "a b"), (None, 1), (1, True)):
        with pytest.raises(LeproApiError):
            await api.fetch_scenes("T", fid, did)
    assert not aioclient_mock.mock_calls


async def test_fetch_scenes_http_error(hass, aioclient_mock):
    aioclient_mock.get(__import__("re").compile(r".*/v3/scene/list/.*"), status=500)
    api = LeproApi(async_get_clientsession(hass), "na", "u", "pw", "en", make_mac("u"), "f")
    with pytest.raises(LeproApiError):
        await api.fetch_scenes("T", 1, 2)


# ---- state ------------------------------------------------------------------
def test_command_payload_is_verbatim_and_validated():
    s = DeviceState()
    s.cloud_scenes = {RES_A: "A"}
    assert s.command_cloud_scene(RES_A) == {"d1": 1, "d2": 2, "d50": RES_A}
    assert s.command_cloud_scene(RES_A, brightness_pct=100) == {"d1": 1, "d2": 2, "d50": RES_A, "d52": 1000}
    assert s.command_cloud_scene(RES_A, brightness=1)["d52"] == 100
    for kw in ({"brightness": 0}, {"brightness": True}, {"brightness_pct": 0}, {"brightness": 5, "brightness_pct": 5}):
        with pytest.raises(ValueError):
            s.command_cloud_scene(RES_A, **kw)
    with pytest.raises(ValueError):
        s.command_cloud_scene(RES_B)


def test_unknown_raw_d50_not_labelled_as_local_effect():
    s = DeviceState()
    s.cloud_scenes = {RES_A: "A"}
    s.apply_report({"d2": 2, "d50": p.build_d50([(1, 2, 3)] * 25, "gradient", 40)})
    assert s.effect == "gradient" and s.cloud_scene is None
    s.apply_report({"d2": 2, "d50": RES_A})
    assert s.cloud_scene == "A" and s.effect is None and s.direction is None and s.palette == []
    assert s.d50_effect == "gradient"  # local interpretation untouched
    s.apply_report({"d2": 3})
    assert s.cloud_scene is None


# ---- HA integration -----------------------------------------------------------
@pytest.fixture
def cloud(fake_cloud):
    sess = Session("tok", "u1", MqttInfo("h", 8883, "r", "c"), FDEV, {})

    async def fetch(self):
        return sess, b"r", b"c"

    async def scenes(self, token, fid, did):
        assert token == "tok" and str(fid) == "77"
        return [CloudScene("cid-1", "Warm", RES_A), CloudScene("cid-2", "Cool", RES_B)] if str(did) == "1000001" else []

    with patch("custom_components.lepro_led.LeproApi.fetch_session", fetch), patch("custom_components.lepro_led.LeproApi.fetch_scenes", scenes):
        yield fake_cloud


async def _setup(hass, cloud):
    e = entry()
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    return e, cloud.instances[0], reg


def _sets(t):
    return [b for topic, b in t.sent if topic.endswith("/set")]


async def test_select_lists_server_labels_and_applies(hass, cloud):
    e, t, reg = await _setup(hass, cloud)
    sel = reg.async_get_entity_id("select", DOMAIN, "1000001_cloud_scene")
    st = hass.states.get(sel)
    assert st.attributes["options"] == ["Warm", "Cool"] and st.attributes["scene_ids"] == {"Warm": "cid-1", "Cool": "cid-2"}
    n = len(_sets(t))
    await hass.services.async_call("select", "select_option", {"entity_id": sel, "option": "Cool"}, blocking=True)
    sent = _sets(t)
    assert len(sent) == n + 1 and sent[-1]["d"] == {"d1": 1, "d2": 2, "d50": RES_B}
    # published, not yet device-confirmed: no selection claimed
    assert hass.states.get(sel).state == "unknown"
    light = reg.async_get_entity_id("light", DOMAIN, "1000001")
    assert hass.states.get(light).attributes["cloud_scene"] is None
    # device reports the exact raw d50 -> confirmed, never relabelled as a local effect
    t.on_message("le/1000001/prp/rpt", json.dumps({"d": {"d2": 2, "d50": RES_B}}).encode())
    await hass.async_block_till_done()
    assert hass.states.get(sel).state == "Cool"
    la = hass.states.get(light).attributes
    assert la["cloud_scene"] == "Cool" and la.get("effect") is None and la["palette"] == []
    assert len(_sets(t)) == n + 1


async def test_other_strip_has_no_scenes_and_cannot_use_foreign_scene(hass, cloud):
    e, t, reg = await _setup(hass, cloud)
    sel_b = reg.async_get_entity_id("select", DOMAIN, "1000002_cloud_scene")
    assert hass.states.get(sel_b).attributes["options"] == []
    light_b = reg.async_get_entity_id("light", DOMAIN, "1000002")
    n = len(_sets(t))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "apply_cloud_scene", {"entity_id": [light_b], "scene_id": "cid-1"}, blocking=True)
    assert len(_sets(t)) == n


async def test_apply_service_exact_payload_with_brightness(hass, cloud):
    e, t, reg = await _setup(hass, cloud)
    light = reg.async_get_entity_id("light", DOMAIN, "1000001")
    seg = reg.async_get_entity_id("light", DOMAIN, "1000001_segment_03")
    n = len(_sets(t))
    await hass.services.async_call(DOMAIN, "apply_cloud_scene", {"entity_id": [light, seg], "scene_id": "cid-1", "brightness_pct": 50}, blocking=True)
    sent = _sets(t)
    assert len(sent) == n + 1  # strip + segment alias = one publish
    assert sent[-1]["d"] == {"d1": 1, "d2": 2, "d50": RES_A, "d52": p.ha_to_d52(128)}
    assert all(set(b["d"]) <= {"d1", "d2", "d50", "d52"} for b in sent[n:])


@pytest.mark.parametrize("data", [
    {"scene_id": "nope"}, {"scene_id": "cid-1", "brightness": 0}, {"scene_id": "cid-1", "brightness": 3, "brightness_pct": 3},
    {"scene_id": ""}, {"scene_id": "cid-1", "url": "http://x"}, {},
])
async def test_apply_rejections_publish_nothing(hass, cloud, data):
    import voluptuous as vol
    e, t, reg = await _setup(hass, cloud)
    light = reg.async_get_entity_id("light", DOMAIN, "1000001")
    n = len(_sets(t))
    with pytest.raises((ServiceValidationError, vol.Invalid)):
        await hass.services.async_call(DOMAIN, "apply_cloud_scene", {"entity_id": [light], **data}, blocking=True)
    assert len(_sets(t)) == n


async def test_apply_to_non_lepro_entity_rejected(hass, cloud):
    await _setup(hass, cloud)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "apply_cloud_scene", {"entity_id": ["light.other"], "scene_id": "cid-1"}, blocking=True)


async def test_setup_survives_catalog_failure(hass, fake_cloud):
    sess = Session("tok", "u1", MqttInfo("h", 8883, "r", "c"), FDEV, {})

    async def fetch(self):
        return sess, b"r", b"c"

    with patch("custom_components.lepro_led.LeproApi.fetch_session", fetch), \
         patch("custom_components.lepro_led.LeproApi.fetch_scenes", AsyncMock(side_effect=LeproApiError("boom"))):
        e, t, reg = await _setup(hass, fake_cloud)
    assert e.state.value == "loaded"
    sel = reg.async_get_entity_id("select", DOMAIN, "1000001_cloud_scene")
    assert hass.states.get(sel).attributes["options"] == []
    assert reg.async_get_entity_id("light", DOMAIN, "1000001")


async def test_refresh_service_updates_and_keeps_old_on_failure(hass, cloud):
    e, t, reg = await _setup(hass, cloud)
    sel = reg.async_get_entity_id("select", DOMAIN, "1000001_cloud_scene")
    light = reg.async_get_entity_id("light", DOMAIN, "1000001")
    with patch("custom_components.lepro_led.LeproApi.fetch_scenes", AsyncMock(return_value=[CloudScene("cid-9", "New", RES_B)])):
        await hass.services.async_call(DOMAIN, "refresh_cloud_scenes", {"entity_id": [light]}, blocking=True)
    assert hass.states.get(sel).attributes["options"] == ["New"]
    with patch("custom_components.lepro_led.LeproApi.fetch_scenes", AsyncMock(side_effect=LeproApiError("x"))):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "refresh_cloud_scenes", {"entity_id": [light]}, blocking=True)
    assert hass.states.get(sel).attributes["options"] == ["New"]


async def test_unload_removes_services_and_select(hass, cloud):
    e, t, reg = await _setup(hass, cloud)
    assert hass.services.has_service(DOMAIN, "apply_cloud_scene") and hass.services.has_service(DOMAIN, "refresh_cloud_scenes")
    assert await hass.config_entries.async_unload(e.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "apply_cloud_scene")
    assert hass.states.get(reg.async_get_entity_id("select", DOMAIN, "1000001_cloud_scene")).state == "unavailable"
