"""Sensor platform for Paketverfolgung providers."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    AMAZON_STATUS_ICONS,
    DEFAULT_ICON,
    DEFAULT_STATUS,
    DOMAIN,
    PROGRESS_ICONS,
    PROGRESS_OUT_FOR_DELIVERY,
    PROGRESS_STATUS,
    TRACKING_PAGE_URL,
)
from .coordinator import AmazonDataUpdateCoordinator, DhlDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    dhl: DhlDataUpdateCoordinator = runtime["dhl"]
    amazon: AmazonDataUpdateCoordinator | None = runtime.get("amazon")

    dhl_known: set[str] = set()
    dhl_entities: dict[str, DhlShipmentSensor] = {}

    @callback
    def _sync_dhl() -> None:
        current_ids = set(dhl.data or {})
        new_ids = current_ids - dhl_known
        if new_ids:
            new_entities = [DhlShipmentSensor(dhl, entry.entry_id, shipment_id) for shipment_id in new_ids]
            for entity in new_entities:
                dhl_entities[entity.shipment_id] = entity
            dhl_known.update(new_ids)
            async_add_entities(new_entities)
        for shipment_id in dhl_known - current_ids:
            entity = dhl_entities.pop(shipment_id, None)
            dhl_known.discard(shipment_id)
            if entity is not None:
                hass.async_create_task(entity.async_remove())

    entry.async_on_unload(dhl.async_add_listener(_sync_dhl))
    _sync_dhl()
    async_add_entities([DhlOutForDeliveryTodaySensor(dhl, entry.entry_id)])

    if amazon is not None:
        amazon_known: set[str] = set()
        amazon_entities: dict[str, AmazonShipmentSensor] = {}
        registry = er.async_get(hass)
        amazon_unique_prefix = f"{entry.entry_id}_amazon_"

        @callback
        def _sync_amazon() -> None:
            current_ids = set(amazon.data or {})
            new_ids = current_ids - amazon_known
            if new_ids:
                new_entities = [
                    AmazonShipmentSensor(amazon, entry.entry_id, shipment_id)
                    for shipment_id in new_ids
                ]
                for entity in new_entities:
                    amazon_entities[entity.shipment_id] = entity
                amazon_known.update(new_ids)
                async_add_entities(new_entities)
            for shipment_id in amazon_known - current_ids:
                entity = amazon_entities.pop(shipment_id, None)
                amazon_known.discard(shipment_id)
                if entity is not None:
                    hass.async_create_task(entity.async_remove())

            # After a successful Amazon refresh, remove registry entries from older
            # parser versions that are no longer returned (e.g. internal shipmentId
            # placeholders without a real tracking number).
            if amazon.last_update_success:
                for registry_entry in er.async_entries_for_config_entry(
                    registry, entry.entry_id
                ):
                    unique_id = registry_entry.unique_id
                    if not unique_id.startswith(amazon_unique_prefix):
                        continue
                    shipment_id = unique_id[len(amazon_unique_prefix):]
                    if shipment_id not in current_ids:
                        registry.async_remove(registry_entry.entity_id)

        entry.async_on_unload(amazon.async_add_listener(_sync_amazon))
        _sync_amazon()


class DhlShipmentSensor(CoordinatorEntity[DhlDataUpdateCoordinator], SensorEntity):
    """Represents a single DHL shipment."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DhlDataUpdateCoordinator, entry_id: str, shipment_id: str) -> None:
        super().__init__(coordinator)
        self.shipment_id = shipment_id
        # Keep the historic unique id unchanged so existing DHL entities survive the upgrade.
        self._attr_unique_id = f"{entry_id}_{shipment_id}"

    @property
    def _shipment(self) -> dict:
        return (self.coordinator.data or {}).get(self.shipment_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.shipment_id in (self.coordinator.data or {})

    @property
    def name(self) -> str:
        info = self._shipment.get("sendungsinfo", {})
        return info.get("sendungsname") or f"DHL {self.shipment_id}"

    @property
    def native_value(self) -> str:
        details = self._shipment.get("sendungsdetails", {})
        verlauf = details.get("sendungsverlauf", {})
        status = verlauf.get("kurzStatus") or verlauf.get("status")
        if status:
            return status
        return PROGRESS_STATUS.get(verlauf.get("fortschritt"), DEFAULT_STATUS)

    @property
    def icon(self) -> str:
        fortschritt = self._shipment.get("sendungsdetails", {}).get("sendungsverlauf", {}).get("fortschritt")
        return PROGRESS_ICONS.get(fortschritt, DEFAULT_ICON)

    @property
    def extra_state_attributes(self) -> dict:
        info = self._shipment.get("sendungsinfo", {})
        details = self._shipment.get("sendungsdetails", {})
        verlauf = details.get("sendungsverlauf", {})
        zustellung = details.get("zustellung", {})
        events = [
            {"datum": event.get("datum"), "status": event.get("status")}
            for event in verlauf.get("events", [])
            if event.get("status")
        ]
        events.reverse()
        return {
            "provider": "dhl",
            "tracking_id": self.shipment_id,
            "progress": verlauf.get("fortschritt"),
            "direction": info.get("sendungsrichtung"),
            "delivery_window_from": zustellung.get("zustellzeitfensterVon"),
            "delivery_window_to": zustellung.get("zustellzeitfensterBis"),
            "tracking_url": TRACKING_PAGE_URL.format(id=self.shipment_id),
            "events": events,
        }


class AmazonShipmentSensor(CoordinatorEntity[AmazonDataUpdateCoordinator], SensorEntity):
    """Represents one Amazon delivery independently from carrier sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AmazonDataUpdateCoordinator, entry_id: str, shipment_id: str) -> None:
        super().__init__(coordinator)
        self.shipment_id = shipment_id
        self._attr_unique_id = f"{entry_id}_amazon_{shipment_id}"

    @property
    def _shipment(self) -> dict:
        return (self.coordinator.data or {}).get(self.shipment_id, {})

    @property
    def available(self) -> bool:
        return super().available and self.shipment_id in (self.coordinator.data or {})

    @property
    def name(self) -> str:
        return f"Amazon {self.shipment_id}"

    @property
    def native_value(self) -> str:
        return self._shipment.get("status") or DEFAULT_STATUS

    @property
    def icon(self) -> str:
        return AMAZON_STATUS_ICONS.get(self._shipment.get("short_status"), "mdi:amazon")

    @property
    def extra_state_attributes(self) -> dict:
        shipment = self._shipment
        return {
            "provider": "amazon",
            "description": shipment.get("name"),
            "tracking_id": shipment.get("tracking_id"),
            "order_id": shipment.get("order_id"),
            "carrier": shipment.get("carrier"),
            "short_status": shipment.get("short_status"),
            "tracking_url": shipment.get("tracking_url"),
        }


class DhlOutForDeliveryTodaySensor(CoordinatorEntity[DhlDataUpdateCoordinator], SensorEntity):
    """Counts DHL shipments currently out for delivery."""

    _attr_has_entity_name = True
    _attr_name = "Heute in Zustellung"
    _attr_icon = "mdi:truck-delivery"
    _attr_native_unit_of_measurement = "Sendungen"

    def __init__(self, coordinator: DhlDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_out_for_delivery_today"

    @property
    def _out_for_delivery(self) -> list[dict]:
        return [
            shipment
            for shipment in (self.coordinator.data or {}).values()
            if shipment.get("sendungsdetails", {}).get("sendungsverlauf", {}).get("fortschritt")
            == PROGRESS_OUT_FOR_DELIVERY
        ]

    @property
    def native_value(self) -> int:
        return len(self._out_for_delivery)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "shipments": [
                {
                    "tracking_id": shipment["id"],
                    "status": shipment.get("sendungsdetails", {}).get("sendungsverlauf", {}).get("status"),
                    "tracking_url": TRACKING_PAGE_URL.format(id=shipment["id"]),
                }
                for shipment in self._out_for_delivery
            ]
        }
