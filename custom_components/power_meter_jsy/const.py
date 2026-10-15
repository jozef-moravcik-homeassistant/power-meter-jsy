from __future__ import annotations
"""The Power Meter JSY"""
"""Author: Jozef Moravcik"""
"""email: jozef.moravcik@moravcik.eu"""

""" const.py """

"""Konštanty pre integráciu Power Meter JSY."""

import dataclasses

DOMAIN = "power_meter_jsy"
NAME = "Power Meter JSY"
MANUFACTURER = "Jozef Moravcik"
MODEL = "JSY-MK-138"
VERSION = "1.01.01"
DOCUMENTATION_URL = "https://github.com/jozef-moravcik-homeassistant/power-meter-jsy"

# ---------------------------------------------------------------------------
# Typy zariadení
# ---------------------------------------------------------------------------
CONF_DEVICE_TYPE = "device_type"
CONF_DEVICE_NAME = "device_name"

DEVICE_TYPE_MODBUS_NODE = "modbus_node"
DEVICE_TYPE_POWER_METER = "power_meter"
DEVICE_TYPE_TOOLS = "tools"        # sprievodca (nevytvára zariadenie): zmena ID a komunikačných parametrov merača

DEVICE_TYPE_OPTIONS = [
    DEVICE_TYPE_MODBUS_NODE,
    DEVICE_TYPE_POWER_METER,
    DEVICE_TYPE_TOOLS,
]
DEFAULT_DEVICE_TYPE = DEVICE_TYPE_MODBUS_NODE

# Platformy
MODBUS_NODE_PLATFORMS = ["sensor"]
POWER_METER_PLATFORMS = ["sensor"]

# ---------------------------------------------------------------------------
# Modbus Node
# ---------------------------------------------------------------------------
# Prefix pre entity_id zariadenia Modbus Node
MODBUS_NODE_ENTITY_ID_PREFIX = "power_meter_jsy_node"

# Číslo Modbus Node zariadenia (1, 2, 3, ...)
CONF_MODBUS_NODE_NUMBER = "modbus_node_number"

# Entity pre zariadenie Modbus Node
ENTITY_MODBUS_NODE_SENSOR_ERROR_CODE = "error_code"

# Referencia Power Meter na Modbus Node (entry_id)
CONF_MODBUS_NODE_ENTRY_ID = "modbus_node_entry_id"
DEFAULT_MODBUS_NODE_ENTRY_ID = ""

# Modbus connection configuration ############################################################################################
CONF_MODBUS_CONNECTION_TYPE = "modbus_connection_type"
MODBUS_CONNECTION_EXISTING_NODE = "existing_node"
MODBUS_CONNECTION_USB           = "usb"
MODBUS_CONNECTION_TCP           = "tcp"
MODBUS_CONNECTION_UDP           = "udp"
MODBUS_CONNECTION_TYPE_OPTIONS = [
    MODBUS_CONNECTION_EXISTING_NODE,
    MODBUS_CONNECTION_USB,
    MODBUS_CONNECTION_TCP,
    MODBUS_CONNECTION_UDP,
]
DEFAULT_MODBUS_CONNECTION_TYPE = MODBUS_CONNECTION_EXISTING_NODE

CONF_MODBUS_NODE_NAME             = "modbus_node_name"
CONF_MODBUS_PORT                  = "port"
CONF_MODBUS_BAUDRATE              = "baudrate"
CONF_MODBUS_BYTESIZE              = "bytesize"
CONF_MODBUS_STOPBITS              = "stopbits"
CONF_MODBUS_PARITY                = "parity"
CONF_MODBUS_DELAY                 = "delay"
CONF_MODBUS_MESSAGE_WAIT          = "message_wait_millisecond"
CONF_MODBUS_TIMEOUT               = "timeout"
CONF_MODBUS_HOST                  = "host"

DEFAULT_MODBUS_BAUDRATE           = 9600
DEFAULT_MODBUS_BYTESIZE           = 8
DEFAULT_MODBUS_STOPBITS           = 1
DEFAULT_MODBUS_PARITY             = "N"
DEFAULT_MODBUS_DELAY              = 1
DEFAULT_MODBUS_MESSAGE_WAIT       = 50
DEFAULT_MODBUS_TIMEOUT            = 1
DEFAULT_MODBUS_TCP_PORT           = 502

# Fyzické zariadenie JSY-MK-138 podporuje iba tieto rýchlosti
MODBUS_BAUDRATE_OPTIONS  = [9600, 19200, 38400]
MODBUS_BYTESIZE_OPTIONS  = [5, 6, 7, 8]
MODBUS_STOPBITS_OPTIONS  = [1, 2]
# N = None, E = Even, O = Odd, M = Mark
MODBUS_PARITY_OPTIONS    = ["N", "E", "O", "M"]

# Modbus Node error codes (bitové príznaky) ##################################################################################
MODBUS_ERROR_NONE           = 0     # Žiadna chyba
MODBUS_ERROR_PORT_OPEN      = 1     # bit0: nepodarilo sa otvoriť modbus port
MODBUS_ERROR_COMMUNICATION  = 2     # bit1: nepodarilo sa komunikovať so zariadením
MODBUS_ERROR_UNKNOWN        = 128   # bit7: neznáma chyba

# Modbus funkčné kódy
MODBUS_CMD_READ_HOLDING  = 3
MODBUS_CMD_WRITE_SINGLE  = 6
MODBUS_CMD_WRITE_MULTIPLE = 16     # 0x10 – povinný pre zápis parametrov JSY-MK-138 (ochrana proti náhodnému zápisu)

# ---------------------------------------------------------------------------
# Power Meter
# ---------------------------------------------------------------------------
# Prefix pre entity_id zariadenia Power Meter
POWER_METER_ENTITY_ID_PREFIX = "power_meter_jsy"

# Číslo Power Meter zariadenia (1, 2, 3, ...)
CONF_POWER_METER_NUMBER = "power_meter_number"

# Modbus ID (slave address) merača – nastavuje sa na Power Meter, nie na Modbus Node,
# pretože na jednej RS485 linke môže byť viac meračov s rôznymi ID.
CONF_MODBUS_DEVICE_ID   = "modbus_device_id"
DEFAULT_MODBUS_DEVICE_ID = 1
MIN_MODBUS_DEVICE_ID     = 1
MAX_MODBUS_DEVICE_ID     = 254

# Interval čítania (s)
CONF_SCAN_INTERVAL      = "scan_interval"
DEFAULT_SCAN_INTERVAL   = 2
MIN_SCAN_INTERVAL       = 2          # zariadenie odpovedá pomaly: kratší interval nie je povolený
MAX_SCAN_INTERVAL       = 100000

# ---------------------------------------------------------------------------
# Identifikácia zariadenia (read-only registre 0x0000 až 0x0003, čítajú sa jedným paketom)
# ---------------------------------------------------------------------------
REG_DEVICE_INFO_ADDRESS = 0x0000
REG_DEVICE_INFO_COUNT   = 4
REG_MODEL               = 0     # 0x0000: model zariadenia (JSY-MK-138 = 0x0138)
REG_RESERVED            = 1     # 0x0001: rezerva
REG_VOLTAGE_RANGE       = 2     # 0x0002: napäťový rozsah v V (250 V = 0x00FA)
REG_CURRENT_RANGE       = 3     # 0x0003: rozsah meracích cievok (50 A = 0x0400)



@dataclasses.dataclass(frozen=True)
class ModelProfile:
    """Popis modelu merača: podľa neho sa čítajú dáta a vytvárajú entity.

    Všetky telemetrické údaje sa čítajú JEDNÝM Modbus paketom (Read Holding Registers, 0x03):
    block_count registrov od adresy block_address. Podľa dokumentácie sú všetky hodnoty UNSIGNED
    (energia je 32-bitová zo 2 registrov), preto sa ukladajú bez znamienka.
    """
    name: str                        # zobrazený názov modelu
    channels: int                    # počet meracích kanálov
    registers_per_channel: int = 7   # napätie, prúd, výkon, energia (2x), cos φ, frekvencia
    block_address: int = 0x0040      # začiatok telemetrického bloku
    # Priradenie fáz (r/w registre): None = model nemá toto nastavenie.
    # Každý register nesie 2 kanály (High Byte = nepárny kanál, Low Byte = párny kanál).
    phase_address: int | None = None
    phase_register_count: int = 0

    @property
    def block_count(self) -> int:
        return self.channels * self.registers_per_channel


# Známe modely: hodnota registra 0x0000 -> profil. Ďalšie modely sa pridávajú sem.
MODEL_PROFILES = {
    0x0138: ModelProfile(name="JSY-MK-138", channels=12, phase_address=0x0010, phase_register_count=6),
}
# Hodnota registra 0x0000 -> názov modelu
MODEL_NAMES = {code: profile.name for code, profile in MODEL_PROFILES.items()}
# Prepočet registra 0x0003 na ampéry: 0x0400 (1024) zodpovedá 50 A
CURRENT_RANGE_FULL_SCALE_RAW = 0x0400
CURRENT_RANGE_FULL_SCALE_A   = 50.0

# Údaje o zariadení uložené v config entry po prečítaní z merača
CONF_DEVICE_MODEL_CODE   = "device_model_code"
CONF_DEVICE_MODEL        = "device_model"
CONF_VOLTAGE_RANGE_V     = "voltage_range_v"
CONF_CURRENT_RANGE_RAW   = "current_range_raw"

# ---------------------------------------------------------------------------
# Telemetria – mapa registrov podľa modelu (viď ModelProfile)
# ---------------------------------------------------------------------------
# Maximálny počet kanálov, pre ktoré sa vytvárajú telemetrické entity.
# Skutočný počet je min(kanály modelu, CHANNEL_COUNT_ACTIVE).
CHANNEL_COUNT_ACTIVE = 12

# Offsety veličín v rámci jedného kanála (index registra od začiatku kanála), podľa Modbus dokumentácie
# (CH1: 0x0040 napätie, 0x0041 prúd, 0x0042 výkon, 0x0043-0x0044 energia, 0x0045 cos φ, 0x0046 frekvencia)
OFFSET_VOLTAGE = 0       # 0x0040
OFFSET_CURRENT = 1       # 0x0041
OFFSET_POWER = 2         # 0x0042
OFFSET_ENERGY = 3        # 0x0043 ~ 0x0044 (32-bit, vyšší register prvý)
OFFSET_POWER_FACTOR = 5  # 0x0045
OFFSET_FREQUENCY = 6     # 0x0046

# Všetky hodnoty sú neznamienkové. Hodnoty mimo rozsahu <MIN, MAX> => entita je unavailable.
VOLTAGE_SCALE = 0.01     # 0,01 V
VOLTAGE_MIN_V = 0.0
VOLTAGE_MAX_V = 300.0
VOLTAGE_PRECISION = 1

# Minimálne napätie kanála, pri ktorom sú frekvencia a cos φ považované za platné.
# Pri nižšom napätí (odpojený vstup, rušenie) sú tieto entity unavailable.
MIN_VOLTAGE_FOR_VALID_MEASUREMENT_V = 20.0

CURRENT_SCALE = 0.001    # 0,001 A
CURRENT_MIN_A = 0.0
CURRENT_MAX_A = 100.0
CURRENT_PRECISION = 3

POWER_SCALE = 1.0        # 1 W
POWER_MIN_W = 0.0
POWER_MAX_W = 65535.0
POWER_PRECISION = 0

ENERGY_SCALE = 0.01      # 0,01 kWh
ENERGY_MIN_KWH = 0.0
ENERGY_MAX_KWH = 42949672.95
ENERGY_PRECISION = 2

POWER_FACTOR_SCALE = 0.001
POWER_FACTOR_MIN = 0.0
POWER_FACTOR_MAX = 1.0
POWER_FACTOR_PRECISION = 3

FREQUENCY_SCALE = 0.01   # 0,01 Hz
FREQUENCY_MIN_HZ = 0.0
FREQUENCY_MAX_HZ = 100.0
FREQUENCY_PRECISION = 2

# Názvy entít / kľúče pre dáta v koordinátore
ENTITY_RAW_DATA = "raw_data"
ENTITY_MODEL = "model"
ENTITY_VOLTAGE_RANGE = "voltage_range"
ENTITY_CIRCUIT_RANGE = "circuit_range"
ATTR_RAW_DATA   = "raw_data"


# ---------------------------------------------------------------------------
# Priradenie fáz k meracím kanálom (registre 0x0010 až 0x0015 pri JSY-MK-138)
# ---------------------------------------------------------------------------
# Možnosti selectu -> hodnota v registri (01 = Fáza A / UA, 02 = Fáza B / UB, 03 = Fáza C / UC)
PHASE_OPTION_TO_CODE = {"a": 1, "b": 2, "c": 3}
PHASE_CODE_TO_OPTION = {code: option for option, code in PHASE_OPTION_TO_CODE.items()}
DEFAULT_PHASE_OPTION = "a"
CONF_PHASE_PREFIX = "phase_ch"      # kľúče polí formulára: phase_ch01 ... phase_ch12


# ---------------------------------------------------------------------------
# Spoľahlivosť komunikácie so zariadením (pomalé odpovede > 1 s)
# ---------------------------------------------------------------------------
# Minimálna medzera medzi dvoma paketmi pre to isté zariadenie (s)
MODBUS_MIN_PACKET_GAP_S = 0.2

# Zápis konfigurácie (priradenie fáz): počet pokusov a FIXNÁ perióda medzi nimi (nezávislá od intervalu čítania)
PHASE_WRITE_ATTEMPTS = 3
PHASE_WRITE_RETRY_DELAY_S = 2.0
# Pauza medzi potvrdením zápisu a spätným čítaním (zariadenie potrebuje čas na spracovanie)
PHASE_VERIFY_DELAY_S = 1.0
# Predĺžený timeout odpovede počas zápisu a overenia (s)
CONFIG_TIMEOUT_S = 3.0

# Pauza čítania počas otvoreného konfiguračného okna: poistka, ak sa okno nedokončí (s)
HUB_RECONNECT_NUDGE_INTERVAL_S = 15   # najmenší odstup urýchlených pripojení core Modbus hubu
HUB_RECONNECT_WAIT_STEPS = 12          # čakanie na pripojenie hubu: 12 x 0,25 s
READ_HARD_TIMEOUT_S = 90            # najdlhšie trvanie jedného čítania (potom skončí chybou a čítanie pokračuje)
RESUME_REFRESH_TIMEOUT_S = 60       # najdlhšie trvanie prvého čítania po obnovení (potom sa čítanie naplánuje aj tak)
CONFIG_PAUSE_WATCHDOG_S = 300


# ---------------------------------------------------------------------------
# Tools – zmena ID a komunikačných parametrov zariadenia (register 0x0004, zápis funkciou 0x10)
# ---------------------------------------------------------------------------
# Register 0x0004: High Byte = Modbus ID zariadenia, Low Byte = formát dát (horné 2 bity) + rýchlosť (dolné 4 bity)
REG_COMM_ADDRESS = 0x0004
COMM_BROADCAST_ID = 0

# Kód rýchlosti v dolných 4 bitoch (zariadenie podporuje len tieto rýchlosti)
COMM_BAUDRATE_CODES = {9600: 6, 19200: 7, 38400: 8}

# Formát dát -> (kód v hornom 2 bity, parita, stop bity); 8 dátových bitov vždy
COMM_FORMATS = {
    "8N1": (0b00, "N", 1),
    "8E1": (0b01, "E", 1),
    "8O1": (0b10, "O", 1),
    "8N2": (0b11, "N", 2),
}
DEFAULT_COMM_FORMAT = "8N1"

CONF_TOOLS_USE_BROADCAST = "use_broadcast"
CONF_TOOLS_NEW_DEVICE_ID = "new_device_id"
CONF_TOOLS_NEW_BAUDRATE = "new_baudrate"
CONF_TOOLS_NEW_FORMAT = "new_data_format"
