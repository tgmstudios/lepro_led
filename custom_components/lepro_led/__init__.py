"""Lepro LED integration."""
from __future__ import annotations

import hashlib
import logging
import os
import ssl

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
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
    SERVICE_SET_SEGMENTS,
    canonical_unique_id,
)
from .runtime import LeproRuntime
from .transport_mqtt import CloudMqttTransport

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "number", "binary_sensor"]
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
    entry.runtime_data = runtime
    await runtime.transport.start()
    if not await runtime.transport.async_wait_connected(15):
        await runtime.async_shutdown()
        raise ConfigEntryNotReady("MQTT broker not reachable")

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await runtime.async_shutdown()
        raise
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
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
            hass.services.async_remove(DOMAIN, SERVICE_SET_SEGMENTS)
    return ok
