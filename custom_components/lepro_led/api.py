"""Lepro cloud REST client (login, profile, certificates, family/device lists)."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass

import aiohttp

from .const import (
    DEVICE_LIST_PATH,
    FAMILY_LIST_PATH,
    LOGIN_PATH,
    REGIONS,
    USER_PROFILE_PATH,
)

APP_VERSION = "1.0.9.202"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class LeproApiError(Exception):
    """Network/server problem; retry later."""


class LeproAuthError(LeproApiError):
    """Credentials rejected."""


def make_mac(account: str) -> str:
    h = hashlib.md5(account.encode()).hexdigest()  # noqa: S324 - legacy derivation, kept for stable device identity
    return f"02:{h[0:2]}:{h[2:4]}:{h[4:6]}:{h[6:8]}:{h[8:10]}"


def make_fcm_token() -> str:
    """Per-install random placeholder (the integration has no push channel)."""
    return secrets.token_urlsafe(24)


@dataclass
class MqttInfo:
    host: str
    port: int
    root_url: str
    cert_url: str


@dataclass
class Session:
    token: str
    uid: str
    mqtt: MqttInfo
    devices: list
    headers: dict


def _obj(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise LeproApiError(f"unexpected {what} response")
    return value


def _str(value, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise LeproApiError(f"unexpected {what} response")
    return value


def _parse_profile(profile) -> tuple[MqttInfo, str]:
    pd = _obj(_obj(profile, "profile").get("data"), "profile")
    mqtt = _obj(pd.get("mqtt"), "profile")
    port = mqtt.get("port")
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise LeproApiError("unexpected profile response")
    try:
        port = int(port)
    except ValueError as err:
        raise LeproApiError("unexpected profile response") from err
    if not 0 < port < 65536 or pd.get("uid") is None:
        raise LeproApiError("unexpected profile response")
    info = MqttInfo(_str(mqtt.get("host"), "profile"), port, _str(mqtt.get("root"), "profile"), _str(mqtt.get("cert"), "profile"))
    return info, str(pd["uid"])


def _parse_family(fam):
    families = _obj(_obj(fam, "family").get("data"), "family").get("list")
    if not isinstance(families, list):
        raise LeproApiError("unexpected family response")
    if not families:
        raise LeproApiError("no family in account")
    fid = _obj(families[0], "family").get("fid")
    if fid is None:
        raise LeproApiError("unexpected family response")
    return fid


def _parse_devices(dev) -> list:
    data = _obj(dev, "device list").get("data")
    if data is None:
        return []
    items = _obj(data, "device list").get("list")
    if items is None:
        return []
    if not isinstance(items, list):
        raise LeproApiError("unexpected device list response")
    return [d for d in items if isinstance(d, dict) and d.get("did") is not None]


class LeproApi:
    def __init__(self, http, region, account, password, language, mac, fcm_token):
        if region not in REGIONS:
            raise ValueError(f"unknown region {region!r}")
        self._http = http
        self._host = REGIONS[region]
        self._account, self._password = account, password
        self._language, self._mac, self._fcm = language, mac, fcm_token

    def _headers(self, token: str | None = None) -> dict:
        ts = str(int(time.time()))
        h = {
            "App-Version": APP_VERSION,
            "Device-Model": "custom_integration",
            "Device-System": "custom",
            "GMT": "+0",
            "Host": self._host,
            "Language": self._language,
            "Platform": "2",
            "Screen-Size": "1536*2048",
            "Slanguage": self._language,
            "User-Agent": f"LE/{APP_VERSION} (Custom Integration)",
            "Timestamp": ts,
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
            h["Accept-Encoding"] = "gzip"
        else:
            h["Content-Type"] = "application/json"
        return h

    async def _json(self, method, path, **kw):
        url = f"https://{self._host}{path}"
        try:
            async with self._http.request(method, url, timeout=REQUEST_TIMEOUT, **kw) as resp:
                if resp.status in (401, 403):
                    raise LeproAuthError(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise LeproApiError(f"HTTP {resp.status} for {path.split('/')[1]}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise LeproApiError(f"request failed: {type(err).__name__}") from err
        except ValueError as err:  # JSON decode failure; never include the body
            raise LeproApiError(f"invalid JSON response for {path.split('/')[1]}") from err

    async def login(self) -> str:
        ts = str(int(time.time()))
        payload = {
            "platform": "2",
            "account": self._account,
            "password": self._password,
            "mac": self._mac,
            "timestamp": ts,
            "language": self._language,
            "fcmToken": self._fcm,
        }
        data = await self._json("POST", LOGIN_PATH, json=payload, headers=self._headers())
        if not isinstance(data, dict):
            raise LeproApiError("unexpected login response")
        code = data.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise LeproApiError("unexpected login response")
        if code != 0:  # explicit rejection; the server's msg is never echoed
            raise LeproAuthError(f"login rejected (code {code})")
        body = data.get("data")
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise LeproApiError("unexpected login response")
        return token

    async def _download(self, url: str, headers: dict) -> bytes:
        try:
            async with self._http.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    raise LeproApiError(f"certificate download HTTP {resp.status}")
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise LeproApiError("certificate download failed") from err

    async def fetch_session(self):
        """Full bootstrap. Returns (Session, root_ca_bytes, client_cert_bytes)."""
        token = await self.login()
        headers = self._headers(token)
        profile = await self._json("GET", USER_PROFILE_PATH, headers=headers)
        info, uid = _parse_profile(profile)
        root = await self._download(info.root_url, headers)
        cert = await self._download(info.cert_url, headers)

        fam = await self._json("GET", FAMILY_LIST_PATH.format(timestamp=headers["Timestamp"]), headers=headers)
        fid = _parse_family(fam)

        headers = {**headers, "Timestamp": str(int(time.time()))}
        dev = await self._json("GET", DEVICE_LIST_PATH.format(fid=fid, timestamp=headers["Timestamp"]), headers=headers)
        devices = _parse_devices(dev)
        return Session(token, uid, info, devices, headers), root, cert
