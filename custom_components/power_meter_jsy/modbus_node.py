from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" modbus_node.py """

"""Modbus Node – centralizovaná Modbus komunikácia pre Power Meter zariadenia."""

import asyncio
import contextlib
import logging
import dataclasses
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import *

try:
    from pymodbus.client import (
        ModbusSerialClient,
        ModbusTcpClient,
        ModbusUdpClient,
    )
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False

LOGGER = logging.getLogger(__name__)


async def _sleep(seconds: float) -> None:
    """Čakanie (samostatná funkcia, aby sa dalo v testoch nahradiť)."""
    await asyncio.sleep(seconds)


class ModbusNodeInstance:
    """Centralizovaná Modbus komunikačná inštancia.

    Jedna Modbus linka (RS485 / TCP / UDP / existujúci Modbus hub v HA) je reprezentovaná
    jedným ModbusNodeInstance. Viacero Power Meter zariadení môže zdieľať jeden ModbusNodeInstance.
    asyncio.Lock zabezpečuje, že na jednej Modbus linke prebieha vždy len jedna komunikácia.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._lock = asyncio.Lock()

        # Modbus klient
        self.modbus_client = None
        self._modbus_client_params = None

        # Nastavenia – budú nastavené z __init__.py
        self.settings = self.Settings()

        # Posledný chybový kód (pre error_code sensor) – bitové príznaky
        self.error_code: int = MODBUS_ERROR_NONE

        # Čas ukončenia posledného paketu pre každé zariadenie (monotonic) – minimálna medzera medzi paketmi
        self._last_packet_end: dict[int, float] = {}

        # Zariadenia, pri ktorých sa už zalogovala chyba čítania (kým sa komunikácia neobnoví)
        self._failing_devices: set[int] = set()

        # Čas posledného urýchleného pripojenia core Modbus hubu (monotonic)
        self._last_hub_nudge: float = -HUB_RECONNECT_NUDGE_INTERVAL_S

        # Dočasne predĺžený timeout (napr. pri zápise konfigurácie); počíta sa súčasných používateľov
        self._timeout_override: float | None = None
        self._timeout_users = 0

    @property
    def effective_timeout(self) -> float:
        """Timeout odpovede v s: dočasne predĺžený, ak je aktívny extended_timeout, inak z nastavení."""
        if self._timeout_override is not None:
            return max(self._timeout_override, self.settings.timeout)
        return self.settings.timeout

    @contextlib.asynccontextmanager
    async def extended_timeout(self, seconds: float):
        """Dočasne predĺži timeout odpovede (zariadenie môže odpovedať aj po viac než 1 s).

        Zmena platí pre celý Modbus Node, kým je kontext aktívny; klient sa pripojí s novým
        timeoutom pri najbližšej komunikácii a po skončení sa vráti pôvodný.
        """
        self._timeout_users += 1
        self._timeout_override = max(seconds, self._timeout_override or 0)
        try:
            yield
        finally:
            self._timeout_users -= 1
            if self._timeout_users <= 0:
                self._timeout_users = 0
                self._timeout_override = None

    async def _ensure_hub_connected(self, hub) -> bool:
        """Core Modbus hub sa pri neúspešnom pripojení (napr. port ešte držalo iné pripojenie) pokúša o nové
        pripojenie až po 60 s. Po uvoľnení portu (napr. po prepnutí z USB na existujúci Modbus Node) preto
        pripojenie urýchlime: čakajúci pokus hubu sa nahradí okamžitým. Pokus sa opakuje najviac raz za 15 s."""
        connected = getattr(hub, "connected", None)
        if connected is None or connected:
            return True
        now = time.monotonic()
        if now - self._last_hub_nudge >= HUB_RECONNECT_NUDGE_INTERVAL_S:
            self._last_hub_nudge = now
            try:
                task = getattr(hub, "_connect_task", None)
                if task is not None and not task.done():
                    task.cancel()
                hub._connect_task = self.hass.async_create_background_task(
                    hub.async_pb_connect(), "power_meter_jsy-modbus-reconnect")
                LOGGER.debug("ModbusNode [%s]: forcing reconnect of Modbus hub '%s'",
                             self._entry_id[:8], getattr(hub, "name", "?"))
            except Exception as err:    # súkromné API core integrácie sa môže líšiť
                LOGGER.debug("ModbusNode [%s]: cannot force hub reconnect: %s", self._entry_id[:8], err)
        for _ in range(HUB_RECONNECT_WAIT_STEPS):
            if hub.connected:
                return True
            await _sleep(0.25)
        return bool(hub.connected)

    async def _respect_packet_gap(self, device_id: int) -> None:
        """Zaistí minimálnu medzeru medzi dvoma paketmi pre to isté zariadenie."""
        last = self._last_packet_end.get(device_id)
        if last is None:
            return
        wait = MODBUS_MIN_PACKET_GAP_S - (time.monotonic() - last)
        if wait > 0:
            await _sleep(wait)

    @dataclasses.dataclass
    class Settings:
        modbus_connection_type: str = DEFAULT_MODBUS_CONNECTION_TYPE
        modbus_node_name: str = ""
        port: str = ""
        baudrate: int = DEFAULT_MODBUS_BAUDRATE
        bytesize: int = DEFAULT_MODBUS_BYTESIZE
        stopbits: int = DEFAULT_MODBUS_STOPBITS
        parity: str = DEFAULT_MODBUS_PARITY
        delay: int = DEFAULT_MODBUS_DELAY
        message_wait_millisecond: int = DEFAULT_MODBUS_MESSAGE_WAIT
        timeout: int = DEFAULT_MODBUS_TIMEOUT
        host: str = ""

    # ------------------------------------------------------------------
    # Logovanie chýb komunikácie
    # ------------------------------------------------------------------
    def _log_failure(self, device_id: int, command: int, message: str, level: int = logging.ERROR) -> None:
        """Chyba čítania sa pri nedostupnom zariadení loguje len PRVÝKRÁT (potom len v debug logu), aby sa
        pri krátkom intervale čítania log nezahltil. Zápisy (konfigurácia) sa logujú vždy."""
        text = f"ModbusNode [{self._entry_id[:8]}] device {device_id}: {message}"
        if command != MODBUS_CMD_READ_HOLDING:
            LOGGER.log(level, text)
        elif device_id not in self._failing_devices:
            if level >= logging.ERROR:
                self._failing_devices.add(device_id)
                LOGGER.log(level, "%s (further read errors of this device are logged only at debug level "
                                  "until communication is restored)", text)
            else:
                LOGGER.debug(text)
        else:
            LOGGER.debug(text)

    def _log_recovered(self, device_id: int, command: int) -> None:
        """Po obnovení komunikácie so zariadením sa to zaloguje jedným informačným záznamom."""
        if command == MODBUS_CMD_READ_HOLDING and device_id in self._failing_devices:
            self._failing_devices.discard(device_id)
            LOGGER.info("ModbusNode [%s] device %s: communication restored", self._entry_id[:8], device_id)

    def close_modbus_client(self):
        """Zatvorí Modbus klienta ak existuje."""
        if self.modbus_client is not None:
            try:
                if hasattr(self.modbus_client, 'close'):
                    self.modbus_client.close()
                    LOGGER.debug("ModbusNode [%s]: client closed", self._entry_id[:8])
            except Exception as e:
                LOGGER.error("ModbusNode [%s]: error closing client: %s", self._entry_id[:8], e)
            finally:
                self.modbus_client = None

    async def release_connection(self) -> None:
        """Počká na dokončenie prebiehajúcej komunikácie a uvoľní pripojenie (sériový port / TCP spojenie).
        Klient sa pri ďalšej komunikácii vytvorí znova."""
        async with self._lock:
            self.close_modbus_client()

    async def modbus_communication(
        self,
        device_id: int,
        command: int,
        address: int,
        count: int,
        attempts: int | None = None,
    ) -> dict:
        """
        Univerzálna metóda pre Modbus komunikáciu – thread-safe cez asyncio.Lock.

        Args:
            device_id: Modbus ID zariadenia (1 Byte)
            command: Modbus funkčný kód (napr. 03=Read Holding, 06=Write Single)
            address: Modbus adresa registra (2 Bytes)
            count: Počet registrov, hodnota na zápis (06) alebo zoznam hodnôt (16)
            attempts: Počet pokusov (predvolene 2 – s opätovným pripojením); 1 = bez vnútorného opakovania

        Returns:
            dict: {'success': bool, 'data': Any, 'error': str}
        """
        async with self._lock:
            result = await self._modbus_communication_unsafe(
                device_id, command, address, count, attempts
            )

        # Aktualizovať error_code sensor
        async_dispatcher_send(self.hass, f"{DOMAIN}_modbus_node_update_{self._entry_id}")

        return result

    async def _modbus_communication_unsafe(
        self,
        device_id: int,
        command: int,
        address: int,
        count: int,
        attempts: int | None = None,
    ) -> dict:
        """Vnútorná implementácia Modbus komunikácie (bez locku).

        Pri zlyhaní zatvorí klienta a automaticky vykoná jeden retry
        s novým pripojením (ak nie je počet pokusov obmedzený).
        """
        max_attempts = attempts or 2

        for attempt in range(1, max_attempts + 1):
            # Zariadenie potrebuje čas na spracovanie predchádzajúceho paketu
            await self._respect_packet_gap(device_id)
            try:
                result = await self._modbus_single_attempt(
                    device_id, command, address, count, attempt,
                )
            finally:
                self._last_packet_end[device_id] = time.monotonic()
            if result['success']:
                self._log_recovered(device_id, command)
                return result

            # Zlyhanie – zatvoriť klienta aby sa pri ďalšom pokuse vytvoril nový
            self._log_failure(
                device_id, command,
                f"attempt {attempt}/{max_attempts} failed: {result['error']} – closing client for reconnect",
                level=logging.WARNING,
            )
            self.close_modbus_client()

        # Všetky pokusy zlyhali
        return result

    async def _call_client(self, func, address: int, value: int, device_id: int):
        """Zavolá metódu pymodbus klienta v executore.

        Parameter s ID zariadenia sa v rôznych verziách pymodbus volá inak:
          - pymodbus >= 3.10 : device_id=  (count/value je len keyword pri čítaní)
          - pymodbus 3.x     : slave=
          - staršie verzie   : unit=
        Preto sa skúšajú postupne všetky varianty.
        """
        is_read = func.__name__.startswith("read_")
        variants = []
        if is_read:
            variants.append(lambda: func(address, count=value, device_id=device_id))
            variants.append(lambda: func(address, count=value, slave=device_id))
            variants.append(lambda: func(address, count=value, unit=device_id))
        else:
            variants.append(lambda: func(address, value, device_id=device_id))
            variants.append(lambda: func(address, value, slave=device_id))
            variants.append(lambda: func(address, value, unit=device_id))

        last_type_error: TypeError | None = None
        for variant in variants:
            try:
                return await self.hass.async_add_executor_job(variant)
            except TypeError as err:
                # Nesprávny názov parametra pre danú verziu pymodbus – skúsiť ďalší
                last_type_error = err
                continue
        raise last_type_error if last_type_error else RuntimeError("Modbus call failed")

    async def _modbus_single_attempt(
        self,
        device_id: int,
        command: int,
        address: int,
        count: int,
        attempt: int,
    ) -> dict:
        """Jeden pokus o Modbus komunikáciu."""
        result = {
            'success': False,
            'data': None,
            'error': None,
        }

        try:
            conn_type = self.settings.modbus_connection_type

            # ***** Existing Node *****
            if conn_type == MODBUS_CONNECTION_EXISTING_NODE:
                node_name = self.settings.modbus_node_name
                if not node_name:
                    result['error'] = "Modbus node name not configured"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                from homeassistant.components.modbus import DOMAIN as MODBUS_DOMAIN
                if MODBUS_DOMAIN not in self.hass.data:
                    result['error'] = "Modbus integration not loaded"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                modbus_hubs = self.hass.data[MODBUS_DOMAIN]
                if node_name not in modbus_hubs:
                    result['error'] = f"Modbus node '{node_name}' not found"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                hub = modbus_hubs[node_name]

                if not await self._ensure_hub_connected(hub):
                    result['error'] = (f"Modbus node '{node_name}' is not connected (its port may be in use by "
                                       f"another connection; the Modbus integration retries every 60 s)")
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                if command == MODBUS_CMD_READ_HOLDING:  # Read Holding Registers
                    response = await hub.async_pb_call(
                        device_id, address, count, "holding"
                    )
                elif command == MODBUS_CMD_WRITE_SINGLE:  # Write Single Register
                    response = await hub.async_pb_call(
                        device_id, address, count, "write_register"
                    )
                elif command == MODBUS_CMD_WRITE_MULTIPLE:  # Write Multiple Registers (count = zoznam hodnôt)
                    response = await hub.async_pb_call(
                        device_id, address, list(count), "write_registers"
                    )
                else:
                    result['error'] = f"Unsupported command: {command}"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_UNKNOWN
                    return result

                if response and not response.isError():
                    result['success'] = True
                    result['data'] = response
                    self.error_code = MODBUS_ERROR_NONE
                else:
                    result['error'] = f"Modbus communication error: {response}"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_COMMUNICATION

            # ***** USB / TCP / UDP *****
            else:
                if not PYMODBUS_AVAILABLE:
                    result['error'] = "pymodbus library not available"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                current_params = {
                    'type': conn_type,
                    'port': str(self.settings.port) if self.settings.port else '',
                    'baudrate': self.settings.baudrate if conn_type == MODBUS_CONNECTION_USB else None,
                    'bytesize': self.settings.bytesize if conn_type == MODBUS_CONNECTION_USB else None,
                    'stopbits': self.settings.stopbits if conn_type == MODBUS_CONNECTION_USB else None,
                    'parity': self.settings.parity if conn_type == MODBUS_CONNECTION_USB else None,
                    'host': self.settings.host if conn_type in (MODBUS_CONNECTION_TCP, MODBUS_CONNECTION_UDP) else None,
                    'timeout': self.effective_timeout,
                }

                if self._modbus_client_params != current_params:
                    LOGGER.debug("ModbusNode [%s]: params changed, reconnecting", self._entry_id[:8])
                    self.close_modbus_client()
                    self._modbus_client_params = current_params

                # Vytvorenie klienta
                if self.modbus_client is None:
                    if conn_type == MODBUS_CONNECTION_USB:
                        self.modbus_client = ModbusSerialClient(
                            port=self.settings.port,
                            baudrate=self.settings.baudrate,
                            bytesize=self.settings.bytesize,
                            stopbits=self.settings.stopbits,
                            parity=self.settings.parity,
                            timeout=self.effective_timeout,
                        )
                    elif conn_type == MODBUS_CONNECTION_TCP:
                        port = self._safe_port(self.settings.port, DEFAULT_MODBUS_TCP_PORT)
                        self.modbus_client = ModbusTcpClient(
                            host=self.settings.host,
                            port=port,
                            timeout=self.effective_timeout,
                        )
                    elif conn_type == MODBUS_CONNECTION_UDP:
                        port = self._safe_port(self.settings.port, DEFAULT_MODBUS_TCP_PORT)
                        self.modbus_client = ModbusUdpClient(
                            host=self.settings.host,
                            port=port,
                            timeout=self.effective_timeout,
                        )

                # Pripojenie
                if not self.modbus_client.connected:
                    await self.hass.async_add_executor_job(self.modbus_client.connect)

                if not self.modbus_client.connected:
                    result['error'] = f"Failed to connect to Modbus device (attempt {attempt})"
                    if conn_type == MODBUS_CONNECTION_USB:
                        result['error'] += (f" – port {self.settings.port} is probably in use by another "
                                            f"connection (e.g. a Modbus node of the Modbus integration)")
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_PORT_OPEN
                    return result

                # Vykonanie príkazu
                response = None
                try:
                    if command == MODBUS_CMD_READ_HOLDING:  # Read Holding Registers
                        response = await self._call_client(
                            self.modbus_client.read_holding_registers, address, count, device_id
                        )
                    elif command == MODBUS_CMD_WRITE_SINGLE:  # Write Single Register
                        LOGGER.debug(
                            "ModbusNode [%s]: write register addr=%d val=%d dev=%d (attempt %d)",
                            self._entry_id[:8], address, count, device_id, attempt,
                        )
                        response = await self._call_client(
                            self.modbus_client.write_register, address, count, device_id
                        )
                    elif command == MODBUS_CMD_WRITE_MULTIPLE:  # Write Multiple Registers (count = zoznam hodnôt)
                        LOGGER.debug(
                            "ModbusNode [%s]: write registers addr=%d values=%s dev=%d (attempt %d)",
                            self._entry_id[:8], address, count, device_id, attempt,
                        )
                        response = await self._call_client(
                            self.modbus_client.write_registers, address, list(count), device_id
                        )
                    else:
                        result['error'] = f"Unsupported command: {command}"
                        self._log_failure(device_id, command, result['error'])
                        self.error_code |= MODBUS_ERROR_UNKNOWN
                        return result
                except Exception as e:
                    result['error'] = f"Modbus call exception (attempt {attempt}): {str(e)}"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_COMMUNICATION
                    return result

                if response and not response.isError():
                    result['success'] = True
                    result['data'] = response
                    self.error_code = MODBUS_ERROR_NONE
                else:
                    result['error'] = f"Modbus communication error (attempt {attempt}): {response}"
                    self._log_failure(device_id, command, result['error'])
                    self.error_code |= MODBUS_ERROR_COMMUNICATION

        except Exception as e:
            result['error'] = f"Exception in modbus_communication (attempt {attempt}): {str(e)}"
            self._log_failure(device_id, command, result['error'])
            self.error_code |= MODBUS_ERROR_UNKNOWN

        return result

    @staticmethod
    def _safe_port(value, default: int) -> int:
        """Konvertuje hodnotu portu na int."""
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default
