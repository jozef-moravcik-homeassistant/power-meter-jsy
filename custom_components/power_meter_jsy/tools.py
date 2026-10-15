from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" tools.py """

"""Tools: zmena Modbus ID a komunikačných parametrov merača (register 0x0004).

Úplne nezávislé od zariadení Modbus Node a Power Meter: používa vlastné, dočasné Modbus pripojenie
(ModbusNodeInstance mimo hass.data), takže nijako neovplyvní už nakonfigurované zariadenia.
"""

import asyncio
import dataclasses
import logging

from homeassistant.core import HomeAssistant

from .const import *
from .modbus_node import ModbusNodeInstance

LOGGER = logging.getLogger(__name__)

# Výsledky zápisu
STATUS_APPLIED = "applied"            # zariadenie odpovedá s novými parametrami
STATUS_PENDING = "pending"            # zápis potvrdilo, ale stále odpovedá so starými (možno až po reštarte)
STATUS_UNREACHABLE = "unreachable"    # neodpovedá ani so starými, ani s novými parametrami
STATUS_WRITE_FAILED = "write_failed"  # zápis zlyhal, zariadenie odpovedá so starými parametrami
STATUS_UNVERIFIED = "unverified"      # zápis odoslaný, ale cez toto pripojenie sa overiť nedá (zmena rýchlosti/formátu)


async def _sleep(seconds: float) -> None:
    """Čakanie (samostatná funkcia, aby sa dalo v testoch nahradiť)."""
    await asyncio.sleep(seconds)


def format_of(parity: str, stopbits: int, bytesize: int = 8) -> str:
    """Názov formátu (napr. 8N1) podľa parity a stop bitov; neznámy formát sa nahradí predvoleným."""
    for name, (_code, par, stop) in COMM_FORMATS.items():
        if par == str(parity) and stop == int(stopbits) and int(bytesize) == 8:
            return name
    return DEFAULT_COMM_FORMAT


def encode_comm_register(device_id: int, baudrate: int, data_format: str) -> int:
    """Hodnota registra 0x0004: High Byte = ID, Low Byte = formát (horné 2 bity) + rýchlosť (dolné 4 bity)."""
    format_code = COMM_FORMATS[data_format][0]
    return ((int(device_id) & 0xFF) << 8) | (format_code << 6) | COMM_BAUDRATE_CODES[int(baudrate)]


@dataclasses.dataclass
class CommSettings:
    """Komunikačné parametre zariadenia."""
    device_id: int
    baudrate: int
    data_format: str


@dataclasses.dataclass
class ToolsResult:
    status: str
    model_code: int | None = None


def make_node(hass: HomeAssistant, conn: dict, *, baudrate: int | None = None, data_format: str | None = None):
    """Vytvorí dočasné Modbus pripojenie z údajov kroku pripojenia.

    baudrate a data_format sa použijú len pri sériovom (USB) pripojení – prepíšu parametre linky.
    Timeout je predĺžený (zariadenie odpovedá pomaly).
    """
    node = ModbusNodeInstance(hass, "tools")
    s = node.settings
    s.modbus_connection_type = conn.get(CONF_MODBUS_CONNECTION_TYPE, DEFAULT_MODBUS_CONNECTION_TYPE)
    s.modbus_node_name = conn.get(CONF_MODBUS_NODE_NAME, "")
    s.port = str(conn.get(CONF_MODBUS_PORT, ""))
    s.baudrate = int(baudrate or conn.get(CONF_MODBUS_BAUDRATE, DEFAULT_MODBUS_BAUDRATE))
    s.bytesize = int(conn.get(CONF_MODBUS_BYTESIZE, DEFAULT_MODBUS_BYTESIZE))
    s.stopbits = int(conn.get(CONF_MODBUS_STOPBITS, DEFAULT_MODBUS_STOPBITS))
    s.parity = conn.get(CONF_MODBUS_PARITY, DEFAULT_MODBUS_PARITY)
    if data_format is not None:
        _code, s.parity, s.stopbits = COMM_FORMATS[data_format]
        s.bytesize = 8
    s.delay = int(float(conn.get(CONF_MODBUS_DELAY, DEFAULT_MODBUS_DELAY)))
    s.message_wait_millisecond = int(float(conn.get(CONF_MODBUS_MESSAGE_WAIT, DEFAULT_MODBUS_MESSAGE_WAIT)))
    s.timeout = max(float(conn.get(CONF_MODBUS_TIMEOUT, DEFAULT_MODBUS_TIMEOUT)), CONFIG_TIMEOUT_S)
    s.host = conn.get(CONF_MODBUS_HOST, "")
    return node


async def async_probe(node: ModbusNodeInstance, device_id: int, attempts: int | None = 1) -> int | None:
    """Prečíta kód modelu (register 0x0000). Vráti None, ak zariadenie neodpovedá."""
    result = await node.modbus_communication(
        device_id, MODBUS_CMD_READ_HOLDING, REG_DEVICE_INFO_ADDRESS, REG_DEVICE_INFO_COUNT, attempts=attempts,
    )
    if not result["success"]:
        return None
    registers = getattr(result["data"], "registers", None)
    if registers is None or len(registers) < REG_DEVICE_INFO_COUNT:
        return None
    return int(registers[REG_MODEL]) & 0xFFFF


async def async_probe_connection(hass: HomeAssistant, conn: dict, device_id: int) -> int | None:
    """Overí spojenie so zariadením (dočasné pripojenie) a vráti kód modelu alebo None."""
    node = make_node(hass, conn)
    try:
        return await async_probe(node, device_id, attempts=None)
    finally:
        node.close_modbus_client()


async def async_apply_settings(
    hass: HomeAssistant, conn: dict, old: CommSettings, new: CommSettings, broadcast: bool,
) -> ToolsResult:
    """Zapíše nové ID a komunikačné parametre do zariadenia a overí výsledok.

    Zariadenie môže zmenu prijať okamžite (a odpovedať už s novými parametrami), preto sa odpoveď na zápis
    neberie ako jediný dôkaz: po každom zápise sa zariadenie skúsi načítať s novými, potom so starými
    parametrami. Zápis sa opakuje najviac PHASE_WRITE_ATTEMPTS-krát v pevných intervaloch.
    """
    is_serial = conn.get(CONF_MODBUS_CONNECTION_TYPE) == MODBUS_CONNECTION_USB
    target_id = COMM_BROADCAST_ID if broadcast else old.device_id
    value = encode_comm_register(new.device_id, new.baudrate, new.data_format)

    old_node = make_node(hass, conn)
    # Pri sériovej linke sa overuje s novou rýchlosťou a formátom; inak ide o to isté pripojenie
    new_node = make_node(hass, conn, baudrate=new.baudrate, data_format=new.data_format) if is_serial else old_node
    nodes = {old_node, new_node}

    # Cez TCP/UDP/existujúci Modbus hub sú parametre linky pevné a aktuálna rýchlosť/formát zariadenia sú
    # neznáme (zadávajú sa až ako nové) – zmenu rýchlosti a formátu overiť nedá, môže sa overiť len nové ID.
    verifiable = is_serial

    acknowledged = False
    old_alive = True      # zariadenie odpovedalo so starými parametrami (alebo sa to ešte nezistilo)
    try:
        for attempt in range(1, PHASE_WRITE_ATTEMPTS + 1):
            if attempt > 1:
                await _sleep(PHASE_WRITE_RETRY_DELAY_S)

            if old_alive and not acknowledged:
                if is_serial:
                    new_node.close_modbus_client()       # port môže používať len jedno pripojenie
                result = await old_node.modbus_communication(
                    target_id, MODBUS_CMD_WRITE_MULTIPLE, REG_COMM_ADDRESS, [value], attempts=1,
                )
                # Pri broadcaste zariadenie neodpovedá; zlyhanie zápisu môže znamenať aj to, že zariadenie
                # už prešlo na nové parametre – preto sa vždy robí overenie.
                acknowledged = acknowledged or result["success"]
                if not verifiable:
                    if not (acknowledged or broadcast):
                        continue
                    if broadcast or new.device_id != old.device_id:
                        # Zmena ID sa overí, ak linka zostala nastavená rovnako, ako zariadenie používa
                        await _sleep(PHASE_VERIFY_DELAY_S)
                        if await async_probe(new_node, new.device_id) is not None:
                            return ToolsResult(STATUS_APPLIED)
                    return ToolsResult(STATUS_UNVERIFIED)
                await _sleep(PHASE_VERIFY_DELAY_S)

            if is_serial:
                old_node.close_modbus_client()
            if await async_probe(new_node, new.device_id) is not None:
                return ToolsResult(STATUS_APPLIED)

            if is_serial:
                new_node.close_modbus_client()
            if not broadcast and await async_probe(old_node, old.device_id) is not None:
                old_alive = True
                continue
            # Zariadenie neodpovedá so starými parametrami: nový zápis nemá zmysel, skúša sa len čítanie s novými
            old_alive = False

        if not verifiable:
            return ToolsResult(STATUS_WRITE_FAILED)
        if old_alive and not broadcast:
            return ToolsResult(STATUS_PENDING if acknowledged else STATUS_WRITE_FAILED)
        return ToolsResult(STATUS_UNREACHABLE)
    finally:
        for node in nodes:
            node.close_modbus_client()
