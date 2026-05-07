"""Support for Hoymiles number sensors."""

import dataclasses
from dataclasses import dataclass
from enum import Enum
import logging
import time

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_DTU_SERIAL_NUMBER,
    CONF_INVERTERS,
    CONF_THREE_PHASE_INVERTERS,
    DOMAIN,
    HASS_CONFIG_COORDINATOR,
    HASS_DATA_COORDINATOR,
)
from .entity import HoymilesCoordinatorEntity, HoymilesEntityDescription

from hoymiles_wifi.hoymiles import DTUType, get_dtu_model_type


class SetAction(Enum):
    """Enum for set actions."""

    POWER_LIMIT = 1


@dataclass(frozen=True)
class HoymilesNumberSensorEntityDescriptionMixin:
    """Mixin for required keys."""


@dataclass(frozen=True)
class HoymilesNumberSensorEntityDescription(
    HoymilesEntityDescription, NumberEntityDescription
):
    """Describes Hoymiles number sensor entity."""

    set_action: SetAction = None
    conversion_factor: float = None
    serial_number: str = None
    is_dtu_sensor: bool = False


CONFIG_CONTROL_ENTITIES = (
    HoymilesNumberSensorEntityDescription(
        key="limit_power_mypower",
        translation_key="limit_power_mypower",
        mode=NumberMode.SLIDER,
        device_class=NumberDeviceClass.POWER_FACTOR,
        set_action=SetAction.POWER_LIMIT,
        conversion_factor=0.1,
        is_dtu_sensor=True,
    ),
)

_LOGGER = logging.getLogger(__name__)
ZERO_LIMIT_SET_GRACE_SECONDS = 10 * 60


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hoymiles number entities."""
    hass_data = hass.data[DOMAIN][config_entry.entry_id]
    config_coordinator = hass_data.get(HASS_CONFIG_COORDINATOR, None)
    data_coordinator = hass_data.get(HASS_DATA_COORDINATOR, None)
    coordinator = data_coordinator or config_coordinator
    single_phase_inverters = config_entry.data.get(CONF_INVERTERS, [])
    three_phase_inverters = config_entry.data.get(CONF_THREE_PHASE_INVERTERS, [])
    dtu_serial_number = config_entry.data[CONF_DTU_SERIAL_NUMBER]

    if coordinator and (single_phase_inverters or three_phase_inverters):
        sensors = []
        for description in CONFIG_CONTROL_ENTITIES:
            if description.is_dtu_sensor is True:
                updated_description = dataclasses.replace(
                    description, serial_number=dtu_serial_number
                )
                sensors.append(
                    HoymilesNumberEntity(
                        config_entry, updated_description, coordinator
                    )
                )
        async_add_entities(sensors)


class HoymilesNumberEntity(HoymilesCoordinatorEntity, NumberEntity, RestoreEntity):
    """Hoymiles Number entity."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        description: HoymilesNumberSensorEntityDescription,
        coordinator: HoymilesCoordinatorEntity,
    ) -> None:
        """Initialize the HoymilesNumberEntity."""
        super().__init__(config_entry, description, coordinator)
        self._attribute_name = description.key
        self._conversion_factor = description.conversion_factor
        self._set_action = description.set_action
        self._native_value = None
        self._last_nonzero_native_value = None
        self._accept_zero_until = 0.0
        self._assumed_state = False

        self.update_state_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.update_state_value()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        """Get the native value of the entity."""
        return self._native_value

    @property
    def assumed_state(self):
        """Return the assumed state of the entity."""
        return self._assumed_state

    async def async_added_to_hass(self) -> None:
        """Restore the last valid value to survive DTU sleep/startup zeros."""
        await super().async_added_to_hass()

        if self._native_value is not None:
            self._remember_nonzero_value(self._native_value)
            return

        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        try:
            restored_value = float(last_state.state)
        except (TypeError, ValueError):
            return

        if restored_value >= 0:
            self._native_value = restored_value
        if restored_value > 0:
            self._last_nonzero_native_value = restored_value

    async def async_set_native_value(self, value: float) -> None:
        """Set the native value of the entity.

        Args:
            value (float): The value to set.
        """
        if self._set_action == SetAction.POWER_LIMIT:
            dtu = self.coordinator.get_dtu()
            if value < 0 or value > 100:
                _LOGGER.error("Power limit value out of range")
                return
            await dtu.async_set_power_limit(value)
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error("Invalid set action!")
            return

        self._assumed_state = True
        self._native_value = value
        if value == 0:
            self._accept_zero_until = time.monotonic() + ZERO_LIMIT_SET_GRACE_SECONDS
        else:
            self._accept_zero_until = 0.0
        self._remember_nonzero_value(value)

    def update_state_value(self):
        """Update the state value of the entity."""

        native_value = None

        if self._set_action == SetAction.POWER_LIMIT:
            # DTU-Lite does not answer get_config(), but real_data_new reports the
            # active limit per inverter as tenths of a percent.
            for data_group_name in ("sgs_data", "tgs_data"):
                data_group = getattr(self.coordinator.data, data_group_name, [])
                for inverter_data in data_group:
                    power_limit = getattr(inverter_data, "power_limit", None)
                    if power_limit is not None:
                        native_value = power_limit
                        break
                if native_value is not None:
                    break

        if native_value is None:
            native_value = getattr(
                self.coordinator.data,
                self._attribute_name,
                None,
            )

        self._assumed_state = False

        if native_value is not None and self._conversion_factor is not None:
            native_value *= self._conversion_factor

        # During night/morning startup DTU-Lite/WLite-S can briefly report a
        # protobuf power_limit of 0 although the configured limit is unchanged.
        # Keep the last non-zero value instead of showing a bogus 0.0%.
        if (
            self._set_action == SetAction.POWER_LIMIT
            and native_value == 0
            and self._last_nonzero_native_value is not None
            and time.monotonic() > self._accept_zero_until
        ):
            native_value = self._last_nonzero_native_value

        self._native_value = native_value
        self._remember_nonzero_value(native_value)

    def _remember_nonzero_value(self, value: float | None) -> None:
        """Remember a valid non-zero value for DTU wake-up zero fallback."""
        if value is not None and value > 0:
            self._last_nonzero_native_value = value
