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


@pytest.mark.parametrize("kw", [dict(json={"code": 1, "msg": "bad"}), dict(json={"code": 0, "data": {}}), dict(status=401)])
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
