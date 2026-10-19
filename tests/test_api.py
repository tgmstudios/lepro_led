import pytest

from custom_components.lepro_led.api import LeproApi, LeproApiError, LeproAuthError, make_fcm_token, make_mac
from homeassistant.helpers.aiohttp_client import async_get_clientsession

H = "api-na-iot.lepro.com"


def api(hass, region="na"):
    return LeproApi(async_get_clientsession(hass), region, "u", "pw", "en", make_mac("u"), "f")


async def test_login_ok_and_payload(hass, aioclient_mock):
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 0, "data": {"token": "T"}})
    assert await api(hass).login() == "T"
    body = aioclient_mock.mock_calls[0][2]
    assert body["platform"] == "2" and body["account"] == "u" and body["fcmToken"] == "f"


@pytest.mark.parametrize("kw", [dict(json={"code": 1, "msg": "bad"}), dict(status=401)])
async def test_login_rejected_is_auth_error(hass, aioclient_mock, kw):
    aioclient_mock.post(f"https://{H}/user/login", **kw)
    with pytest.raises(LeproAuthError):
        await api(hass).login()


async def test_server_error_is_retryable_not_auth(hass, aioclient_mock):
    aioclient_mock.post(f"https://{H}/user/login", status=500)
    with pytest.raises(LeproApiError) as ei:
        await api(hass).login()
    assert not isinstance(ei.value, LeproAuthError)


async def test_timeout_is_api_error(hass, aioclient_mock):
    import asyncio
    aioclient_mock.post(f"https://{H}/user/login", exc=asyncio.TimeoutError())
    with pytest.raises(LeproApiError):
        await api(hass).login()


async def test_region_host_selected(hass, aioclient_mock):
    aioclient_mock.post("https://api-eu-iot.lepro.com/user/login", json={"code": 0, "data": {"token": "T"}})
    assert await api(hass, "eu").login() == "T"
    with pytest.raises(ValueError):
        api(hass, "zz")


async def test_fetch_session_full_flow_and_missing_mqtt(hass, aioclient_mock):
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 0, "data": {"token": "T"}})
    aioclient_mock.get(f"https://{H}/user/profile", json={"data": {"uid": 1}})
    with pytest.raises(LeproApiError):
        await api(hass).fetch_session()
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 0, "data": {"token": "T"}})
    aioclient_mock.get(f"https://{H}/user/profile", json={"data": {"uid": 1, "mqtt": {"host": "m", "port": "8883", "root": "https://c/r", "cert": "https://c/c"}}})
    aioclient_mock.get("https://c/r", content=b"R")
    aioclient_mock.get("https://c/c", content=b"C")
    aioclient_mock.get(__import__("re").compile(rf"https://{H}/family/list/.*"), json={"data": {"list": [{"fid": 9}]}})
    aioclient_mock.get(__import__("re").compile(rf"https://{H}/v3/device/list/fid/9/.*"), json={"data": {"list": [{"did": 1}, {"nodid": 2}]}})
    s, root, cert = await api(hass).fetch_session()
    assert (root, cert) == (b"R", b"C") and s.mqtt.port == 8883 and [d["did"] for d in s.devices] == [1]


def test_fcm_token_is_random_per_install():
    assert make_fcm_token() != make_fcm_token()


@pytest.mark.parametrize("body", [[1], "x", None, 5, {"code": 0, "data": [1]}, {"code": 0, "data": "t"}, {"code": 0, "data": {}},
                                   {"code": 0, "data": {"token": 5}}, {"code": 0, "data": {"token": ""}}, {"code": "0", "data": {"token": "T"}},
                                   {"code": True, "data": {"token": "T"}}, {"data": {"token": "T"}}])
async def test_login_malformed_is_api_error_not_auth_or_attribute_error(hass, aioclient_mock, body):
    aioclient_mock.post(f"https://{H}/user/login", json=body)
    with pytest.raises(LeproApiError) as ei:
        await api(hass).login()
    assert not isinstance(ei.value, LeproAuthError)


async def test_login_explicit_rejection_is_auth_error_without_server_msg_echo(hass, aioclient_mock):
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 1001, "msg": "bad password hunter2 tokenXYZ"})
    with pytest.raises(LeproAuthError) as ei:
        await api(hass).login()
    assert "hunter2" not in str(ei.value) and "tokenXYZ" not in str(ei.value)


async def test_login_invalid_json_is_api_error(hass, aioclient_mock):
    aioclient_mock.post(f"https://{H}/user/login", content=b"<html>not json</html>")
    with pytest.raises(LeproApiError) as ei:
        await api(hass).login()
    assert not isinstance(ei.value, LeproAuthError)


import re as _re

GOOD_PROFILE = {"data": {"uid": 1, "mqtt": {"host": "m", "port": "8883", "root": "https://c/r", "cert": "https://c/c"}}}


def _session_mocks(aioclient_mock, profile=GOOD_PROFILE, family=None, devices=None):
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 0, "data": {"token": "T"}})
    aioclient_mock.get(f"https://{H}/user/profile", json=profile)
    aioclient_mock.get("https://c/r", content=b"R")
    aioclient_mock.get("https://c/c", content=b"C")
    aioclient_mock.get(_re.compile(rf"https://{H}/family/list/.*"), json=family if family is not None else {"data": {"list": [{"fid": 9}]}})
    aioclient_mock.get(_re.compile(rf"https://{H}/v3/device/list/.*"), json=devices if devices is not None else {"data": {"list": [{"did": 1}]}})


@pytest.mark.parametrize("profile", [
    [1], "x", {"data": [1]}, {"data": {"uid": 1, "mqtt": [1]}}, {"data": {"uid": 1, "mqtt": "m"}},
    {"data": {"uid": 1, "mqtt": {"host": "", "port": 1, "root": "r", "cert": "c"}}},
    {"data": {"uid": 1, "mqtt": {"host": 5, "port": 1, "root": "r", "cert": "c"}}},
    {"data": {"uid": 1, "mqtt": {"host": "m", "port": 0, "root": "r", "cert": "c"}}},
    {"data": {"uid": 1, "mqtt": {"host": "m", "port": 70000, "root": "r", "cert": "c"}}},
    {"data": {"uid": 1, "mqtt": {"host": "m", "port": "x", "root": "r", "cert": "c"}}},
    {"data": {"uid": 1, "mqtt": {"host": "m", "port": 1, "root": 7, "cert": "c"}}},
])
async def test_fetch_session_malformed_profile_is_api_error(hass, aioclient_mock, profile):
    _session_mocks(aioclient_mock, profile=profile)
    with pytest.raises(LeproApiError) as ei:
        await api(hass).fetch_session()
    assert not isinstance(ei.value, LeproAuthError)


@pytest.mark.parametrize("family", [[1], "x", {"data": [1]}, {"data": {"list": "x"}}, {"data": {"list": []}},
                                      {"data": {"list": [5]}}, {"data": {"list": [{"nofid": 1}]}}, {"data": {"list": [{"fid": None}]}}])
async def test_fetch_session_malformed_family_is_api_error(hass, aioclient_mock, family):
    _session_mocks(aioclient_mock, family=family)
    with pytest.raises(LeproApiError) as ei:
        await api(hass).fetch_session()
    assert not isinstance(ei.value, LeproAuthError)


@pytest.mark.parametrize("devices", [[1], "x", {"data": [1]}, {"data": "x"}, {"data": {"list": "x"}}, {"data": {"list": {"did": 1}}}])
async def test_fetch_session_malformed_device_payload_is_api_error(hass, aioclient_mock, devices):
    _session_mocks(aioclient_mock, devices=devices)
    with pytest.raises(LeproApiError) as ei:
        await api(hass).fetch_session()
    assert not isinstance(ei.value, LeproAuthError)


async def test_fetch_session_invalid_json_in_profile_is_api_error(hass, aioclient_mock):
    _session_mocks(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"https://{H}/user/login", json={"code": 0, "data": {"token": "T"}})
    aioclient_mock.get(f"https://{H}/user/profile", content=b"garbage")
    with pytest.raises(LeproApiError):
        await api(hass).fetch_session()


async def test_fetch_session_empty_device_list_and_nondict_entries_filtered(hass, aioclient_mock):
    _session_mocks(aioclient_mock, devices={"data": {"list": [5, None, {"did": 3}]}})
    s, _, _ = await api(hass).fetch_session()
    assert [d["did"] for d in s.devices] == [3]
