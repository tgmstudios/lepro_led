"""Lepro LED integration."""
from __future__ import annotations

import copy
import hashlib
import math
import logging
import os
import ssl

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed, ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LeproApi, LeproApiError, LeproAuthError, make_fcm_token, make_mac
from .const import (
    CONF_ACCOUNT,
    CONF_FCM,
    CONF_LANGUAGE,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    SERVICE_APPLY_CLOUD_SCENE,
    SERVICE_REFRESH_CLOUD_SCENES,
    SERVICE_SET_SCENE,
    SERVICE_SET_SEGMENTS,
    SERVICES,
    canonical_unique_id,
)
from .state import SCENE_EFFECTS as SCENE_EFFECT_NAMES
from .runtime import LeproRuntime
from .transport_mqtt import CloudMqttTransport

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "number", "binary_sensor", "select"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
type LeproConfigEntry = ConfigEntry[LeproRuntime]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


def _write_and_build_ssl(root_path, cert_path, key_path, root: bytes, cert: bytes) -> ssl.SSLContext:
    os.makedirs(os.path.dirname(root_path), exist_ok=True)
    for path, data in ((root_path, root), (cert_path, cert)):
        with open(path, "wb") as fh:
            fh.write(data)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=root_path)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 adds `region`; entries created by this fork were always North America."""
    if entry.version == 1 and entry.minor_version < 2:
        data = {**entry.data}
        data.setdefault(CONF_REGION, DEFAULT_REGION)
        kwargs = {}
        if entry.unique_id is None and isinstance(data.get(CONF_ACCOUNT), str):
            uid = canonical_unique_id(data[CONF_ACCOUNT], data[CONF_REGION])
            if any(e.entry_id != entry.entry_id and e.unique_id == uid for e in hass.config_entries.async_entries(DOMAIN)):
                # never merge/steal: leave this entry as-is (no unique_id) and let the user remove one
                _LOGGER.warning("Another Lepro entry already uses the same account/region; leaving this legacy entry without a unique id")
            else:
                kwargs["unique_id"] = uid
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2, **kwargs)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LeproConfigEntry) -> bool:
    data = dict(entry.data)
    updates = {}
    if CONF_MAC not in data:
        updates[CONF_MAC] = make_mac(data[CONF_ACCOUNT])
    if CONF_FCM not in data:
        updates[CONF_FCM] = make_fcm_token()
    if updates:
        data.update(updates)
        hass.config_entries.async_update_entry(entry, data=data)

    api = LeproApi(
        async_get_clientsession(hass),
        data.get(CONF_REGION, DEFAULT_REGION),
        data[CONF_ACCOUNT],
        data[CONF_PASSWORD],
        data.get(CONF_LANGUAGE, "en"),
        data[CONF_MAC],
        data[CONF_FCM],
    )
    try:
        session, root, cert = await api.fetch_session()
    except LeproAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except LeproApiError as err:
        raise ConfigEntryNotReady(str(err)) from err
    if not session.devices:
        raise ConfigEntryNotReady("No devices found in Lepro account")

    cert_dir = os.path.join(hass.config.config_dir, ".lepro_led")
    key_path = os.path.join(os.path.dirname(__file__), "client_key.pem")
    try:
        ssl_ctx = await hass.async_add_executor_job(
            _write_and_build_ssl,
            os.path.join(cert_dir, f"{entry.entry_id}_root_ca.pem"),
            os.path.join(cert_dir, f"{entry.entry_id}_client_cert.pem"),
            key_path,
            root,
            cert,
        )
    except (OSError, ssl.SSLError) as err:
        raise ConfigEntryNotReady(f"TLS setup failed: {type(err).__name__}") from err

    runtime = LeproRuntime(session.devices)
    suffix = hashlib.sha256(entry.entry_id.encode()).hexdigest()[:32]
    topics = [f"le/{suffix}/act/app/exe"] + [f"le/{did}/prp/#" for did in runtime.devices]
    runtime.transport = CloudMqttTransport(
        session.mqtt.host,
        session.mqtt.port,
        ssl_ctx,
        f"lepro-app-{suffix}",
        topics,
        runtime.handle_message,
        runtime.handle_status,
    )
    runtime.api, runtime.token = api, session.token
    entry.runtime_data = runtime
    await runtime.transport.start()
    if not await runtime.transport.async_wait_connected(15):
        await runtime.async_shutdown()
        raise ConfigEntryNotReady("MQTT broker not reachable")

    for did in runtime.devices:  # optional catalog: a failure must never break the basic light integration
        try:
            await runtime.refresh_scenes(did)
        except LeproApiError as err:
            _LOGGER.debug("cloud scene catalog unavailable: %s", err)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await runtime.async_shutdown()
        raise
    _register_services(hass)
    return True


def _strict_int(lo: int, hi: int):
    def check(v):
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise vol.Invalid(f"must be an integer {lo}..{hi}")
        return v

    return check


def _strict_number(lo: float, hi: float):
    def check(v):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
            raise vol.Invalid(f"must be a number {lo}..{hi}")
        return v

    return check


_SCENE_FIELDS = ("effect", "colors", "speed", "brightness", "brightness_pct", "direction")


def _scene_cross_check(data: dict) -> dict:
    given = [k for k in _SCENE_FIELDS if k in data]
    if not given:
        raise vol.Invalid("provide at least one of " + ", ".join(_SCENE_FIELDS))
    if "brightness" in data and "brightness_pct" in data:
        raise vol.Invalid("use either brightness or brightness_pct, not both")
    return data


_STRICT_RGB = vol.All(vol.ExactSequence([_strict_int(0, 255)] * 3), vol.Coerce(tuple))
SCENE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_ids,
            vol.Optional("effect"): vol.In(SCENE_EFFECT_NAMES),
            vol.Optional("colors"): vol.All(list, [_STRICT_RGB], vol.Length(min=1, max=25)),
            vol.Optional("speed"): _strict_number(0, 100),
            vol.Optional("brightness"): _strict_int(1, 255),
            vol.Optional("brightness_pct"): _strict_number(1, 100),
            vol.Optional("direction"): vol.In(("clockwise", "counterclockwise")),
        }
    ),
    _scene_cross_check,
)


def _resolve_device(hass: HomeAssistant, entity_id: str):
    """Map a Lepro light entity (strip or segment) to (runtime, did), or None."""
    from homeassistant.helpers import entity_registry as er

    ent = er.async_get(hass).async_get(entity_id)
    if ent is None or ent.platform != DOMAIN or ent.domain != "light" or ent.config_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(ent.config_entry_id)
    if entry is None or not hasattr(entry, "runtime_data"):
        return None
    did = ent.unique_id.split("_segment_")[0]
    if did not in entry.runtime_data.devices:
        return None
    return entry.runtime_data, did


def _register_services(hass: HomeAssistant) -> None:
    _register_set_segments(hass)
    _register_set_scene(hass)
    _register_cloud_scene_services(hass)


def _register_set_scene(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCENE):
        return

    async def handle(call: ServiceCall) -> None:
        targets: dict = {}
        for entity_id in call.data["entity_id"]:
            found = _resolve_device(hass, entity_id)
            if found is None:
                raise ServiceValidationError(f"{entity_id} is not a loaded Lepro light")
            rt, did = found
            targets[(id(rt), did)] = (rt, did)  # strip + its segment lights collapse to one command
        args = {k: call.data[k] for k in _SCENE_FIELDS if k in call.data}
        args["colors"] = call.data.get("colors")
        args = {k: v for k, v in args.items() if v is not None}
        # dry-run on every device first so one bad device cannot leave the others half-updated
        for rt, did in targets.values():
            try:
                copy.deepcopy(rt.devices[did].state).command_scene(**args)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
        for rt, did in targets.values():
            await rt.send(did, lambda s: s.command_scene(**args))

    hass.services.async_register(DOMAIN, SERVICE_SET_SCENE, handle, schema=SCENE_SCHEMA)


APPLY_CLOUD_SCENE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("scene_id"): vol.All(str, vol.Length(min=1, max=80)),
            vol.Optional("brightness"): _strict_int(1, 255),
            vol.Optional("brightness_pct"): _strict_number(1, 100),
        }
    ),
    lambda d: d if not ("brightness" in d and "brightness_pct" in d) else (_ for _ in ()).throw(vol.Invalid("use either brightness or brightness_pct, not both")),
)
REFRESH_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_ids})


def _unique_targets(hass: HomeAssistant, entity_ids) -> dict:
    targets: dict = {}
    for entity_id in entity_ids:
        found = _resolve_device(hass, entity_id)
        if found is None:
            raise ServiceValidationError(f"{entity_id} is not a loaded Lepro light")
        targets[(id(found[0]), found[1])] = found
    return targets


def _register_cloud_scene_services(hass: HomeAssistant) -> None:
    if not hass.services.has_service(DOMAIN, SERVICE_APPLY_CLOUD_SCENE):

        async def apply(call: ServiceCall) -> None:
            targets = _unique_targets(hass, call.data["entity_id"])
            args = {k: call.data[k] for k in ("brightness", "brightness_pct") if k in call.data}
            plan = []
            for rt, did in targets.values():
                scene = rt.scenes.get(did, {}).get(call.data["scene_id"])
                if scene is None:
                    raise ServiceValidationError("unknown cloud scene for this strip; refresh the catalog or pick a listed scene")
                try:
                    copy.deepcopy(rt.devices[did].state).command_cloud_scene(scene.d50, **args)
                except ValueError as err:
                    raise ServiceValidationError(str(err)) from err
                plan.append((rt, did, scene))
            for rt, did, scene in plan:
                await rt.send(did, lambda s, d50=scene.d50: s.command_cloud_scene(d50, **args))

        hass.services.async_register(DOMAIN, SERVICE_APPLY_CLOUD_SCENE, apply, schema=APPLY_CLOUD_SCENE_SCHEMA)

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_CLOUD_SCENES):

        async def refresh(call: ServiceCall) -> None:
            for rt, did in _unique_targets(hass, call.data["entity_id"]).values():
                try:
                    await rt.refresh_scenes(did)
                except LeproApiError as err:
                    raise HomeAssistantError(f"Could not read Lepro cloud scenes: {err}") from err

        hass.services.async_register(DOMAIN, SERVICE_REFRESH_CLOUD_SCENES, refresh, schema=REFRESH_SCHEMA)


def _register_set_segments(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_SEGMENTS):
        return

    async def handle(call: ServiceCall) -> None:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        for entity_id in call.data["entity_id"]:
            ent = reg.async_get(entity_id)
            if ent is None or ent.platform != DOMAIN or ent.config_entry_id is None:
                continue
            entry = hass.config_entries.async_get_entry(ent.config_entry_id)
            if entry is None or not hasattr(entry, "runtime_data"):
                continue
            did = ent.unique_id.split("_segment_")[0]
            if did not in entry.runtime_data.devices:
                continue
            colors = [tuple(c) for c in call.data["colors"]]
            await entry.runtime_data.send(did, lambda s: s.command_segments(colors))

    rgb = vol.All(vol.ExactSequence([cv.byte, cv.byte, cv.byte]), vol.Coerce(tuple))
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SEGMENTS,
        handle,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_ids, vol.Required("colors"): vol.All(cv.ensure_list, [rgb], vol.Length(min=1, max=25))}),
    )


async def async_unload_entry(hass: HomeAssistant, entry: LeproConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
        if not any(e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED for e in hass.config_entries.async_entries(DOMAIN)):
            for service in SERVICES:
                hass.services.async_remove(DOMAIN, service)
    return ok
