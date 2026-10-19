from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
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
    canonical_unique_id,
)

REGION_OPTIONS = [
    selector.SelectOptionDict(value="na", label="North America"),
    selector.SelectOptionDict(value="us", label="United States"),
    selector.SelectOptionDict(value="eu", label="Europe"),
    selector.SelectOptionDict(value="fe", label="Far East"),
]
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): selector.SelectSelector(
            selector.SelectSelectorConfig(options=REGION_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)
        ),
        vol.Optional(CONF_LANGUAGE, default="en"): str,
    }
)


async def _validate(hass, data) -> str | None:
    api = LeproApi(
        async_get_clientsession(hass), data.get(CONF_REGION, DEFAULT_REGION), data[CONF_ACCOUNT],
        data[CONF_PASSWORD], data.get(CONF_LANGUAGE, "en"),
        data.get(CONF_MAC) or make_mac(data[CONF_ACCOUNT]), data.get(CONF_FCM) or "validate",
    )
    try:
        await api.login()
    except LeproAuthError:
        return "invalid_auth"
    except LeproApiError:
        return "cannot_connect"
    return None


class LeproLedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            account = user_input[CONF_ACCOUNT]
            region = user_input.get(CONF_REGION, DEFAULT_REGION)
            await self.async_set_unique_id(canonical_unique_id(account, region))
            self._abort_if_unique_id_configured()
            # legacy entries may have no unique_id yet (migration is not guaranteed to have run)
            if any(
                e.unique_id is None
                and str(e.data.get(CONF_ACCOUNT, "")).lower() == account.lower()
                and e.data.get(CONF_REGION, DEFAULT_REGION) == region
                for e in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            err = await _validate(self.hass, user_input)
            if err is None:
                data = {**user_input, CONF_MAC: make_mac(user_input[CONF_ACCOUNT]), CONF_FCM: make_fcm_token()}
                return self.async_create_entry(title="Lepro LED", data=data)
            errors["base"] = err
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            new = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            err = await _validate(self.hass, new)
            if err is None:
                return self.async_update_reload_and_abort(entry, data=new)
            errors["base"] = err
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors
        )
