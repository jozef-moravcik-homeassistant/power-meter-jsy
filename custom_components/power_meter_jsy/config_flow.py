from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" config_flow.py """

"""Config flow pre Power Meter JSY."""

import logging
import re

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import *
from . import tools
from .device_info import (
    async_read_device_info as _read_device_info,
    async_read_phases,
    async_write_phases,
    current_range_amps,
    phase_key,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pomocné funkcie
# ---------------------------------------------------------------------------

async def _get_modbus_node_names(hass: HomeAssistant) -> list[str]:
    """Vráti zoznam názvov existujúcich Modbus nodes (hubov) v systéme (core integrácia modbus)."""
    return list(hass.data.get("modbus", {}).keys())


async def _get_free_serial_ports(hass: HomeAssistant) -> list[str]:
    """Vráti dostupné sériové porty, ktoré ešte nie sú použité v Modbus nodes (type: serial)."""
    import serial.tools.list_ports  # pyserial je závislosť HA

    all_ports: list[str] = await hass.async_add_executor_job(
        lambda: sorted(p.device for p in serial.tools.list_ports.comports())
    )

    used_ports: set[str] = set()
    for hub in hass.data.get("modbus", {}).values():
        try:
            endpoint = getattr(hub, "endpoint", None)          # HA 2026.10+: ("serial", port)
            candidates = [endpoint[1]] if isinstance(endpoint, tuple) and len(endpoint) > 1 and endpoint[0] == "serial" else []
            candidates.append((getattr(hub, "_pb_params", None) or {}).get("port"))   # staršie verzie HA
            candidates.extend(getattr(hub, attr, None) for attr in ("port", "_port", "com_port"))
            for port_val in candidates:
                if isinstance(port_val, str) and port_val:
                    used_ports.add(port_val)
        except Exception:
            pass

    return [p for p in all_ports if p not in used_ports]


def _current(entry, key, default):
    """Získa aktuálnu hodnotu z options alebo data config_entry."""
    return entry.options.get(key, entry.data.get(key, default))


def _get_modbus_node_entries(hass: HomeAssistant) -> list[dict]:
    """Vráti zoznam existujúcich Modbus Node config entries ako [{"value": entry_id, "label": name}]."""
    nodes = []
    for e in hass.config_entries.async_entries(DOMAIN):
        if e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_MODBUS_NODE:
            nodes.append({"value": e.entry_id, "label": e.data.get(CONF_DEVICE_NAME, "Modbus Node")})
    return nodes


def _next_free_number(entries, pattern: str) -> int:
    """Najmenšie voľné číslo N pre názov zariadenia podľa regulárneho výrazu (napr. 'Modbus Node (\\d+)$')."""
    used: set[int] = set()
    for e in entries:
        m = re.match(pattern, e.data.get(CONF_DEVICE_NAME, ""))
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


def _baudrate_selector() -> SelectSelector:
    """Baudrate – fyzické zariadenie podporuje len 9600, 19200 a 38400."""
    return SelectSelector(SelectSelectorConfig(
        options=[str(x) for x in MODBUS_BAUDRATE_OPTIONS],
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="modbus_baudrate",
    ))


def _safe_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _format_current_range(raw: int) -> str:
    """Rozsah meracích cievok: surová hodnota registra 0x0003 a prepočet na A (0x0400 = 50 A)."""
    return f"{raw} (≈ {current_range_amps(raw):.1f} A)"


def _device_id_in_use(hass: HomeAssistant, node_entry_id: str, device_id: int, exclude_entry_id: str | None = None) -> bool:
    """True, ak na rovnakom Modbus Node už existuje iný Power Meter s rovnakým Modbus ID."""
    for e in hass.config_entries.async_entries(DOMAIN):
        if e.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_POWER_METER or e.entry_id == exclude_entry_id:
            continue
        if (
            _current(e, CONF_MODBUS_NODE_ENTRY_ID, "") == node_entry_id
            and _safe_int(_current(e, CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID), DEFAULT_MODBUS_DEVICE_ID) == device_id
        ):
            return True
    return False


# ===========================================================================
# Spoločné kroky pre Modbus Node (použité v ConfigFlow aj OptionsFlow)
# ===========================================================================

def _phase_selector() -> SelectSelector:
    """Priradenie fázy: Fáza A / B / C (popisy sú v prekladoch)."""
    return SelectSelector(SelectSelectorConfig(
        options=list(PHASE_OPTION_TO_CODE),
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="phase",
    ))


class _PhaseStepMixin:
    """Krok Power Meter: priradenie fáz k meracím kanálom (obsah závisí od modelu zariadenia).

    Pri vstupe do kroku sa aktuálne nastavenie načíta zo zariadenia. Po odoslaní sa zmenené
    nastavenie zapíše späť do zariadenia (nezmenené sa nezapisuje). Priradenie fáz sa NEUKLADÁ
    do konfigurácie – zdrojom pravdy je vždy zariadenie.
    """

    _phases: list[int] | None = None     # nastavenie prečítané zo zariadenia (kódy 1..3 po kanáloch)

    async def _finish_power_meter(self):  # pragma: no cover - implementuje potomok
        raise NotImplementedError

    def _phase_profile(self):
        profile = MODEL_PROFILES.get(self._data.get(CONF_DEVICE_MODEL_CODE))
        return profile if profile is not None and profile.phase_address is not None else None

    async def async_step_power_meter_phases(self, user_input=None):
        profile = self._phase_profile()
        if profile is None:
            # Model nemá nastavenie fáz – krok sa preskočí
            return await self._finish_power_meter()

        node_entry_id = self._data[CONF_MODBUS_NODE_ENTRY_ID]
        device_id = self._data[CONF_MODBUS_DEVICE_ID]
        errors = {}

        if self._phases is None:
            # Vstup do kroku: načítanie aktuálneho nastavenia zo zariadenia
            phases, error = await async_read_phases(self.hass, node_entry_id, device_id, profile)
            if error is not None:
                # Prázdny formulár s chybou; odoslanie skúsi čítanie znova
                return self.async_show_form(
                    step_id="power_meter_phases", data_schema=vol.Schema({}), errors={"base": error},
                    description_placeholders={"model": profile.name},
                )
            self._phases = phases
            user_input = None
            # Neplatná hodnota v registri (nie 1..3): upozornenie, vo formulári je predvolená Fáza A
            if any(code not in PHASE_CODE_TO_OPTION for code in phases):
                errors["base"] = "phase_invalid_value"

        defaults = {phase_key(i + 1): PHASE_CODE_TO_OPTION.get(code, DEFAULT_PHASE_OPTION)
                    for i, code in enumerate(self._phases)}

        if user_input is not None:
            new_phases = [
                PHASE_OPTION_TO_CODE.get(user_input.get(phase_key(i + 1)), old)
                for i, old in enumerate(self._phases)
            ]
            if new_phases == self._phases:
                return await self._finish_power_meter()
            error = await async_write_phases(self.hass, node_entry_id, device_id, profile, new_phases)
            if error is None:
                self._phases = new_phases
                return await self._finish_power_meter()
            errors["base"] = error
            defaults = {phase_key(i + 1): PHASE_CODE_TO_OPTION[code] for i, code in enumerate(new_phases)}

        schema = vol.Schema({
            vol.Required(phase_key(channel), default=defaults[phase_key(channel)]): _phase_selector()
            for channel in range(1, profile.channels + 1)
        })
        return self.async_show_form(
            step_id="power_meter_phases", data_schema=schema, errors=errors,
            description_placeholders={"model": profile.name},
        )


class _ModbusNodeStepsMixin:
    """Kroky nastavenia Modbus pripojenia. Používa self._data a _finish_modbus_node()."""

    async def _finish_modbus_node(self):  # pragma: no cover - implementuje potomok
        raise NotImplementedError

    def _node_used_by(self) -> str:
        """Zoznam zariadení Power Meter, ktoré používajú upravovaný Modbus Node (pre varovanie v options flow)."""
        return "–"

    def _modbus_step_id(self, base: str) -> str:
        """Krok Tools má vlastné okná (s vlastnými titulkami a popismi: opisujú AKTUÁLNE parametre zariadenia)."""
        if self._data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_TOOLS:
            return "tools_" + base[len("modbus_"):]
        return base

    # Kroky Tools sa spracúvajú rovnako ako kroky Modbus pripojenia (len s iným id okna)
    async def async_step_tools_connection_type(self, user_input=None):
        return await self.async_step_modbus_connection_type(user_input)

    async def async_step_tools_existing_node(self, user_input=None):
        return await self.async_step_modbus_existing_node(user_input)

    async def async_step_tools_usb(self, user_input=None):
        return await self.async_step_modbus_usb(user_input)

    async def async_step_tools_tcp(self, user_input=None):
        return await self.async_step_modbus_tcp(user_input)

    async def async_step_tools_udp(self, user_input=None):
        return await self.async_step_modbus_udp(user_input)

    # ------------------------------------------------------------------
    # Typ pripojenia
    # ------------------------------------------------------------------
    async def async_step_modbus_connection_type(self, user_input=None):
        errors = {}

        if user_input is not None:
            connection_type = user_input[CONF_MODBUS_CONNECTION_TYPE]
            self._data.update(user_input)

            if connection_type == MODBUS_CONNECTION_EXISTING_NODE:
                if not await _get_modbus_node_names(self.hass):
                    errors["base"] = "no_modbus_nodes"
                else:
                    return await self.async_step_modbus_existing_node()
            elif connection_type == MODBUS_CONNECTION_USB:
                return await self.async_step_modbus_usb()
            elif connection_type == MODBUS_CONNECTION_TCP:
                return await self.async_step_modbus_tcp()
            elif connection_type == MODBUS_CONNECTION_UDP:
                return await self.async_step_modbus_udp()

        data_schema = vol.Schema({
            vol.Required(
                CONF_MODBUS_CONNECTION_TYPE,
                default=self._data.get(CONF_MODBUS_CONNECTION_TYPE, DEFAULT_MODBUS_CONNECTION_TYPE),
            ): SelectSelector(SelectSelectorConfig(
                options=MODBUS_CONNECTION_TYPE_OPTIONS,
                mode=SelectSelectorMode.LIST,
                translation_key="modbus_connection_type",
            )),
        })
        return self.async_show_form(
            step_id=self._modbus_step_id("modbus_connection_type"), data_schema=data_schema, errors=errors,
            description_placeholders={"used_by": self._node_used_by()},
        )

    # ------------------------------------------------------------------
    # Existujúci Modbus Node (core integrácia modbus)
    # ------------------------------------------------------------------
    async def async_step_modbus_existing_node(self, user_input=None):
        errors = {}

        if user_input is not None:
            self._data["node_" + CONF_MODBUS_NODE_NAME] = user_input[CONF_MODBUS_NODE_NAME]
            self._data[CONF_MODBUS_NODE_NAME] = user_input[CONF_MODBUS_NODE_NAME]
            return await self._finish_modbus_node()

        nodes = await _get_modbus_node_names(self.hass)
        current_node = self._data.get("node_" + CONF_MODBUS_NODE_NAME, "")
        if current_node not in nodes:
            current_node = nodes[0] if nodes else ""

        data_schema = vol.Schema({
            vol.Required(CONF_MODBUS_NODE_NAME, default=current_node): SelectSelector(
                SelectSelectorConfig(options=nodes, mode=SelectSelectorMode.DROPDOWN)
            ),
        })
        return self.async_show_form(
            step_id=self._modbus_step_id("modbus_existing_node"), data_schema=data_schema, errors=errors,
        )

    # ------------------------------------------------------------------
    # USB port (type: serial)
    # ------------------------------------------------------------------
    async def async_step_modbus_usb(self, user_input=None):
        errors = {}

        if user_input is not None:
            for key in (
                CONF_MODBUS_PORT, CONF_MODBUS_BAUDRATE, CONF_MODBUS_BYTESIZE, CONF_MODBUS_STOPBITS,
                CONF_MODBUS_PARITY, CONF_MODBUS_DELAY, CONF_MODBUS_MESSAGE_WAIT, CONF_MODBUS_TIMEOUT,
            ):
                self._data["usb_" + key] = user_input[key]   # s prefixom (pamätá si hodnoty pri prepínaní typu)
                self._data[key] = user_input[key]            # bez prefixu (finálne uloženie)
            return await self._finish_modbus_node()

        available_ports = await _get_free_serial_ports(self.hass)
        current_port = self._data.get("usb_" + CONF_MODBUS_PORT, "")

        # Aktuálne nastavený port ostáva v zozname, aj keď ho práve používa tento Node
        if current_port and current_port not in available_ports:
            available_ports = [current_port] + available_ports

        if not available_ports:
            errors["base"] = "no_serial_ports"

        port_schema_field = (
            SelectSelector(SelectSelectorConfig(options=available_ports, mode=SelectSelectorMode.DROPDOWN))
            if available_ports
            else TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
        )

        if not (current_port and (current_port.startswith("/dev/") or current_port.startswith("COM") or current_port in available_ports)):
            current_port = available_ports[0] if available_ports else ""

        # Uložený baudrate mimo povolenej trojice sa nahradí predvoleným
        current_baud = str(self._data.get("usb_" + CONF_MODBUS_BAUDRATE, DEFAULT_MODBUS_BAUDRATE))
        if current_baud not in [str(x) for x in MODBUS_BAUDRATE_OPTIONS]:
            current_baud = str(DEFAULT_MODBUS_BAUDRATE)

        data_schema = vol.Schema({
            vol.Required(CONF_MODBUS_PORT, default=current_port): port_schema_field,
            vol.Required(CONF_MODBUS_BAUDRATE, default=current_baud): _baudrate_selector(),
            vol.Required(
                CONF_MODBUS_BYTESIZE,
                default=str(self._data.get("usb_" + CONF_MODBUS_BYTESIZE, DEFAULT_MODBUS_BYTESIZE)),
            ): SelectSelector(SelectSelectorConfig(
                options=[str(x) for x in MODBUS_BYTESIZE_OPTIONS],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="modbus_bytesize",
            )),
            vol.Required(
                CONF_MODBUS_STOPBITS,
                default=str(self._data.get("usb_" + CONF_MODBUS_STOPBITS, DEFAULT_MODBUS_STOPBITS)),
            ): SelectSelector(SelectSelectorConfig(
                options=[str(x) for x in MODBUS_STOPBITS_OPTIONS],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="modbus_stopbits",
            )),
            vol.Required(
                CONF_MODBUS_PARITY,
                default=str(self._data.get("usb_" + CONF_MODBUS_PARITY, DEFAULT_MODBUS_PARITY)),
            ): SelectSelector(SelectSelectorConfig(
                options=MODBUS_PARITY_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="modbus_parity",
            )),
            vol.Required(
                CONF_MODBUS_DELAY,
                default=self._data.get("usb_" + CONF_MODBUS_DELAY, DEFAULT_MODBUS_DELAY),
            ): NumberSelector(NumberSelectorConfig(min=0, max=3600, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(
                CONF_MODBUS_MESSAGE_WAIT,
                default=self._data.get("usb_" + CONF_MODBUS_MESSAGE_WAIT, DEFAULT_MODBUS_MESSAGE_WAIT),
            ): NumberSelector(NumberSelectorConfig(min=0, max=10000, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(
                CONF_MODBUS_TIMEOUT,
                default=self._data.get("usb_" + CONF_MODBUS_TIMEOUT, DEFAULT_MODBUS_TIMEOUT),
            ): NumberSelector(NumberSelectorConfig(min=0, max=300, step=1, mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(
            step_id=self._modbus_step_id("modbus_usb"), data_schema=data_schema, errors=errors)

    # ------------------------------------------------------------------
    # TCP / UDP
    # ------------------------------------------------------------------
    async def _network_step(self, prefix: str, step_id: str, user_input):
        if user_input is not None:
            for key in (CONF_MODBUS_HOST, CONF_MODBUS_PORT, CONF_MODBUS_TIMEOUT):
                self._data[prefix + key] = user_input[key]
                self._data[key] = user_input[key]
            return await self._finish_modbus_node()

        current_host = self._data.get(prefix + CONF_MODBUS_HOST, "")
        current_port = _safe_int(self._data.get(prefix + CONF_MODBUS_PORT, DEFAULT_MODBUS_TCP_PORT), DEFAULT_MODBUS_TCP_PORT)

        data_schema = vol.Schema({
            vol.Required(CONF_MODBUS_HOST, default=current_host): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_MODBUS_PORT, default=current_port): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_MODBUS_TIMEOUT,
                default=self._data.get(prefix + CONF_MODBUS_TIMEOUT, DEFAULT_MODBUS_TIMEOUT),
            ): NumberSelector(NumberSelectorConfig(min=0, max=300, step=1, mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(step_id=self._modbus_step_id(step_id), data_schema=data_schema)

    async def async_step_modbus_tcp(self, user_input=None):
        return await self._network_step("tcp_", "modbus_tcp", user_input)

    async def async_step_modbus_udp(self, user_input=None):
        return await self._network_step("udp_", "modbus_udp", user_input)


# ===========================================================================
# ConfigFlow
# ===========================================================================

class PowerMeterJsyConfigFlow(_ModbusNodeStepsMixin, _PhaseStepMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self._data = {}
        self._modbus_node_number = 1
        self._tools_model = None
        self._tools_paused = []         # koordinátory, ktorých čítanie Tools pozastavil

    # ------------------------------------------------------------------
    # Tools – pozastavenie čítania všetkých zariadení (kolízia s komunikáciou Tools)
    # ------------------------------------------------------------------
    @callback
    def _tools_pause_polling(self) -> None:
        """Preruší pravidelné čítanie všetkých Power Meter zariadení. Volá sa pri každom kroku Tools,
        čím sa zároveň predlžuje poistka (watchdog) koordinátora."""
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            coordinator = entry_data.get("coordinator") if isinstance(entry_data, dict) else None
            if coordinator is None:
                continue
            if coordinator not in self._tools_paused:
                if coordinator.paused:       # pozastavené niekým iným (okno nastavení) – nechá sa tak
                    continue
                self._tools_paused.append(coordinator)
            coordinator.pause()

    async def _tools_release_connections(self) -> None:
        """Uvoľní pripojenia všetkých Modbus Node (sériový port / TCP spojenie), aby ich Tools mohol použiť.
        Node si pripojenie pri ďalšej komunikácii vytvorí sám."""
        for entry_data in list(self.hass.data.get(DOMAIN, {}).values()):
            node = entry_data.get("modbus_node_instance") if isinstance(entry_data, dict) else None
            if node is not None:
                await node.release_connection()

    @callback
    def async_remove(self) -> None:
        """Pri skončení alebo zrušení sprievodcu sa čítanie všetkých zariadení obnoví."""
        paused, self._tools_paused = self._tools_paused, []
        for coordinator in paused:
            self.hass.async_create_task(coordinator.async_resume())

    # Táto metóda tu musí byť – presmeruje flow na prvý krok konfigurácie
    async def async_step_user(self, user_input=None):
        return await self.async_step_device_type(user_input)

    # ------------------------------------------------------------------
    # Krok 0 – Výber typu zariadenia
    # ------------------------------------------------------------------
    async def async_step_device_type(self, user_input=None):
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        has_modbus_node = any(
            e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_MODBUS_NODE for e in existing_entries
        )

        if user_input is not None:
            device_type = user_input[CONF_DEVICE_TYPE]
            self._data[CONF_DEVICE_TYPE] = device_type

            if device_type == DEVICE_TYPE_MODBUS_NODE:
                n = _next_free_number(existing_entries, r"Modbus Node (\d+)$")
                self._data[CONF_DEVICE_NAME] = f"Modbus Node {n}"
                self._data[CONF_MODBUS_NODE_NUMBER] = n
                self._modbus_node_number = n
                return await self.async_step_modbus_connection_type()

            if device_type == DEVICE_TYPE_TOOLS:
                # Sprievodca bez trvalého zariadenia: najprv parametre aktuálneho pripojenia k zariadeniu.
                # Počas sprievodcu sa čítanie všetkých zariadení preruší.
                self._tools_pause_polling()
                await self._tools_release_connections()
                return await self.async_step_modbus_connection_type()

            return await self.async_step_power_meter_settings()

        default_type = DEVICE_TYPE_POWER_METER if has_modbus_node else DEVICE_TYPE_MODBUS_NODE

        data_schema = vol.Schema({
            vol.Required(CONF_DEVICE_TYPE, default=default_type): SelectSelector(SelectSelectorConfig(
                options=DEVICE_TYPE_OPTIONS,
                mode=SelectSelectorMode.LIST,
                translation_key="device_type",
            )),
        })
        return self.async_show_form(step_id="device_type", data_schema=data_schema)

    # ------------------------------------------------------------------
    # Modbus Node – dokončenie (spoločné pre všetky typy pripojenia)
    # ------------------------------------------------------------------
    async def _finish_modbus_node(self):
        if self._data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_TOOLS:
            return await self.async_step_tools_device()
        device_name = self._data.get(CONF_DEVICE_NAME, "Modbus Node")
        await self.async_set_unique_id(f"{DOMAIN}_modbus_node_{self._modbus_node_number}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=device_name, data=self._data)


    # ------------------------------------------------------------------
    # Tools – zmena Modbus ID a komunikačných parametrov zariadenia (sprievodca, nič sa neukladá)
    # ------------------------------------------------------------------
    def _tools_current_format(self) -> str:
        if self._data.get(CONF_MODBUS_CONNECTION_TYPE) == MODBUS_CONNECTION_USB:
            return tools.format_of(
                self._data.get(CONF_MODBUS_PARITY, DEFAULT_MODBUS_PARITY),
                _safe_int(self._data.get(CONF_MODBUS_STOPBITS, DEFAULT_MODBUS_STOPBITS), DEFAULT_MODBUS_STOPBITS),
                _safe_int(self._data.get(CONF_MODBUS_BYTESIZE, DEFAULT_MODBUS_BYTESIZE), DEFAULT_MODBUS_BYTESIZE),
            )
        return DEFAULT_COMM_FORMAT

    def _tools_current_baudrate(self) -> int:
        if self._data.get(CONF_MODBUS_CONNECTION_TYPE) == MODBUS_CONNECTION_USB:
            baud = _safe_int(self._data.get(CONF_MODBUS_BAUDRATE, DEFAULT_MODBUS_BAUDRATE), DEFAULT_MODBUS_BAUDRATE)
            if baud in MODBUS_BAUDRATE_OPTIONS:
                return baud
        return DEFAULT_MODBUS_BAUDRATE

    async def async_step_tools_device(self, user_input=None):
        """Tools – krok 1: aktuálne ID zariadenia a spôsob zápisu (konkrétne ID / broadcast)."""
        self._tools_pause_polling()
        await self._tools_release_connections()
        errors = {}

        if user_input is not None:
            device_id = _safe_int(user_input[CONF_MODBUS_DEVICE_ID], DEFAULT_MODBUS_DEVICE_ID)
            broadcast = bool(user_input.get(CONF_TOOLS_USE_BROADCAST, False))
            self._data[CONF_MODBUS_DEVICE_ID] = device_id
            self._data[CONF_TOOLS_USE_BROADCAST] = broadcast
            self._tools_model = None
            if broadcast:
                # Pri broadcaste sa zariadenie nedá čítať (nie je známe jeho ID) – spojenie sa neoveruje
                return await self.async_step_tools_new_params()
            model_code = await tools.async_probe_connection(self.hass, self._data, device_id)
            if model_code is not None:
                self._tools_model = MODEL_NAMES.get(model_code, f"Unknown (0x{model_code:04X})")
                return await self.async_step_tools_new_params()
            errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(
                CONF_MODBUS_DEVICE_ID,
                default=self._data.get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID),
            ): NumberSelector(NumberSelectorConfig(
                min=MIN_MODBUS_DEVICE_ID, max=MAX_MODBUS_DEVICE_ID, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(
                CONF_TOOLS_USE_BROADCAST, default=self._data.get(CONF_TOOLS_USE_BROADCAST, False),
            ): BooleanSelector(),
        })
        return self.async_show_form(step_id="tools_device", data_schema=schema, errors=errors)

    async def async_step_tools_new_params(self, user_input=None):
        """Tools – krok 2: nové ID, rýchlosť a formát dát, ktoré sa zapíšu do zariadenia."""
        self._tools_pause_polling()
        errors = {}
        old = tools.CommSettings(
            self._data[CONF_MODBUS_DEVICE_ID], self._tools_current_baudrate(), self._tools_current_format(),
        )

        if user_input is not None:
            new = tools.CommSettings(
                _safe_int(user_input[CONF_TOOLS_NEW_DEVICE_ID], old.device_id),
                _safe_int(user_input[CONF_TOOLS_NEW_BAUDRATE], old.baudrate),
                user_input[CONF_TOOLS_NEW_FORMAT],
            )
            self._data[CONF_TOOLS_NEW_DEVICE_ID] = new.device_id
            self._data[CONF_TOOLS_NEW_BAUDRATE] = new.baudrate
            self._data[CONF_TOOLS_NEW_FORMAT] = new.data_format
            # Pri TCP/UDP/existujúcom Node sa aktuálna rýchlosť a formát nezadávajú, takže sa nedajú porovnať
            serial = self._data.get(CONF_MODBUS_CONNECTION_TYPE) == MODBUS_CONNECTION_USB
            if serial and not self._data.get(CONF_TOOLS_USE_BROADCAST) and new == old:
                errors["base"] = "tools_no_change"
            else:
                return await self.async_step_tools_confirm()

        schema = vol.Schema({
            vol.Required(
                CONF_TOOLS_NEW_DEVICE_ID,
                default=self._data.get(CONF_TOOLS_NEW_DEVICE_ID, old.device_id),
            ): NumberSelector(NumberSelectorConfig(
                min=MIN_MODBUS_DEVICE_ID, max=MAX_MODBUS_DEVICE_ID, step=1, mode=NumberSelectorMode.BOX)),
            vol.Required(
                CONF_TOOLS_NEW_BAUDRATE, default=str(self._data.get(CONF_TOOLS_NEW_BAUDRATE, old.baudrate)),
            ): _baudrate_selector(),
            vol.Required(
                CONF_TOOLS_NEW_FORMAT, default=self._data.get(CONF_TOOLS_NEW_FORMAT, old.data_format),
            ): SelectSelector(SelectSelectorConfig(
                options=list(COMM_FORMATS), mode=SelectSelectorMode.DROPDOWN, translation_key="data_format",
            )),
        })
        return self.async_show_form(step_id="tools_new_params", data_schema=schema, errors=errors)

    async def async_step_tools_confirm(self, user_input=None):
        """Tools – krok 3: zhrnutie, potvrdenie a zápis do zariadenia s overením."""
        self._tools_pause_polling()
        errors = {}
        broadcast = bool(self._data.get(CONF_TOOLS_USE_BROADCAST))
        old = tools.CommSettings(
            self._data[CONF_MODBUS_DEVICE_ID], self._tools_current_baudrate(), self._tools_current_format(),
        )
        new = tools.CommSettings(
            self._data[CONF_TOOLS_NEW_DEVICE_ID], self._data[CONF_TOOLS_NEW_BAUDRATE],
            self._data[CONF_TOOLS_NEW_FORMAT],
        )
        serial = self._data.get(CONF_MODBUS_CONNECTION_TYPE) == MODBUS_CONNECTION_USB
        placeholders = {
            "target": "0 (broadcast)" if broadcast else str(old.device_id),
            "model": self._tools_model or "?",
            "old_id": "?" if broadcast else str(old.device_id),
            "old_baud": str(old.baudrate) if serial else "?", "old_format": old.data_format if serial else "?",
            "new_id": str(new.device_id), "new_baud": str(new.baudrate), "new_format": new.data_format,
        }

        if user_input is not None:
            await self._tools_release_connections()
            result = await tools.async_apply_settings(self.hass, self._data, old, new, broadcast)
            if result.status == tools.STATUS_WRITE_FAILED:
                errors["base"] = "write_failed"
            else:
                return self.async_abort(reason=f"tools_{result.status}", description_placeholders=placeholders)

        return self.async_show_form(
            step_id="tools_confirm", data_schema=vol.Schema({}), errors=errors,
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------
    # Power Meter – krok 1: Modbus Node, Modbus ID, interval čítania
    # ------------------------------------------------------------------
    async def async_step_power_meter_settings(self, user_input=None):
        errors = {}
        modbus_nodes = _get_modbus_node_entries(self.hass)
        if not modbus_nodes:
            errors["base"] = "no_modbus_node_entries"

        if user_input is not None and modbus_nodes:
            device_id = _safe_int(user_input[CONF_MODBUS_DEVICE_ID], DEFAULT_MODBUS_DEVICE_ID)
            node_entry_id = user_input[CONF_MODBUS_NODE_ENTRY_ID]
            self._data.update(user_input)
            self._data[CONF_MODBUS_DEVICE_ID] = device_id
            self._data[CONF_SCAN_INTERVAL] = _safe_int(user_input[CONF_SCAN_INTERVAL], DEFAULT_SCAN_INTERVAL)

            if _device_id_in_use(self.hass, node_entry_id, device_id):
                errors["base"] = "device_id_in_use"
            else:
                # Po potvrdení sa zo zariadenia načítajú základné parametre (0x0000, 0x0002, 0x0003)
                info, error = await _read_device_info(self.hass, node_entry_id, device_id)
                if error is None:
                    self._data.update(info)
                    return await self.async_step_power_meter_device_info()
                errors["base"] = error

        schema_fields = {}
        if modbus_nodes:
            current_node = self._data.get(CONF_MODBUS_NODE_ENTRY_ID, "")
            if current_node not in [n["value"] for n in modbus_nodes]:
                current_node = modbus_nodes[0]["value"]
            schema_fields[vol.Required(CONF_MODBUS_NODE_ENTRY_ID, default=current_node)] = SelectSelector(
                SelectSelectorConfig(options=modbus_nodes, mode=SelectSelectorMode.DROPDOWN)
            )
        schema_fields[vol.Required(
            CONF_MODBUS_DEVICE_ID,
            default=self._data.get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID),
        )] = NumberSelector(NumberSelectorConfig(
            min=MIN_MODBUS_DEVICE_ID, max=MAX_MODBUS_DEVICE_ID, step=1, mode=NumberSelectorMode.BOX))
        schema_fields[vol.Required(
            CONF_SCAN_INTERVAL,
            default=self._data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )] = NumberSelector(NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1, mode=NumberSelectorMode.BOX,
            unit_of_measurement="s"))

        return self.async_show_form(
            step_id="power_meter_settings", data_schema=vol.Schema(schema_fields), errors=errors,
        )

    # ------------------------------------------------------------------
    # Power Meter – krok 2: parametre načítané zo zariadenia (model, rozsahy)
    # ------------------------------------------------------------------
    async def async_step_power_meter_device_info(self, user_input=None):
        """Zobrazí parametre prečítané zo zariadenia. Potvrdením sa vytvorí zariadenie."""
        if user_input is not None:
            # Ďalší krok: nastavenia špecifické pre model (priradenie fáz)
            self._phases = None
            return await self.async_step_power_meter_phases()

        return self.async_show_form(
            step_id="power_meter_device_info",
            data_schema=vol.Schema({}),
            description_placeholders={
                "model": self._data[CONF_DEVICE_MODEL],
                "model_code": f"0x{self._data[CONF_DEVICE_MODEL_CODE]:04X}",
                "voltage_range": f"{self._data[CONF_VOLTAGE_RANGE_V]} V",
                "current_range": _format_current_range(self._data[CONF_CURRENT_RANGE_RAW]),
            },
        )

    async def _finish_power_meter(self):
        """Vytvorenie zariadenia Power Meter po poslednom kroku."""
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        n = _next_free_number(existing_entries, r"Power Meter (\d+)$")
        self._data[CONF_DEVICE_NAME] = f"Power Meter {n}"
        self._data[CONF_POWER_METER_NUMBER] = n

        await self.async_set_unique_id(f"{DOMAIN}_power_meter_{n}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._data[CONF_DEVICE_NAME], data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return PowerMeterJsyOptionsFlowHandler()


# ===========================================================================
# OptionsFlow
# ===========================================================================

class PowerMeterJsyOptionsFlowHandler(_ModbusNodeStepsMixin, _PhaseStepMixin, config_entries.OptionsFlow):
    """Options flow – zmena nastavení Modbus Node alebo Power Meter."""

    def __init__(self):
        super().__init__()
        self._data = {}
        self._base_data_loaded = False
        self._paused_coordinator = None     # koordinátor, ktorého čítanie je pozastavené počas okna
        self._skip_resume = False           # po uložení zmien sa integrácia načíta znova (čítanie sa spustí samo)

    def _node_used_by(self) -> str:
        """Názvy zariadení Power Meter, ktoré používajú tento Modbus Node ('–' ak žiadne)."""
        names = sorted(
            _current(e, CONF_DEVICE_NAME, e.title)
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_POWER_METER
            and _current(e, CONF_MODBUS_NODE_ENTRY_ID, "") == self.config_entry.entry_id
        )
        return ", ".join(names) if names else "–"

    def _coordinator(self):
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        return entry_data.get("coordinator") if isinstance(entry_data, dict) else None

    @callback
    def async_remove(self) -> None:
        """Volá sa pri skončení alebo zrušení okna: obnoví pravidelné čítanie (okamžité čítanie + reset časovača)."""
        coordinator, self._paused_coordinator = self._paused_coordinator, None
        if coordinator is not None and not self._skip_resume:
            self.hass.async_create_task(coordinator.async_resume())

    async def _finish_modbus_node(self):
        return self.async_create_entry(title="", data=self._data)

    async def _finish_power_meter(self):
        # Ak sa zmenili nastavenia, HA integráciu načíta znova a nový koordinátor hneď prečíta dáta
        # a spustí časovač. Inak sa čítanie obnoví v async_remove().
        self._skip_resume = dict(self.config_entry.options) != dict(self._data)
        return self.async_create_entry(title="", data=self._data)

    async def async_step_init(self, user_input=None):
        # Načítať VŠETKY aktuálne hodnoty z config_entry hneď na začiatku
        if not self._base_data_loaded:
            self._base_data_loaded = True
            current_data = dict(self.config_entry.data)
            current_data.update(self.config_entry.options)
            self._data.update(current_data)

        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_POWER_METER)

        if device_type == DEVICE_TYPE_MODBUS_NODE:
            return await self.async_step_modbus_connection_type()

        # Power Meter: počas konfigurácie sa pravidelné čítanie preruší (zariadenie odpovedá pomaly)
        coordinator = self._coordinator()
        if coordinator is not None and self._paused_coordinator is None:
            coordinator.pause()
            self._paused_coordinator = coordinator

        return await self.async_step_power_meter_settings()

    # ------------------------------------------------------------------
    # Power Meter – nastavenie (Options)
    # ------------------------------------------------------------------
    async def async_step_power_meter_settings(self, user_input=None):
        errors = {}
        modbus_nodes = _get_modbus_node_entries(self.hass)
        if not modbus_nodes:
            errors["base"] = "no_modbus_node_entries"

        if user_input is not None and modbus_nodes:
            device_id = _safe_int(user_input[CONF_MODBUS_DEVICE_ID], DEFAULT_MODBUS_DEVICE_ID)
            node_entry_id = user_input[CONF_MODBUS_NODE_ENTRY_ID]
            self._data.update(user_input)
            self._data[CONF_MODBUS_DEVICE_ID] = device_id
            self._data[CONF_SCAN_INTERVAL] = _safe_int(user_input[CONF_SCAN_INTERVAL], DEFAULT_SCAN_INTERVAL)

            if _device_id_in_use(self.hass, node_entry_id, device_id, exclude_entry_id=self.config_entry.entry_id):
                errors["base"] = "device_id_in_use"
            else:
                # Parametre zariadenia sa načítajú znova (po zmene Modbus Node / ID môže ísť o iný merač)
                info, error = await _read_device_info(self.hass, node_entry_id, device_id)
                if error is None:
                    self._data.update(info)
                    self._phases = None
                    return await self.async_step_power_meter_phases()
                errors["base"] = error

        schema_fields = {}
        if modbus_nodes:
            current_node = _current(self.config_entry, CONF_MODBUS_NODE_ENTRY_ID, "")
            if current_node not in [n["value"] for n in modbus_nodes]:
                current_node = modbus_nodes[0]["value"]
            schema_fields[vol.Required(CONF_MODBUS_NODE_ENTRY_ID, default=current_node)] = SelectSelector(
                SelectSelectorConfig(options=modbus_nodes, mode=SelectSelectorMode.DROPDOWN)
            )
        schema_fields[vol.Required(
            CONF_MODBUS_DEVICE_ID,
            default=_safe_int(_current(self.config_entry, CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID), DEFAULT_MODBUS_DEVICE_ID),
        )] = NumberSelector(NumberSelectorConfig(
            min=MIN_MODBUS_DEVICE_ID, max=MAX_MODBUS_DEVICE_ID, step=1, mode=NumberSelectorMode.BOX))
        schema_fields[vol.Required(
            CONF_SCAN_INTERVAL,
            default=max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, _safe_int(
                _current(self.config_entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL), DEFAULT_SCAN_INTERVAL))),
        )] = NumberSelector(NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1, mode=NumberSelectorMode.BOX,
            unit_of_measurement="s"))

        return self.async_show_form(
            step_id="power_meter_settings", data_schema=vol.Schema(schema_fields), errors=errors,
        )
