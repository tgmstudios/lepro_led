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
        if not isinstance(data, dict) or data.get("code") != 0:
            raise LeproAuthError(str((data or {}).get("msg", "login rejected"))[:100])
        token = (data.get("data") or {}).get("token")
        if not token:
            raise LeproAuthError("no token in login response")
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
        try:
            pd = profile["data"]
            mqtt = pd["mqtt"]
            info = MqttInfo(mqtt["host"], int(mqtt["port"]), mqtt["root"], mqtt["cert"])
            uid = str(pd["uid"])
        except (KeyError, TypeError, ValueError) as err:
            raise LeproApiError("unexpected profile response") from err
        root = await self._download(info.root_url, headers)
        cert = await self._download(info.cert_url, headers)

        fam = await self._json("GET", FAMILY_LIST_PATH.format(timestamp=headers["Timestamp"]), headers=headers)
        try:
            fid = fam["data"]["list"][0]["fid"]
        except (KeyError, IndexError, TypeError) as err:
            raise LeproApiError("no family in account") from err

        headers = {**headers, "Timestamp": str(int(time.time()))}
        dev = await self._json("GET", DEVICE_LIST_PATH.format(fid=fid, timestamp=headers["Timestamp"]), headers=headers)
        devices = ((dev or {}).get("data") or {}).get("list") or []
        devices = [d for d in devices if isinstance(d, dict) and d.get("did") is not None]
        return Session(token, uid, info, devices, headers), root, cert
