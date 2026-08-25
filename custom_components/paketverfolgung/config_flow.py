"""Config flow for the Paketverfolgung integration."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .amazon_api import (
    AmazonApiClient,
    AmazonAuthError,
    AmazonCaptchaError,
    AmazonOtpChallenge,
)
from .const import (
    CONF_AMAZON_COOKIES,
    CONF_AMAZON_ENABLED,
    CONF_AMAZON_OTP,
    CONF_AMAZON_PASSWORD,
    CONF_AMAZON_USERNAME,
    CONF_AUTO_DISCOVERY,
    CONF_DHL_REDIRECT,
    CONF_DHL_SESSION,
    CONF_TRACKING_NUMBERS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DHL_AUTH_BASE,
    DHL_CLIENT_ID,
    DHL_CODE_CHALLENGE,
    DHL_CODE_VERIFIER,
    DHL_LOGIN_CLAIMS,
    DHL_LOGIN_STATE,
    DHL_REDIRECT_URI,
    DOMAIN,
    MIN_UPDATE_INTERVAL_MINUTES,
)
from .dhl_api import DhlApiClient, DhlAuthError, extract_authorization_code


def _clean_tracking_numbers(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    seen: list[str] = []
    for value in raw:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _tracking_numbers_schema(default: list[str]) -> dict:
    return {
        vol.Optional(CONF_TRACKING_NUMBERS, default=default): selector.TextSelector(
            selector.TextSelectorConfig(multiple=True)
        ),
    }


def _dhl_login() -> tuple[str, str]:
    params = {
        "redirect_uri": DHL_REDIRECT_URI,
        "state": DHL_LOGIN_STATE,
        "client_id": DHL_CLIENT_ID,
        "response_type": "code",
        "scope": "openid offline_access",
        "claims": DHL_LOGIN_CLAIMS,
        "nonce": "",
        "login_hint": "",
        "prompt": "login",
        "ui_locales": "de-DE",
        "code_challenge": DHL_CODE_CHALLENGE,
        "code_challenge_method": "S256",
    }
    return DHL_CODE_VERIFIER, f"{DHL_AUTH_BASE}/authorize?{urlencode(params)}"


def _dhl_redirect_schema() -> vol.Schema:
    return vol.Schema(
        {vol.Required(CONF_DHL_REDIRECT): selector.TextSelector(selector.TextSelectorConfig())}
    )


def _amazon_login_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_AMAZON_USERNAME): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
            ),
            vol.Required(CONF_AMAZON_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _amazon_otp_schema() -> vol.Schema:
    return vol.Schema(
        {vol.Required(CONF_AMAZON_OTP): selector.TextSelector(selector.TextSelectorConfig())}
    )


def _export_amazon_cookie_store(client: AmazonApiClient) -> dict:
    """Store cookies separately for amazon.de and www.amazon.de."""
    domains: dict[str, dict[str, str]] = {}
    for domain in ("amazon.de", "www.amazon.de"):
        domains[domain] = {
            name: morsel.value
            for name, morsel in client._jar.filter_cookies(URL(f"https://{domain}/")).items()
        }
    return {"_format": "domain_v1", "domains": domains}


def _amazon_cookie_store_is_current(store: Any) -> bool:
    return isinstance(store, dict) and store.get("_format") == "domain_v1"


class PaketverfolgungConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paketverfolgung."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_data: dict[str, Any] = {}
        self._pkce_verifier: str | None = None
        self._login_url: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            auto_discovery = bool(user_input.get(CONF_AUTO_DISCOVERY, False))
            self._pending_data = {
                CONF_TRACKING_NUMBERS: numbers,
                CONF_AUTO_DISCOVERY: auto_discovery,
                CONF_AMAZON_ENABLED: False,
            }
            if auto_discovery:
                self._pkce_verifier, self._login_url = _dhl_login()
                return await self.async_step_dhl_login()
            return await self._create_entry(self._pending_data)

        schema = vol.Schema(
            {
                **_tracking_numbers_schema([]),
                vol.Optional(CONF_AUTO_DISCOVERY, default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_dhl_login(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = extract_authorization_code(user_input[CONF_DHL_REDIRECT].strip())
                client = DhlApiClient(async_get_clientsession(self.hass))
                session = await client.exchange_code(code, self._pkce_verifier or DHL_CODE_VERIFIER)
            except DhlAuthError:
                errors["base"] = "dhl_auth"
            else:
                return await self._create_entry({**self._pending_data, CONF_DHL_SESSION: session})
        if not self._login_url:
            self._pkce_verifier, self._login_url = _dhl_login()
        return self.async_show_form(
            step_id="dhl_login",
            data_schema=_dhl_redirect_schema(),
            errors=errors,
            description_placeholders={"login_url": self._login_url},
        )

    async def _create_entry(self, data: dict[str, Any]) -> Any:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Paketverfolgung", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PaketverfolgungOptionsFlow(entry)


class PaketverfolgungOptionsFlow(OptionsFlow):
    """Manage DHL and Amazon providers plus the update interval."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._pending_options: dict[str, Any] = {}
        self._pkce_verifier: str | None = None
        self._login_url: str | None = None
        self._amazon_challenge: AmazonOtpChallenge | None = None

    def _amazon_needs_login(self) -> bool:
        """Return True for missing/legacy cookies or a rejected Amazon session."""
        store = self._entry.data.get(CONF_AMAZON_COOKIES)
        if not _amazon_cookie_store_is_current(store):
            return True
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id) or {}
        coordinator = runtime.get("amazon") if isinstance(runtime, dict) else None
        return coordinator is not None and not coordinator.last_update_success

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            numbers = _clean_tracking_numbers(user_input.get(CONF_TRACKING_NUMBERS))
            auto_discovery = bool(user_input.get(CONF_AUTO_DISCOVERY, False))
            amazon_enabled = bool(user_input.get(CONF_AMAZON_ENABLED, False))
            self._pending_options = {
                CONF_TRACKING_NUMBERS: numbers,
                CONF_UPDATE_INTERVAL: user_input[CONF_UPDATE_INTERVAL],
                CONF_AUTO_DISCOVERY: auto_discovery,
                CONF_AMAZON_ENABLED: amazon_enabled,
            }
            if auto_discovery and not self._entry.data.get(CONF_DHL_SESSION):
                self._pkce_verifier, self._login_url = _dhl_login()
                return await self.async_step_dhl_login()
            if amazon_enabled and self._amazon_needs_login():
                return await self.async_step_amazon_login()
            return self.async_create_entry(title="", data=self._pending_options)

        current_numbers = self._entry.options.get(
            CONF_TRACKING_NUMBERS, self._entry.data.get(CONF_TRACKING_NUMBERS, [])
        )
        current_interval = self._entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        current_auto = self._entry.options.get(
            CONF_AUTO_DISCOVERY, self._entry.data.get(CONF_AUTO_DISCOVERY, False)
        )
        current_amazon = self._entry.options.get(
            CONF_AMAZON_ENABLED, self._entry.data.get(CONF_AMAZON_ENABLED, False)
        )
        schema = vol.Schema(
            {
                **_tracking_numbers_schema(current_numbers),
                vol.Required(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL_MINUTES)
                ),
                vol.Optional(CONF_AUTO_DISCOVERY, default=current_auto): bool,
                vol.Optional(CONF_AMAZON_ENABLED, default=current_amazon): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_dhl_login(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = extract_authorization_code(user_input[CONF_DHL_REDIRECT].strip())
                client = DhlApiClient(async_get_clientsession(self.hass))
                session = await client.exchange_code(code, self._pkce_verifier or DHL_CODE_VERIFIER)
            except DhlAuthError:
                errors["base"] = "dhl_auth"
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry, data={**self._entry.data, CONF_DHL_SESSION: session}
                )
                if self._pending_options.get(CONF_AMAZON_ENABLED) and self._amazon_needs_login():
                    return await self.async_step_amazon_login()
                return self.async_create_entry(title="", data=self._pending_options)
        if not self._login_url:
            self._pkce_verifier, self._login_url = _dhl_login()
        return self.async_show_form(
            step_id="dhl_login",
            data_schema=_dhl_redirect_schema(),
            errors=errors,
            description_placeholders={"login_url": self._login_url},
        )

    async def async_step_amazon_login(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = AmazonApiClient()
            try:
                result = await client.login(
                    user_input[CONF_AMAZON_USERNAME].strip(),
                    user_input[CONF_AMAZON_PASSWORD],
                )
            except AmazonCaptchaError:
                errors["base"] = "amazon_captcha"
            except AmazonAuthError:
                errors["base"] = "amazon_auth"
            else:
                if result.otp is not None:
                    self._amazon_challenge = result.otp
                    return await self.async_step_amazon_otp()
                if result.cookies:
                    self.hass.config_entries.async_update_entry(
                        self._entry,
                        data={
                            **self._entry.data,
                            CONF_AMAZON_COOKIES: _export_amazon_cookie_store(client),
                            CONF_AMAZON_ENABLED: True,
                        },
                    )
                    return self.async_create_entry(title="", data=self._pending_options)
            finally:
                await client.close()
        return self.async_show_form(
            step_id="amazon_login", data_schema=_amazon_login_schema(), errors=errors
        )

    async def async_step_amazon_otp(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        if self._amazon_challenge is None:
            return await self.async_step_amazon_login()
        if user_input is not None:
            client = AmazonApiClient(self._amazon_challenge.cookies)
            try:
                await client.submit_otp(
                    self._amazon_challenge, user_input[CONF_AMAZON_OTP].strip()
                )
            except AmazonCaptchaError:
                errors["base"] = "amazon_captcha"
            except AmazonAuthError:
                errors["base"] = "amazon_otp"
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={
                        **self._entry.data,
                        CONF_AMAZON_COOKIES: _export_amazon_cookie_store(client),
                        CONF_AMAZON_ENABLED: True,
                    },
                )
                return self.async_create_entry(title="", data=self._pending_options)
            finally:
                await client.close()
        return self.async_show_form(
            step_id="amazon_otp", data_schema=_amazon_otp_schema(), errors=errors
        )
