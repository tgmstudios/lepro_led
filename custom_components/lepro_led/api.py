"""Lepro cloud REST client (login, profile, certificates, family/device lists)."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from dataclasses import dataclass

import aiohttp

from .const import (
    DEVICE_LIST_PATH,
    FAMILY_LIST_PATH,
    LOGIN_PATH,
    REGIONS,
    SCENE_LIST_PATH,
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


SCENE_LIMIT = 50
_PATH_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_D50_RES = re.compile(r"[A-Za-z0-9:;._-]{1,2048}")
_LABEL_MAX = 64


@dataclass(frozen=True)
class CloudScene:
    """A saved cloud scene for one strip. `d50` is the server string, replayed verbatim."""

    scene_id: str
    label: str
    d50: str


def _path_id(value, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not _PATH_ID.fullmatch(str(value)):
        raise LeproApiError(f"invalid {what}")
    return str(value)


def _id_part(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    if not _PATH_ID.fullmatch(text) or set(text) == {"0"}:
        return None  # empty / all-zero ids carry no identity
    return text


def _parse_scenes(resp, fid: str, did: str) -> list[CloudScene]:
    """Validate a scene-list response; rows that are not usable striplight d50 scenes are skipped."""
    body = _obj(resp, "scene list")
    code = body.get("code")
    if isinstance(code, bool) or code != 0:
        raise LeproApiError("unexpected scene list response")
    data = _obj(body.get("data"), "scene list")
    rows = data.get("list")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise LeproApiError("unexpected scene list response")
    found: list[tuple[str, str | None, str]] = []
    seen_ids: set[str] = set()
    for row in rows[:SCENE_LIMIT]:
        if not isinstance(row, dict):
            continue
        # identity keys, when the server includes them, must match the strip we asked about
        if any(k in row and str(row[k]) != want for k, want in (("fid", fid), ("did", did))):
            continue
        strip = row.get("striplight")
        if not isinstance(strip, dict) or strip.get("dp") != "d50":
            continue
        res = strip.get("res")
        if not isinstance(res, str) or not _D50_RES.fullmatch(res):
            continue
        cid, sid = _id_part(row.get("cid")), _id_part(row.get("sid"))
        scene_id = f"cid-{cid}" if cid else f"sid-{sid}" if sid else None
        if scene_id is None or scene_id in seen_ids:
            continue
        seen_ids.add(scene_id)
        name = row.get("groupName")
        label = None
        if isinstance(name, str):
            label = " ".join(name.split())[:_LABEL_MAX] or None
        found.append((scene_id, label, res))
    bases = [label or f"Scene {scene_id.split('-', 1)[1]}" for scene_id, label, _ in found]
    counts: dict[str, int] = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1
    taken = {b for b in bases if counts[b] == 1}  # unique names keep their text; others get the scene id
    scenes = []
    for (scene_id, _label, res), base in zip(found, bases):
        shown = base
        if counts[base] > 1:
            shown = f"{base} ({scene_id})"
            n = 1
            while shown in taken:  # a real name may already look like a suffixed one
                n += 1
                shown = f"{base} ({scene_id}) #{n}"
            taken.add(shown)
        scenes.append(CloudScene(scene_id, shown, res))
    return scenes


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

    async def fetch_scenes(self, token: str, fid, did) -> list[CloudScene]:
        """Read-only GET of the saved cloud scenes of one strip (first page, cid 0, limit 50)."""
        fid, did = _path_id(fid, "family id"), _path_id(did, "device id")
        headers = self._headers(token)
        path = SCENE_LIST_PATH.format(fid=fid, did=did, timestamp=headers["Timestamp"])
        return _parse_scenes(await self._json("GET", path, headers=headers), fid, did)
