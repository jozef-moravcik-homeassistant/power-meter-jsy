from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" sensor.py """

"""Sensor platforma – Modbus Node (error code) a Power Meter (napätia kanálov)."""

import dataclasses
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import *
from .coordinator import PowerMeterCoordinator
from .device_info import current_range_amps

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telemetrické senzory kanálov – "selector": vytvárajú sa podľa profilu modelu.
# Ďalšie senzory sa pridávajú len doplnením položky do tejto n-tice.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class TelemetryDescription:
    key: str                       # ide do unique_id / entity_id / translation_key (channel_<key>)
    offset: int                    # posun registra v rámci kanála
    scale: float
    precision: int
    device_class: SensorDeviceClass
    unit: str | None
    icon: str
    min_value: float
    max_value: float
    requires_voltage: bool = False  # bez platného napätia kanála je entita unavailable
    zero_without_voltage: bool = False  # bez platného napätia kanála je hodnota 0
    # (platné napätie = napätie kanála >= MIN_VOLTAGE_FOR_VALID_MEASUREMENT_V)
    words: int = 1                 # počet 16-bit registrov (2 = 32-bit hodnota, vyšší register prvý)
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT


# Poradie zodpovedá poradiu registrov v Modbus dokumentácii (CH1: 0x0040 až 0x0046, ďalšie kanály +7).
TELEMETRY_SENSORS: tuple[TelemetryDescription, ...] = (
    # 0x0040 – napätie
    TelemetryDescription(
        key="voltage", offset=OFFSET_VOLTAGE, scale=VOLTAGE_SCALE, precision=VOLTAGE_PRECISION,
        device_class=SensorDeviceClass.VOLTAGE, unit=UnitOfElectricPotential.VOLT, icon="mdi:sine-wave",
        min_value=VOLTAGE_MIN_V, max_value=VOLTAGE_MAX_V,
    ),
    # 0x0041 – prúd
    TelemetryDescription(
        key="current", offset=OFFSET_CURRENT, scale=CURRENT_SCALE, precision=CURRENT_PRECISION,
        device_class=SensorDeviceClass.CURRENT, unit=UnitOfElectricCurrent.AMPERE, icon="mdi:current-ac",
        min_value=CURRENT_MIN_A, max_value=CURRENT_MAX_A,
    ),
    # 0x0042 – činný výkon
    TelemetryDescription(
        key="power", offset=OFFSET_POWER, scale=POWER_SCALE, precision=POWER_PRECISION,
        device_class=SensorDeviceClass.POWER, unit=UnitOfPower.WATT, icon="mdi:flash",
        min_value=POWER_MIN_W, max_value=POWER_MAX_W,
    ),
    # 0x0043 ~ 0x0044 – celková činná energia (32-bit)
    TelemetryDescription(
        key="energy", offset=OFFSET_ENERGY, scale=ENERGY_SCALE, precision=ENERGY_PRECISION,
        device_class=SensorDeviceClass.ENERGY, unit=UnitOfEnergy.KILO_WATT_HOUR, icon="mdi:counter",
        min_value=ENERGY_MIN_KWH, max_value=ENERGY_MAX_KWH, words=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # 0x0045 – cos φ
    TelemetryDescription(
        key="power_factor", offset=OFFSET_POWER_FACTOR, scale=POWER_FACTOR_SCALE, precision=POWER_FACTOR_PRECISION,
        device_class=SensorDeviceClass.POWER_FACTOR, unit=None, icon="mdi:angle-acute",
        min_value=POWER_FACTOR_MIN, max_value=POWER_FACTOR_MAX,
    ),
    # 0x0046 – frekvencia
    TelemetryDescription(
        key="frequency", offset=OFFSET_FREQUENCY, scale=FREQUENCY_SCALE, precision=FREQUENCY_PRECISION,
        device_class=SensorDeviceClass.FREQUENCY, unit=UnitOfFrequency.HERTZ, icon="mdi:sine-wave",
        min_value=FREQUENCY_MIN_HZ, max_value=FREQUENCY_MAX_HZ, zero_without_voltage=True,
    ),
)


def _telemetry_unique_id(entry_id: str, channel: int, key: str) -> str:
    return f"{entry_id}_channel{channel:02d}_{key}"


def _active_channels(profile: ModelProfile) -> int:
    return min(profile.channels, CHANNEL_COUNT_ACTIVE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_type = data.get(CONF_DEVICE_TYPE)

    # -----------------------------------------------------------------------
    # Modbus Node – error code
    # -----------------------------------------------------------------------
    if device_type == DEVICE_TYPE_MODBUS_NODE:
        async_add_entities([
            ModbusNodeErrorCodeSensor(
                entry_id=entry.entry_id,
                device_name=data[CONF_DEVICE_NAME],
                node_number=data[CONF_MODBUS_NODE_NUMBER],
            )
        ])
        return

    # -----------------------------------------------------------------------
    # Power Meter – raw data, všeobecné údaje a telemetria podľa modelu
    # -----------------------------------------------------------------------
    coordinator: PowerMeterCoordinator = data["coordinator"]
    meter_number = data[CONF_POWER_METER_NUMBER]
    device_name = data[CONF_DEVICE_NAME]
    device_model = coordinator.info.get(CONF_DEVICE_MODEL) or data.get(CONF_DEVICE_MODEL, MODEL)
    profile = coordinator.profile

    entities: list[SensorEntity] = [
        PowerMeterRawDataSensor(coordinator, entry.entry_id, device_name, device_model, meter_number),
    ]
    for key in INFO_SENSOR_KEYS:
        entities.append(
            PowerMeterInfoSensor(coordinator, entry.entry_id, device_name, device_model, meter_number, key)
        )

    expected_unique_ids = {f"{entry.entry_id}_{ENTITY_RAW_DATA}"}
    expected_unique_ids.update(f"{entry.entry_id}_{key}" for key in INFO_SENSOR_KEYS)

    if profile is not None:
        for channel in range(1, _active_channels(profile) + 1):
            for description in TELEMETRY_SENSORS:
                entities.append(
                    PowerMeterTelemetrySensor(
                        coordinator, entry.entry_id, device_name, device_model, meter_number, channel, description,
                    )
                )
                expected_unique_ids.add(_telemetry_unique_id(entry.entry_id, channel, description.key))

        # Selector: odstránenie senzorov, ktoré zariadenie (podľa modelu) nemá.
        # Robí sa len pri známom modeli, aby nedostupné zariadenie neodstránilo existujúce entity.
        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg_entry.domain == "sensor" and reg_entry.unique_id not in expected_unique_ids:
                LOGGER.info("Removing sensor not supported by model %s: %s", device_model, reg_entry.entity_id)
                registry.async_remove(reg_entry.entity_id)
    else:
        LOGGER.warning("Model of %s is unknown – telemetry sensors are not created", device_name)

    async_add_entities(entities)


def _power_meter_device_info(
    entry_id: str, device_name: str, device_model: str, node_entry_id: str | None = None,
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=device_name,
        manufacturer=MANUFACTURER,
        model=device_model,
        sw_version=VERSION,
        configuration_url=DOCUMENTATION_URL,
        # Prepojenie so zariadením Modbus Node, cez ktoré merač komunikuje
        via_device=(DOMAIN, node_entry_id) if node_entry_id else None,
    )


# ===========================================================================
# Modbus Node – Error Code
# ===========================================================================

class ModbusNodeErrorCodeSensor(SensorEntity):
    """Diagnostický senzor s chybovým kódom Modbus Node (bitové príznaky)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = ENTITY_MODBUS_NODE_SENSOR_ERROR_CODE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, entry_id: str, device_name: str, node_number: int) -> None:
        self._entry_id = entry_id
        self._device_name = device_name
        self._attr_unique_id = f"{entry_id}_{ENTITY_MODBUS_NODE_SENSOR_ERROR_CODE}"
        self._attr_native_value = MODBUS_ERROR_NONE
        self.entity_id = f"sensor.{MODBUS_NODE_ENTITY_ID_PREFIX}_{node_number}_{ENTITY_MODBUS_NODE_SENSOR_ERROR_CODE}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model="Modbus Node",
            sw_version=VERSION,
            configuration_url=DOCUMENTATION_URL,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_modbus_node_update_{self._entry_id}",
                self._handle_error_code_update,
            )
        )

    @callback
    def _handle_error_code_update(self) -> None:
        """Načíta error_code z ModbusNodeInstance a aktualizuje senzor."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        if isinstance(entry_data, dict):
            modbus_node = entry_data.get("modbus_node_instance")
            if modbus_node is not None and self._attr_native_value != modbus_node.error_code:
                self._attr_native_value = modbus_node.error_code
                self.async_write_ha_state()


# ===========================================================================
# Power Meter – raw data (diagnostika)
# ===========================================================================

class PowerMeterRawDataSensor(CoordinatorEntity[PowerMeterCoordinator], SensorEntity):
    """Diagnostický senzor: stav = počet načítaných registrov, atribút raw_data = zoznam hodnôt.

    Štandardne je vypnutý. Atribút raw_data sa NEZAPISUJE do recorderu
    (_unrecorded_attributes), takže nezaťažuje databázu pri čítaní každé 2 s.
    """

    _attr_has_entity_name = True
    _attr_translation_key = ENTITY_RAW_DATA
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:code-array"
    _unrecorded_attributes = frozenset({ATTR_RAW_DATA})

    def __init__(self, coordinator, entry_id, device_name, device_model, meter_number) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_name = device_name
        self._device_model = device_model
        self._attr_unique_id = f"{entry_id}_{ENTITY_RAW_DATA}"
        self.entity_id = f"sensor.{POWER_METER_ENTITY_ID_PREFIX}_{meter_number}_{ENTITY_RAW_DATA}"

    @property
    def device_info(self) -> DeviceInfo:
        return _power_meter_device_info(
            self._entry_id, self._device_name, self._device_model, self.coordinator.modbus_node_entry_id)

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return len(data) if data is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {ATTR_RAW_DATA: list(data)} if data is not None else {}


# ===========================================================================
# Power Meter – všeobecné údaje o zariadení (registre 0x0000 až 0x0003)
# ===========================================================================

INFO_SENSOR_KEYS = (ENTITY_MODEL, ENTITY_VOLTAGE_RANGE, ENTITY_CIRCUIT_RANGE)


class PowerMeterInfoSensor(CoordinatorEntity[PowerMeterCoordinator], SensorEntity):
    """Všeobecný údaj o zariadení: model, napäťový rozsah (V), rozsah prúdu (A).

    Entita je unavailable, ak čítanie zlyhalo alebo údaj chýba.
    Chýbajúci údaj / neznámy model vyvolá opätovné načítanie v koordinátore.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id, device_name, device_model, meter_number, key: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_name = device_name
        self._device_model = device_model
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = f"sensor.{POWER_METER_ENTITY_ID_PREFIX}_{meter_number}_{key}"

        if key == ENTITY_MODEL:
            self._attr_icon = "mdi:chip"
        elif key == ENTITY_VOLTAGE_RANGE:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_suggested_display_precision = 0
        elif key == ENTITY_CIRCUIT_RANGE:
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_suggested_display_precision = 1

    @property
    def device_info(self) -> DeviceInfo:
        return _power_meter_device_info(
            self._entry_id, self._device_name, self._device_model, self.coordinator.modbus_node_entry_id)

    @property
    def native_value(self) -> str | float | int | None:
        info = self.coordinator.info
        if self._key == ENTITY_MODEL:
            return info.get(CONF_DEVICE_MODEL)
        if self._key == ENTITY_VOLTAGE_RANGE:
            return info.get(CONF_VOLTAGE_RANGE_V)
        raw = info.get(CONF_CURRENT_RANGE_RAW)
        return None if raw is None else round(current_range_amps(raw), 1)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


# ===========================================================================
# Power Meter – telemetria kanála (generická)
# ===========================================================================

class PowerMeterTelemetrySensor(CoordinatorEntity[PowerMeterCoordinator], SensorEntity):
    """Telemetrický údaj jedného kanála (read-only register).

    Entita je unavailable, ak:
      - zdroj dát (Modbus čítanie) zlyhal,
      - v dátach chýba príslušný register,
      - hodnota je mimo rozsahu <min_value, max_value>.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator, entry_id, device_name, device_model, meter_number, channel: int,
        description: TelemetryDescription,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_name = device_name
        self._device_model = device_model
        self._description = description
        self._channel = channel

        profile = coordinator.profile
        registers_per_channel = profile.registers_per_channel if profile else 0
        self._register_index = (channel - 1) * registers_per_channel + description.offset

        channel_txt = f"{channel:02d}"
        self._attr_translation_key = f"channel_{description.key}"
        self._attr_translation_placeholders = {"channel": channel_txt}
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_native_unit_of_measurement = description.unit
        self._attr_suggested_display_precision = description.precision
        self._attr_icon = description.icon
        self._attr_unique_id = _telemetry_unique_id(entry_id, channel, description.key)
        self.entity_id = f"sensor.{POWER_METER_ENTITY_ID_PREFIX}_{meter_number}_channel{channel_txt}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        return _power_meter_device_info(
            self._entry_id, self._device_name, self._device_model, self.coordinator.modbus_node_entry_id)

    def _voltage_ok(self, data) -> bool:
        """True, ak napätie rovnakého kanála dosahuje minimum pre platné meranie (frekvencia, cos φ)."""
        index = self._register_index - self._description.offset + OFFSET_VOLTAGE
        if not data or index >= len(data):
            return False
        return int(data[index]) * VOLTAGE_SCALE >= MIN_VOLTAGE_FOR_VALID_MEASUREMENT_V

    def _value(self) -> float | None:
        """Vráti hodnotu alebo None, ak údaj chýba / je mimo povoleného rozsahu."""
        data = self.coordinator.data
        if (self._description.requires_voltage or self._description.zero_without_voltage) \
                and not self._voltage_ok(data):
            if self._description.zero_without_voltage and data:
                return 0.0
            return None
        words = self._description.words
        if not data or self._register_index + words > len(data):
            return None
        try:
            raw = 0
            for word in data[self._register_index:self._register_index + words]:
                raw = (raw << 16) | (int(word) & 0xFFFF)
            value = float(raw) * self._description.scale
        except (TypeError, ValueError):
            return None
        if not (self._description.min_value <= value <= self._description.max_value):
            return None
        return round(value, self._description.precision)

    @property
    def available(self) -> bool:
        return super().available and self._value() is not None

    @property
    def native_value(self) -> float | None:
        return self._value()
