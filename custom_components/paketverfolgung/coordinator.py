"""DataUpdateCoordinators for Paketverfolgung providers."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from yarl import URL

from .amazon_api import AmazonApiClient, AmazonApiError, AmazonAuthError
from .const import (
    CONF_AMAZON_COOKIES,
    CONF_AMAZON_ENABLED,
    CONF_AUTO_DISCOVERY,
    CONF_DHL_SESSION,
    CONF_TRACKING_NUMBERS,
    DOMAIN,
)
from .dhl_api import DhlApiClient, DhlApiError, DhlAuthError

_LOGGER = logging.getLogger(__name__)


def _load_amazon_cookie_store(client: AmazonApiClient, store: dict) -> bool:
    """Restore domain-scoped Amazon cookies. Return False for legacy stores."""
    if store.get("_format") != "domain_v1":
        return False
    domains = store.get("domains") or {}
    for domain, cookies in domains.items():
        if not isinstance(cookies, dict) or not cookies:
            continue
        client._jar.update_cookies(cookies, response_url=URL(f"https://{domain}/"))
    return True


def _export_amazon_cookie_store(client: AmazonApiClient) -> dict:
    """Persist the exact cookie sets sent to Amazon's two relevant hosts."""
    domains: dict[str, dict[str, str]] = {}
    for domain in ("amazon.de", "www.amazon.de"):
        domains[domain] = {
            name: morsel.value
            for name, morsel in client._jar.filter_cookies(URL(f"https://{domain}/")).items()
        }
    return {"_format": "domain_v1", "domains": domains}


class DhlDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetch details for manually tracked and account-discovered DHL shipments."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_dhl", update_interval=update_interval)
        self.entry = entry
        self.client = DhlApiClient(async_get_clientsession(hass))
        self.options_snapshot = dict(entry.options)

    async def _async_update_data(self) -> dict[str, dict]:
        tracking_numbers = list(
            self.entry.options.get(
                CONF_TRACKING_NUMBERS,
                self.entry.data.get(CONF_TRACKING_NUMBERS, []),
            )
        )
        auto_discovery = self.entry.options.get(
            CONF_AUTO_DISCOVERY,
            self.entry.data.get(CONF_AUTO_DISCOVERY, False),
        )

        try:
            if auto_discovery:
                session = self.entry.data.get(CONF_DHL_SESSION)
                if not session:
                    raise DhlAuthError("DHL auto-discovery is enabled but no session exists")
                refreshed = await self.client.refresh_session(dict(session))
                if refreshed != session:
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={**self.entry.data, CONF_DHL_SESSION: refreshed},
                    )
                    session = refreshed
                account_numbers = await self.client.fetch_account_tracking_numbers(session)
                for shipment_id in account_numbers:
                    if shipment_id not in tracking_numbers:
                        tracking_numbers.append(shipment_id)
            shipments = await self.client.fetch_shipments(tracking_numbers)
        except DhlAuthError as err:
            raise UpdateFailed(f"DHL account authentication error: {err}") from err
        except DhlApiError as err:
            raise UpdateFailed(f"Error fetching DHL shipments: {err}") from err

        _LOGGER.debug("DHL: %d shipment(s) fetched", len(shipments))
        return {shipment["id"]: shipment for shipment in shipments if shipment.get("id")}


class AmazonDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Fetch active Amazon deliveries with a persisted cookie session."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_amazon", update_interval=update_interval)
        self.entry = entry

    async def _async_update_data(self) -> dict[str, dict]:
        enabled = self.entry.options.get(
            CONF_AMAZON_ENABLED, self.entry.data.get(CONF_AMAZON_ENABLED, False)
        )
        if not enabled:
            return {}
        cookies = self.entry.data.get(CONF_AMAZON_COOKIES) or {}
        if not cookies:
            raise UpdateFailed("Amazon is enabled but no authenticated session exists")

        client = AmazonApiClient()
        if not _load_amazon_cookie_store(client, cookies):
            await client.close()
            raise UpdateFailed("Amazon session uses an old cookie format; login required")

        try:
            shipments, _ = await client.fetch_shipments()
            refreshed_cookies = _export_amazon_cookie_store(client)
        except AmazonAuthError as err:
            raise UpdateFailed(f"Amazon authentication error: {err}") from err
        except AmazonApiError as err:
            raise UpdateFailed(f"Error fetching Amazon shipments: {err}") from err
        finally:
            await client.close()

        if refreshed_cookies != cookies:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_AMAZON_COOKIES: refreshed_cookies},
            )
        _LOGGER.debug("Amazon: %d shipment(s) fetched", len(shipments))
        return {shipment["id"]: shipment for shipment in shipments if shipment.get("id")}
