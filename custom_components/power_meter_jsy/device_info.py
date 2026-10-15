from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" device_info.py """

"""Čítanie a vyhodnotenie všeobecných údajov o zariadení (registre 0x0000 až 0x0003)."""

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .const import *

LOGGER = logging.getLogger(__name__)


async def _sleep(seconds: float) -> None:
    """Čakanie (samostatná funkcia, aby sa dalo v testoch nahradiť)."""
    await asyncio.sleep(seconds)

# Kľúče údajov o zariadení (ukladajú sa aj do config entry)
INFO_KEYS = (
    CONF_DEVICE_MODEL_CODE,
    CONF_DEVICE_MODEL,
    CONF_VOLTAGE_RANGE_V,
    CONF_CURRENT_RANGE_RAW,
)


def model_name(code: int) -> str:
    """Názov modelu podľa hodnoty registra 0x0000 (napr. 0x0138 -> JSY-MK-138)."""
    return MODEL_NAMES.get(code, f"Unknown (0x{code:04X})")


def current_range_amps(raw: int) -> float:
    """Rozsah meracích cievok v A z registra 0x0003 (0x0400 = 50 A, 0x03E8 = 48,8 A)."""
    return raw * CURRENT_RANGE_FULL_SCALE_A / CURRENT_RANGE_FULL_SCALE_RAW


def info_from_registers(registers) -> dict:
    """Zostaví údaje o zariadení zo 4 registrov 0x0000 až 0x0003."""
    model_code = int(registers[REG_MODEL]) & 0xFFFF
    return {
        CONF_DEVICE_MODEL_CODE: model_code,
        CONF_DEVICE_MODEL: model_name(model_code),
        CONF_VOLTAGE_RANGE_V: int(registers[REG_VOLTAGE_RANGE]) & 0xFFFF,
        CONF_CURRENT_RANGE_RAW: int(registers[REG_CURRENT_RANGE]) & 0xFFFF,
    }


def info_is_valid(info: dict | None) -> bool:
    """Údaje sú platné, ak sú vyplnené všetky hodnoty a model je medzi známymi modelmi."""
    if not info:
        return False
    if any(info.get(key) is None for key in INFO_KEYS):
        return False
    return info.get(CONF_DEVICE_MODEL_CODE) in MODEL_PROFILES


async def async_read_device_info(hass: HomeAssistant, node_entry_id: str, device_id: int):
    """Prečíta z merača základné parametre (registre 0x0000 až 0x0003) JEDNÝM paketom.

    Returns:
        (info, None) pri úspechu, alebo (None, kľúč_chyby) pri zlyhaní.
    """
    from . import get_modbus_node_instance

    node = get_modbus_node_instance(hass, node_entry_id)
    if node is None:
        return None, "node_not_loaded"

    result = await node.modbus_communication(
        device_id, MODBUS_CMD_READ_HOLDING, REG_DEVICE_INFO_ADDRESS, REG_DEVICE_INFO_COUNT,
    )
    if not result["success"]:
        return None, "cannot_connect"

    registers = getattr(result["data"], "registers", None)
    if registers is None or len(registers) < REG_DEVICE_INFO_COUNT:
        return None, "cannot_connect"

    return info_from_registers(registers), None


# ---------------------------------------------------------------------------
# Priradenie fáz k meracím kanálom
# ---------------------------------------------------------------------------
def phase_key(channel: int) -> str:
    """Kľúč poľa formulára pre kanál (1-based), napr. phase_ch01."""
    return f"{CONF_PHASE_PREFIX}{channel:02d}"


def phases_from_registers(registers, channels: int) -> list[int]:
    """Rozloží registre (High Byte = nepárny kanál, Low Byte = párny kanál) na kódy fáz po kanáloch."""
    phases: list[int] = []
    for register in registers:
        value = int(register) & 0xFFFF
        phases.append((value >> 8) & 0xFF)
        phases.append(value & 0xFF)
    return phases[:channels]


def phases_to_registers(phases: list[int]) -> list[int]:
    """Zloží kódy fáz po kanáloch do registrov (2 kanály na register)."""
    padded = list(phases) + [PHASE_OPTION_TO_CODE[DEFAULT_PHASE_OPTION]] * (len(phases) % 2)
    return [((padded[i] & 0xFF) << 8) | (padded[i + 1] & 0xFF) for i in range(0, len(padded), 2)]


async def async_read_phases(hass: HomeAssistant, node_entry_id: str, device_id: int, profile: ModelProfile):
    """Prečíta priradenie fáz zo zariadenia JEDNÝM paketom.

    Returns:
        (zoznam kódov fáz po kanáloch, None) alebo (None, kľúč_chyby).
    """
    from . import get_modbus_node_instance

    node = get_modbus_node_instance(hass, node_entry_id)
    if node is None:
        return None, "node_not_loaded"

    result = await node.modbus_communication(
        device_id, MODBUS_CMD_READ_HOLDING, profile.phase_address, profile.phase_register_count,
    )
    if not result["success"]:
        return None, "cannot_connect"

    registers = getattr(result["data"], "registers", None)
    if registers is None or len(registers) < profile.phase_register_count:
        return None, "cannot_connect"

    return phases_from_registers(registers, profile.channels), None


async def async_write_phases(
    hass: HomeAssistant, node_entry_id: str, device_id: int, profile: ModelProfile, phases: list[int],
):
    """Zapíše priradenie fáz do zariadenia (funkcia 0x10, JEDEN paket) a overí ho spätným čítaním.

    Zariadenie odpovedá pomaly, preto:
      - platí predĺžený timeout odpovede (CONFIG_TIMEOUT_S),
      - zápis a overenie sa opakujú najviac PHASE_WRITE_ATTEMPTS-krát vo FIXNÝCH intervaloch
        PHASE_WRITE_RETRY_DELAY_S (nezávisle od intervalu čítania telemetrie),
      - po potvrdení zápisu sa pred spätným čítaním počká PHASE_VERIFY_DELAY_S,
      - ak zápis zariadenie potvrdilo, ale spätné čítanie ešte nesedí, ďalší pokus iba zopakuje
        overenie (zariadenie mohlo zmenu len pomaly spracúvať); až po neúspešnom overení sa zápis zopakuje.

    Returns:
        None pri úspechu, inak kľúč chyby: node_not_loaded / write_failed / write_verify_failed.
    """
    from . import get_modbus_node_instance

    node = get_modbus_node_instance(hass, node_entry_id)
    if node is None:
        return "node_not_loaded"

    registers = phases_to_registers(phases)
    acknowledged = False        # zariadenie potvrdilo zápis (ale zmena ešte nemusí byť viditeľná)
    error = "write_failed"

    async with node.extended_timeout(CONFIG_TIMEOUT_S):
        for attempt in range(1, PHASE_WRITE_ATTEMPTS + 1):
            if attempt > 1:
                await _sleep(PHASE_WRITE_RETRY_DELAY_S)

            if not acknowledged:
                result = await node.modbus_communication(
                    device_id, MODBUS_CMD_WRITE_MULTIPLE, profile.phase_address, registers, attempts=1,
                )
                if not result["success"]:
                    LOGGER.warning("Phase write attempt %d/%d failed: %s",
                                   attempt, PHASE_WRITE_ATTEMPTS, result["error"])
                    error = "write_failed"
                    continue
                acknowledged = True
                await _sleep(PHASE_VERIFY_DELAY_S)

            result = await node.modbus_communication(
                device_id, MODBUS_CMD_READ_HOLDING, profile.phase_address, profile.phase_register_count,
                attempts=1,
            )
            stored = None
            if result["success"]:
                regs = getattr(result["data"], "registers", None)
                if regs is not None and len(regs) >= profile.phase_register_count:
                    stored = phases_from_registers(regs, profile.channels)
            if stored == list(phases):
                return None
            LOGGER.warning("Phase verify attempt %d/%d: device returned %s, expected %s",
                           attempt, PHASE_WRITE_ATTEMPTS, stored, list(phases))
            error = "write_verify_failed"
            acknowledged = False if attempt >= 2 else True    # po opakovanom neúspešnom overení sa zápis zopakuje

    return error
