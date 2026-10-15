from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" coordinator.py """

"""DataUpdateCoordinator – číta telemetriu merača JEDNÝM Modbus paketom a udržiava všeobecné údaje o zariadení."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import *
from .device_info import INFO_KEYS, async_read_device_info, info_is_valid

LOGGER = logging.getLogger(__name__)


class PowerMeterCoordinator(DataUpdateCoordinator[list[int]]):
    """Číta telemetrický blok registrov jedným Modbus paketom.

    Dáta (coordinator.data) sú zoznam neznamienkových 16-bit hodnôt (podľa Modbus dokumentácie).
    Dĺžka a adresa bloku závisia od modelu zariadenia (ModelProfile).

    Všeobecné údaje o zariadení (model, napäťový rozsah, rozsah prúdu – registre 0x0000 až 0x0003)
    sú v coordinator.info. Znovu sa načítavajú:
      - pri každom štarte / reloade integrácie (prvé čítanie),
      - ak niektorý z údajov chýba,
      - ak model nie je medzi známymi modelmi.
    Ak sa zmenia oproti uloženým v config entry, uložia sa tam. Zmena po štarte spôsobí reload
    integrácie, pri ktorom sa entity vytvoria podľa nového modelu a nepotrebné sa odstránia.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device_name: str,
        modbus_node_entry_id: str,
        modbus_device_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{config_entry.entry_id[:8]}",
            # Interval sa obmedzuje na povolený rozsah (staršie zariadenia mohli mať kratší interval)
            update_interval=timedelta(seconds=max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(scan_interval)))),
        )
        self._entry_id = config_entry.entry_id
        self.device_name = device_name
        self.modbus_node_entry_id = modbus_node_entry_id
        self.modbus_device_id = modbus_device_id

        # Všeobecné údaje o zariadení – začína sa tým, čo je uložené v config entry
        self.info: dict = self._info_from_entry()
        # Po každom štarte sa údaje zo zariadenia načítajú znova
        self._force_info_read = True

        # Pauza čítania počas otvoreného konfiguračného okna zariadenia
        self._paused = False
        self._pause_watchdog: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------
    # Pauza čítania (konfiguračné okno)
    # ------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    @callback
    def pause(self) -> None:
        """Preruší pravidelné čítanie (kým je otvorené konfiguračné okno zariadenia).

        Poistka: ak sa čítanie neobnoví (okno sa nedokončí), obnoví sa samo po CONFIG_PAUSE_WATCHDOG_S.
        """
        self._paused = True
        self._unschedule_refresh()
        if self._pause_watchdog is not None:
            self._pause_watchdog()
        self._pause_watchdog = async_call_later(self.hass, CONFIG_PAUSE_WATCHDOG_S, self._watchdog_fired)
        LOGGER.debug("%s: polling paused", self.device_name)

    @callback
    def _watchdog_fired(self, _now) -> None:
        self._pause_watchdog = None
        if self._paused:
            LOGGER.warning("%s: polling resumed by watchdog (configuration was not finished)", self.device_name)
            self.hass.async_create_task(self.async_resume())

    async def async_resume(self) -> None:
        """Obnoví čítanie: hneď sa prečítajú dáta a časovač sa naplánuje odznova od konca čítania,
        takže ďalšie čítanie príde až po celom nastavenom intervale."""
        if self._pause_watchdog is not None:
            self._pause_watchdog()
            self._pause_watchdog = None
        if not self._paused:
            return
        self._paused = False
        LOGGER.debug("%s: polling resumed", self.device_name)
        try:
            # Čítanie nesmie visieť donekonečna (napr. linka s inými parametrami po zmene v Tools)
            await asyncio.wait_for(self.async_refresh(), timeout=RESUME_REFRESH_TIMEOUT_S)
        except asyncio.TimeoutError:
            LOGGER.warning("%s: first read after resume timed out", self.device_name)
        except Exception:  # noqa: BLE001 - obnovenie plánovania nesmie závisieť od výsledku čítania
            LOGGER.exception("%s: first read after resume failed", self.device_name)
        finally:
            # Záruka: pravidelné čítanie pokračuje, aj keď prvé čítanie zlyhalo alebo sa prerušilo
            if not self._paused and getattr(self, "_unsub_refresh", None) is None:
                self._schedule_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        """Počas pauzy sa ďalšie čítanie neplánuje."""
        if self._paused:
            return
        super()._schedule_refresh()

    async def async_shutdown(self) -> None:
        if self._pause_watchdog is not None:
            self._pause_watchdog()
            self._pause_watchdog = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Údaje o zariadení
    # ------------------------------------------------------------------
    def _info_from_entry(self) -> dict:
        entry = self.config_entry
        return {key: entry.options.get(key, entry.data.get(key)) for key in INFO_KEYS}

    @property
    def info_valid(self) -> bool:
        return info_is_valid(self.info)

    @property
    def profile(self) -> ModelProfile | None:
        """Profil aktuálneho modelu, alebo None ak model nie je známy."""
        return MODEL_PROFILES.get(self.info.get(CONF_DEVICE_MODEL_CODE))

    def _persist_info(self) -> None:
        """Uloží aktuálne údaje o zariadení do config entry (ak sa líšia od uložených).

        Zmena po štarte spôsobí reload integrácie (update listener), pri ktorom sa entity
        vytvoria podľa nového modelu a nepotrebné entity sa odstránia.
        """
        entry = self.config_entry
        if self._info_from_entry() == self.info:
            return
        new_data = {**entry.data, **self.info}
        new_options = {**entry.options}
        for key in INFO_KEYS:
            if key in new_options:           # options majú prednosť pred data
                new_options[key] = self.info[key]
        LOGGER.info(
            "%s: device info changed, saving to config entry (model: %s)",
            self.device_name, self.info.get(CONF_DEVICE_MODEL),
        )
        self.hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)

    def _apply_info(self, info: dict) -> None:
        old_model = self.info.get(CONF_DEVICE_MODEL_CODE)
        self.info = dict(info)

        if not info_is_valid(self.info):
            LOGGER.warning(
                "%s: unsupported device model %s (register 0x0000 = 0x%04X)",
                self.device_name, self.info.get(CONF_DEVICE_MODEL), self.info.get(CONF_DEVICE_MODEL_CODE) or 0,
            )
            # Neznámy model sa neukladá – nesmie vyvolať odstránenie existujúcich entít
            return

        if old_model is not None and old_model != self.info.get(CONF_DEVICE_MODEL_CODE):
            LOGGER.info("%s: device model changed to %s", self.device_name, self.info.get(CONF_DEVICE_MODEL))
        self._persist_info()

    # ------------------------------------------------------------------
    # Čítanie
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> list[int]:
        """Čítanie s pevným stropom trvania: zaseknuté čítanie (napr. po zmene parametrov linky) nesmie
        navždy zastaviť pravidelné čítanie – skončí chybou a ďalšie čítanie sa naplánuje."""
        try:
            return await asyncio.wait_for(self._read_data(), timeout=READ_HARD_TIMEOUT_S)
        except asyncio.TimeoutError as err:
            raise UpdateFailed("Modbus read timed out") from err

    async def _read_data(self) -> list[int]:
        from . import get_modbus_node_instance

        # Počas pauzy sa do zariadenia neposiela nič (ani ručná aktualizácia entity)
        if self._paused:
            return self.data if self.data is not None else []

        # Inštancia Modbus Node sa hľadá pri každom čítaní, takže prežije reload Modbus Node
        modbus_node = get_modbus_node_instance(self.hass, self.modbus_node_entry_id)
        if modbus_node is None:
            raise UpdateFailed("Modbus Node is not available")

        # Načítanie všeobecných údajov: po štarte, pri chýbajúcich údajoch alebo neznámom modeli
        if self._force_info_read or not self.info_valid:
            info, error = await async_read_device_info(
                self.hass, self.modbus_node_entry_id, self.modbus_device_id,
            )
            if error is not None:
                raise UpdateFailed(f"Cannot read device info: {error}")
            self._force_info_read = False
            self._apply_info(info)

        profile = self.profile
        if profile is None:
            # Nepodporovaný model – telemetria sa nečíta (entity sú unavailable), údaje o zariadení sa
            # pri ďalšom čítaní skúsia načítať znova
            return []

        result = await modbus_node.modbus_communication(
            self.modbus_device_id,
            MODBUS_CMD_READ_HOLDING,
            profile.block_address,
            profile.block_count,
        )

        if not result["success"]:
            raise UpdateFailed(result["error"] or "Modbus read failed")

        registers = getattr(result["data"], "registers", None)
        if registers is None or len(registers) < profile.block_count:
            raise UpdateFailed(
                f"Unexpected number of registers: "
                f"{0 if registers is None else len(registers)} (expected {profile.block_count})"
            )

        return [int(r) & 0xFFFF for r in registers[:profile.block_count]]
