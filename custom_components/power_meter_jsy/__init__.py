from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" __init__.py """

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .const import *

LOGGER = logging.getLogger(__name__)


def _log_modbus_settings(entry_id: str, settings) -> None:
    """Vypíše aktuálne Modbus nastavenia do debug logu."""
    conn_type = settings.modbus_connection_type

    LOGGER.debug("=== MODBUS NASTAVENIA [entry_id: %s] ===", entry_id)
    LOGGER.debug("  Typ pripojenia : %s", conn_type)

    if conn_type == MODBUS_CONNECTION_EXISTING_NODE:
        LOGGER.debug("  Modbus Node    : %s", settings.modbus_node_name)

    elif conn_type == MODBUS_CONNECTION_USB:
        LOGGER.debug("  Port (port)                       : %s", settings.port)
        LOGGER.debug("  Baudrate (baudrate)               : %s", settings.baudrate)
        LOGGER.debug("  Data Size (bytesize)              : %s", settings.bytesize)
        LOGGER.debug("  Stop Bits (stopbits)              : %s", settings.stopbits)
        LOGGER.debug("  Parity (parity)                   : %s", settings.parity)
        LOGGER.debug("  Delay (delay)                     : %s s", settings.delay)
        LOGGER.debug("  Msg Wait (message_wait_millisecond): %s ms", settings.message_wait_millisecond)
        LOGGER.debug("  Timeout (timeout)                 : %s s", settings.timeout)

    elif conn_type in (MODBUS_CONNECTION_TCP, MODBUS_CONNECTION_UDP):
        LOGGER.debug("  Host (host)                       : %s", settings.host)
        LOGGER.debug("  Port (port)                       : %s", settings.port)
        LOGGER.debug("  Msg Wait (message_wait_millisecond): %s ms", settings.message_wait_millisecond)
        LOGGER.debug("  Timeout (timeout)                 : %s s", settings.timeout)

    LOGGER.debug("=== KONIEC MODBUS NASTAVENÍ ===")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Power Meter JSY from a config entry."""

    def _get(key, default):
        """Pomocná funkcia: číta z options, fallback na data, potom default."""
        return entry.options.get(key, entry.data.get(key, default))

    device_type = _get(CONF_DEVICE_TYPE, DEVICE_TYPE_POWER_METER)
    hass.data.setdefault(DOMAIN, {})

    # -----------------------------------------------------------------------
    # Modbus Node – registrácia do hass.data s Modbus nastaveniami
    # -----------------------------------------------------------------------
    if device_type == DEVICE_TYPE_MODBUS_NODE:
        from .modbus_node import ModbusNodeInstance

        modbus_node_number = int(_get(CONF_MODBUS_NODE_NUMBER, 1))
        modbus_connection_type = _get(CONF_MODBUS_CONNECTION_TYPE, DEFAULT_MODBUS_CONNECTION_TYPE)

        # SelectSelector ukladá hodnoty ako str, NumberSelector ako float -> int konverzia
        modbus_node_instance = ModbusNodeInstance(hass, entry.entry_id)
        settings = modbus_node_instance.settings
        settings.modbus_connection_type = modbus_connection_type
        settings.modbus_node_name = _get(CONF_MODBUS_NODE_NAME, "")
        settings.port = str(_get(CONF_MODBUS_PORT, ""))
        settings.baudrate = int(_get(CONF_MODBUS_BAUDRATE, DEFAULT_MODBUS_BAUDRATE))
        settings.bytesize = int(_get(CONF_MODBUS_BYTESIZE, DEFAULT_MODBUS_BYTESIZE))
        settings.stopbits = int(_get(CONF_MODBUS_STOPBITS, DEFAULT_MODBUS_STOPBITS))
        settings.parity = _get(CONF_MODBUS_PARITY, DEFAULT_MODBUS_PARITY)
        settings.delay = int(float(_get(CONF_MODBUS_DELAY, DEFAULT_MODBUS_DELAY)))
        settings.message_wait_millisecond = int(float(_get(CONF_MODBUS_MESSAGE_WAIT, DEFAULT_MODBUS_MESSAGE_WAIT)))
        settings.timeout = int(float(_get(CONF_MODBUS_TIMEOUT, DEFAULT_MODBUS_TIMEOUT)))
        settings.host = _get(CONF_MODBUS_HOST, "")

        _log_modbus_settings(entry.entry_id, settings)

        hass.data[DOMAIN][entry.entry_id] = {
            CONF_DEVICE_TYPE: device_type,
            CONF_DEVICE_NAME: _get(CONF_DEVICE_NAME, "Modbus Node"),
            CONF_MODBUS_NODE_NUMBER: modbus_node_number,
            "modbus_node_instance": modbus_node_instance,
        }

        # Zariadenie Modbus Node sa zaregistruje hneď, aby sa naň mohli odkazovať zariadenia Power Meter (via_device)
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name=_get(CONF_DEVICE_NAME, "Modbus Node"),
            manufacturer=MANUFACTURER,
            model="Modbus Node",
            sw_version=VERSION,
            configuration_url=DOCUMENTATION_URL,
        )

        await hass.config_entries.async_forward_entry_setups(entry, MODBUS_NODE_PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(update_listener))
        return True

    # -----------------------------------------------------------------------
    # Power Meter
    # -----------------------------------------------------------------------
    from .coordinator import PowerMeterCoordinator

    modbus_node_entry_id = _get(CONF_MODBUS_NODE_ENTRY_ID, DEFAULT_MODBUS_NODE_ENTRY_ID)

    # Modbus Node už neexistuje (bol odstránený) – upozornenie v Opravách (Repairs)
    if hass.config_entries.async_get_entry(modbus_node_entry_id) is None:
        _report_missing_node(hass, entry)
        raise ConfigEntryNotReady(f"Modbus Node '{modbus_node_entry_id}' does not exist")
    ir.async_delete_issue(hass, DOMAIN, _missing_node_issue_id(entry.entry_id))

    # Modbus Node musí byť už načítaný (pri štarte HA sa môže načítať až po Power Meter)
    if get_modbus_node_instance(hass, modbus_node_entry_id) is None:
        raise ConfigEntryNotReady(
            f"Modbus Node '{modbus_node_entry_id}' is not loaded yet"
        )

    coordinator = PowerMeterCoordinator(
        hass,
        config_entry=entry,
        device_name=_get(CONF_DEVICE_NAME, "Power Meter"),
        modbus_node_entry_id=modbus_node_entry_id,
        modbus_device_id=int(float(_get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID))),
        scan_interval=int(float(_get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))),
    )

    hass.data[DOMAIN][entry.entry_id] = {
        CONF_DEVICE_TYPE: device_type,
        CONF_DEVICE_NAME: _get(CONF_DEVICE_NAME, "Power Meter"),
        CONF_POWER_METER_NUMBER: int(_get(CONF_POWER_METER_NUMBER, 1)),
        CONF_MODBUS_NODE_ENTRY_ID: modbus_node_entry_id,
        CONF_DEVICE_MODEL: _get(CONF_DEVICE_MODEL, MODEL),
        "coordinator": coordinator,
    }

    # Prvé čítanie. Pri zlyhaní (linka nedostupná) setup nekončí chybou: entity sa
    # vytvoria ako unavailable a koordinátor ďalej periodicky skúša čítať.
    await coordinator.async_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, POWER_METER_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


def _missing_node_issue_id(meter_entry_id: str) -> str:
    return f"node_missing_{meter_entry_id}"


def _report_missing_node(hass: HomeAssistant, meter_entry: ConfigEntry) -> None:
    """Vytvorí upozornenie (Opravy), že Power Meter používa Modbus Node, ktorý neexistuje."""
    meter_name = meter_entry.options.get(CONF_DEVICE_NAME, meter_entry.data.get(CONF_DEVICE_NAME, meter_entry.title))
    ir.async_create_issue(
        hass, DOMAIN, _missing_node_issue_id(meter_entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="node_missing",
        translation_placeholders={"meter": str(meter_name)},
    )


def get_modbus_node_instance(hass: HomeAssistant, modbus_node_entry_id: str):
    """Vráti ModbusNodeInstance pre dané entry_id.

    Power Meter zariadenia volajú túto funkciu aby získali referenciu
    na zdieľanú Modbus komunikačnú inštanciu.

    Returns:
        ModbusNodeInstance alebo None ak neexistuje.
    """
    if not modbus_node_entry_id:
        LOGGER.error("No Modbus Node entry_id configured")
        return None

    node_data = hass.data.get(DOMAIN, {}).get(modbus_node_entry_id)
    if node_data is None:
        LOGGER.debug("Modbus Node entry '%s' not found in hass.data", modbus_node_entry_id)
        return None

    instance = node_data.get("modbus_node_instance")
    if instance is None:
        LOGGER.error("ModbusNodeInstance not found for entry '%s'", modbus_node_entry_id)
        return None

    return instance


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update – reload, aby sa prejavili zmeny nastavení."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_POWER_METER)

    if device_type == DEVICE_TYPE_MODBUS_NODE:
        platforms = MODBUS_NODE_PLATFORMS
    else:
        platforms = POWER_METER_PLATFORMS

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if device_type == DEVICE_TYPE_MODBUS_NODE and isinstance(entry_data, dict):
            node_instance = entry_data.get("modbus_node_instance")
            if node_instance is not None:
                node_instance.close_modbus_client()
                LOGGER.debug("Closed modbus client during unload")

    return unload_ok



async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Po odstránení zariadenia: upozorní na Power Meter zariadenia, ktoré odstránený Modbus Node používali."""
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_POWER_METER)
    if device_type == DEVICE_TYPE_MODBUS_NODE:
        for other in hass.config_entries.async_entries(DOMAIN):
            if (
                other.entry_id != entry.entry_id
                and other.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_POWER_METER) == DEVICE_TYPE_POWER_METER
                and other.options.get(CONF_MODBUS_NODE_ENTRY_ID, other.data.get(CONF_MODBUS_NODE_ENTRY_ID)) == entry.entry_id
            ):
                _report_missing_node(hass, other)
    else:
        ir.async_delete_issue(hass, DOMAIN, _missing_node_issue_id(entry.entry_id))
