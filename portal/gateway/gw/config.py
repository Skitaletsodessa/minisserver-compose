"""Gateway configuration: environment, feature flags and the SEED data (device meta, icon keywords, scenarios, service tiles).
The seed tables are inserted into the database on first start and are then DATA (editable), not code."""
import os

VERSION = "1.0.0"
DATA = os.environ.get("GW_DATA", "/data")
BIND = os.environ.get("GW_BIND", "127.0.0.1")
PORT = int(os.environ.get("GW_PORT", "17444"))
PUBLIC_URL = os.environ.get("GW_PUBLIC_URL", "https://minisserver.tail6bf4d5.ts.net:17443")
HA_URL = os.environ.get("GW_HA_URL", "http://127.0.0.1:8123")
HA_TOKEN_FILE = os.environ.get("GW_HA_TOKEN_FILE", "/secrets/ha-token")
SCHED_URL = os.environ.get("GW_SCHED_URL", "http://127.0.0.1:16444")
SCHED_TOKEN_ADMIN_FILE = os.environ.get("GW_SCHED_TOKEN_ADMIN_FILE", "/secrets/sched-admin")
SCHED_TOKEN_HOUSEHOLD_FILE = os.environ.get("GW_SCHED_TOKEN_HOUSEHOLD_FILE", "/secrets/sched-household")

# AdGuard origin (Task 29 Part 2): the admin password is a file mounted read-only into the container; never an environment variable
AGH_URL = os.environ.get("GW_AGH_URL", "http://127.0.0.1:3000/control")
AGH_PASSWORD_FILE = os.environ.get("GW_AGH_PASSWORD_FILE", "/secrets-agh/password")
PARITY_STATE_FILE = os.environ.get("GW_PARITY_STATE", "/ro/adguard-parity/state.json")
OUI_FILE = os.environ.get("GW_OUI_FILE", "/ro/ieee-data/oui.txt")
FILTER_FETCH_ALLOW_FILE = os.environ.get("GW_FILTER_FETCH_ALLOW", os.path.join(DATA, "filter-fetch-allow.txt"))   # operator-written; empty/absent in normal operation
ORIGIN_DNS = os.environ.get("GW_ORIGIN_DNS", "192.168.31.2")
REPLICA_DNS = os.environ.get("GW_REPLICA_DNS", "192.168.31.5")

# feature flags (the app builds its tabs from them); cameras stay off
FEATURES = {"parental": True, "cameras": False}

# limits (ADR 0007)
MAX_PHONES_TOTAL = 20
MAX_PHONES_PER_PERSON = 5
ENROL_TTL_S = 600
SETUP_TTL_S = 3600
PHONE_SLIDING_S = 90 * 86400
SESSION_IDLE_S = 12 * 3600
SESSION_ABS_S = 7 * 86400
SWITCH_CONFIRM_S = 6.0
VACUUM_CONFIRM_S = 12.0
STOP_GUARD_S = 120.0            # after stopping a running plan: watch this long for a robot that starts anyway (observed live: it starts up to ~1 min after the script)
STOP_GUARD_POLL_S = 2.0
DEBOUNCE_S = 1.5
PROBE_EVERY_S = 30
SSE_HEARTBEAT_S = 25
SSE_RESUME_S = 300
IDEM_TTL_S = 24 * 3600

# HA: what the gateway may touch (in addition to what the schedule service lists); everything else is refused by the gateway itself
HA_ALLOWED_SERVICES = {"switch": {"turn_on", "turn_off"}, "script": {"turn_on", "turn_off"}, "vacuum": {"pause", "start", "return_to_base"}}

# plug meta that HA/the schedule service do not carry: kind, power sensor (None = no trustworthy meter), switchable by the household
DEVICE_SEED = {
    "bathroom_towels": {"name": "Полотенцесушитель", "switch_mode": "timed", "kind": "dryer", "power_sensor": "sensor.bathroom_towels_power", "household_switchable": 1},
    "plant_rack1":     {"name": "Свет для растений — стеллаж 1", "kind": "lamp",  "power_sensor": "sensor.living_room_rack_power", "household_switchable": 1},
    "plant_rack2":     {"name": "Свет для растений — стеллаж 2", "kind": "lamp",  "power_sensor": "sensor.livingroom_rack_big_power", "household_switchable": 1},
    "plant_orchids":   {"name": "Свет для орхидей", "kind": "lamp",  "power_sensor": None, "household_switchable": 1},          # fake meter (always 0): no value shown
    "plant_citrus":    {"name": "Свет для цитрусов — 2 этаж", "kind": "lamp",  "power_sensor": None, "household_switchable": 1},          # GoLogi IP44: no metering at all (raw dps 1,9,38,40-44)
    "plant_cacti":     {"name": "Свет для кактусов (фитолампа)", "kind": "lamp",  "power_sensor": "sensor.minisserver_power", "household_switchable": 1},
    "parrot_uv":       {"name": "УФ-лампа для попугая", "switch_mode": "timed", "kind": "uv",    "power_sensor": "sensor.bedroom_parrot_power", "household_switchable": 1},
}

# robots and OUR scenarios = the existing HA scripts (never new automations)
VACUUM_SEED = {
    "bedroom": {"name": "Пылесос — спальня", "vacuum": "vacuum.xiaomi_ov51gl_e160_robot_cleaner", "script": "script.vacuum_bedroom_plan",
                "schedule_id": "vacuum_bedroom", "icon": "vacuum",
                "scenarios": {"morning": {"name": "Тихая уборка", "description": "Спальня: пылесос в режиме Silent, затем станция.", "variables": {"plan": "morning"}},
                              "evening": {"name": "Турбо + мойка", "description": "Спальня: пылесос Turbo, затем протирка Level 3.", "variables": {"plan": "evening"}}}},
    "livingroom": {"name": "Пылесос — гостиная", "vacuum": "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "script": "script.vacuum_livingroom_plan",
                   "schedule_id": "vacuum_livingroom", "icon": "vacuum",
                   "scenarios": {"full": {"name": "Полная уборка", "description": "Коридор, ванная, гостиная, кухня: пылесос и протирка, станция после пылесоса.", "variables": {}}}},
}

# keyword -> icon key (matched against the lower-cased display name and id); Russian and English; first match by priority wins
ICON_SEED = [
    ("сушилк", "towel_dryer"), ("towel", "towel_dryer"), ("dryer", "towel_dryer"),
    ("орхиде", "orchid"), ("orchid", "orchid"),
    ("кактус", "cactus"), ("cacti", "cactus"), ("cactus", "cactus"),
    ("попуг", "parrot_lamp"), ("parrot", "parrot_lamp"), ("uv", "parrot_lamp"), ("ультрафиолет", "parrot_lamp"),
    ("стеллаж", "plant_rack"), ("rack", "plant_rack"),
    ("растен", "plant"), ("plant", "plant"), ("лампа", "lamp"), ("light", "lamp"),
    ("пылесос", "vacuum"), ("vacuum", "vacuum"),
]

# service tiles (data): id, name, icon, tile url, probe (kind:target), visible_to
BASE = "https://minisserver.tail6bf4d5.ts.net"
SERVICE_SEED = [
    ("jellyfin",  "Jellyfin",            "jellyfin",       BASE + ":9443",  "http:http://192.168.31.2:8096/health",                "household"),
    ("immich",    "Immich",              "immich",         BASE + ":11443", "http:http://192.168.31.2:2283/api/server/ping",       "household"),
    ("navidrome", "Navidrome",           "navidrome",      BASE + ":10443", "http:http://192.168.31.2:4533/ping",                  "household"),
    ("adguard",   "AdGuard Home",        "adguard",        BASE + ":13443", "http:http://127.0.0.1:3000/",                         "admin"),
    ("adguard_pi", "AdGuard (Pi)",       "adguard",        "https://piserver.tail6bf4d5.ts.net", "dns:192.168.31.5",               "admin"),
    ("ha",        "Home Assistant",      "home_assistant", BASE + ":15443", "internal:ha",                                         "admin"),
    ("schedules", "Расписания",          "schedule",       BASE + ":16443", "internal:schedules",                                  "admin"),
    ("qbittorrent", "qBittorrent",       "qbittorrent",    BASE + ":12443", "http:http://192.168.31.2:8081/",                      "admin"),
    ("scrutiny",  "Scrutiny (диски)",    "scrutiny",       BASE,            "http:http://127.0.0.1:8080/api/health",               "admin"),
    ("child_ui",  "Родительский контроль (старая страница)", "shield", BASE + ":14443", "http:http://127.0.0.1:8099/",           "admin"),
]

# ── timed runs (Task 29 Part 1) ──
# switch_mode per device (editable data, seed default timed for the dryer and the UV lamp, plain for the plant lights): `timed` = the On button becomes
# "Включить на…" and a plain ON is refused; OFF is always allowed. Presets are filtered by the plug's max_on_minutes guardrail.
RUN_PRESETS = {"dryer": [30, 60, 120], "uv": [60, 120, 240], "lamp": [30, 60, 120, 240]}
RUN_EXTEND_MIN = 30
RUN_ARM_WAIT_S = 60.0         # how long the gateway waits for HA to load the run end (the applier needs 10-35 s, longer right after another change) before it switches nothing on
RUN_ARM_POLL_S = 1.0
