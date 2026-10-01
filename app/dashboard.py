"""Nur-Lese-API für das EVA-Dashboard (P8.D1) — **rein beobachtend**.

Dieses Modul liefert ausschließlich **lesende** Betriebsdaten für ein späteres
Frontend (P8.D2, Port 8767). Es startet **nichts**, schreibt **nichts**,
erzeugt **keinen** HA-Befehl und legt **keinen** Test-Hook an. Die API ist damit
sicher im Betrieb: ein Frontend, das pollt, kann keinen Turn auslösen.

**E96 (P9.T1) – die eine Schreibroute, ausdrücklich eng begrenzt.**  Es gibt
jetzt **genau eine** Route, die etwas verändert:
`POST /api/config/oww-threshold` mit dem Body `{"threshold": <float>}`.
Sie ist **nur** wirksam, wenn `DASHBOARD_WRITE_ENABLED=true` (Default
**aus** ⇒ HTTP **403**, damit ein Client „aus" erkennt), sie schreibt
**ausschließlich** `oww_threshold` (Float in **[0.0, 1.0]**, Fremdfeld ⇒ 400,
Wertfehler ⇒ 422), sie ruft **niemals** Home Assistant und **niemals** ein
Gerät.  **Kein** `/api/services`, **kein** TTS, **kein** LED-Puls – das
Dashboard schaltet nichts.  Sie schreibt den neuen Wert in die laufende
Settings-Instanz (⇒ wirkt **ohne** Manager-Neustart, weil
`app/wake_word._idle_threshold` die Schwelle bei jedem Vergleich aus den
Settings liest) **und** in die `.env` des Containers, damit ein Neustart sie
nicht verliert, und protokolliert jede Änderung auf `INFO` (im Dashboard-Log
sichtbar).  ⚠️ Das Projekt hat **keine** Auth/CSRF-Schicht: der Schreibmodus
gehört nur ins vertrauenswürdige LAN und wird nach dem Tuning wieder
abgeschaltet (STATE.md §3/§4, E96-Restrisiko).

**E111 (P11.T2) – die zweite Schreibroute, `PUT /api/config`.**  Es gibt
jetzt **zwei** Schreib-Routen: die obige `POST /api/config/oww-threshold`
(E96, genau eine Zahl) **und** `PUT /api/config`, das **mehrere** der
E111-Kern-Felder in einem Request schreibt (`oww_threshold`, `log_level`,
`audio_dump_enabled`, `whisper_language`, `oww_barge_in_threshold`,
`oww_cooldown_ms`, `wake_attempt_keep_floor`, `router_confidence_gate`,
`router_needs_param_threshold`, `jev_mode`, `ha_entity_domains`).  Dieselbe
Auth-Schicht (E110: `DASHBOARD_API_TOKEN` ⇒ 401 vor 403), dieselbe
Schreibmodus-Sperre (`DASHBOARD_WRITE_ENABLED` ⇒ 403), dieselbe Persistenz
(:func:`write_env_value`, eine Zeile pro Feld, Secrets bytegleich).  Die
Antwort teilt mit, welche Felder **ohne** Neustart wirken
(`effective_without_restart`) und welche einen Neustart brauchen
(`restarted_required`).  Auch hier gilt: **kein** HA-Call, **kein**
Geräteschalten, **kein** Secret in Antwort oder Log.

Verkabelung (bewusst entkoppelt von `app.main`)
----------------------------------------------
Exportiert wird genau ein `APIRouter`; der Hauptagent hängt ihn später in
`app/main.py` mit **einer** Zeile ein::

    from app import dashboard
    application.include_router(dashboard.router)

Alle Daten kommen aus **Dependency-Injection über den `Request`**
(`request.app.state.*`). Dort liegen laut `app/main.py` bereits `settings`,
`pipeline`, `ws_server`, `ha_client`, `mdns` und `connect_sequences` — es wird
**kein** `from app.main import …` gemacht, damit der Router ohne die App
importierbar und testbar bleibt. Fehlt eine Komponente, antwortet der
Endpunkt mit **200 + graceful defaults + `degraded: true`**, damit das
Dashboard **nie weiß ausfällt** (kein 500 aus einem Poller heraus).

Datenschutz / Geheimnisse
--------------------------
* **Nie** ausgegeben werden: API-Keys, Tokens (HA-/LLM-), `.env`-Inhalte,
  WLAN-Credentials, Pfade mit Secret-Werten.
* **Nie** ausgegeben werden Transkript-Text, DeepSeek-Antworttext und
  Audioinhalt — die Historie-Projektion arbeitet deshalb mit einer **Allowlist**
  von Feldern, kein Textfeld verlässt dieses Modul.
* Log-Zeilen werden über `redact()` maskiert (`Authorization:`, `Bearer …`,
  `api_key=`, `token=`, `password=`, Userinfo in URLs, …).
* `app.config`-Felder werden über eine **explizite Allowlist** projiziert und
  zusätzlich gegen Secret-Charakter (`key`/`token`/`password`/`secret`/
  `credential`) geprüft — ein `*-field` mit Secret-Charakter erreicht die
  Antwort **nie**, auch nicht maskiert.

P8.D3 – was sich geändert hat
----------------------------
* **Log-Stream (Teil 1).** :class:`MemoryLogBuffer` wird jetzt im `lifespan`
  von `app/main.py` **selbst** angehängt
  (:func:`attach_memory_log_buffer`) und beim Shutdown **zwingend** wieder
  entfernt (:func:`detach_memory_log_buffer`). Vorher war der Puffer nur eine
  Opt-In-Klasse ohne Benutzer ⇒ live `source: "none"`. Der Puffer ist
  **begrenzt** (:data:`LOG_BUFFER_MAXLEN`, `deque(maxlen=…)`), **idempotent**
  angehängt und **rekursionsfrei** (der Handler loggt nie selbst).
* **Config-`degraded` (Teil 2).** Die „fehlt"-Erkennung benutzt **nicht** mehr
  einen Objektidentitätsvergleich (`settings_obj is _module_settings` – immer
  wahr, weil `app/main.py` genau dieses Singleton in `app.state` legt),
  sondern die **Verfügbarkeit** in `request.app.state`. Siehe
  :func:`read_config` und :data:`SETTINGS_SOURCE_APP_STATE`.
* **Turn-Historie (Teil 3).** `app/pipeline.Pipeline` führt jetzt einen
  **begrenzten In-Memory-Ringpuffer** `Pipeline.turn_history` (P8.D3); dieser
  Leser war der P8.D1-Vertrag und bleibt unverändert. Neu in der Allowlist:
  `transcript` (**rotiert + gekürzt**, bewusst eine bewusste Abweichung von der
  P8.D1-Regel „kein Textfeld verlässt dieses Modul" – der Puffer erlaubt genau
  ein solches Feld, redigiert und längenbegrenzt), `target_entity_id`,
  `needs_param`, `extracted_param`, `error`, `error_code`, `barge_in`.
  `duration_ms`/`target_confidence`/`wake_score` aus dem P8.D3-Auftrag sind
  unter den **Leser-Namen** `duration_seconds`/`confidence`/`score` realisiert –
  `app/static/dashboard/app.js` ist unveränderlich und liest genau diese drei.

Ehrliche Grenzen (bewusst nicht kaschiert)
-----------------------------------------
* Die Pipeline führt **keinen** Previous-State und **keinen** Übergangs-
  Zeitpunkt (`app/pipeline.py` kennt nur `DeviceState.state`). Deshalb liefert
  `/api/state` `transitions_tracked: false` mit `previous_state: null` /
  `since: null`. **Nichts wird erfunden.**
* `app/logger.py` hat weiterhin **keinen** File-Handler (Container sammelt
  `stdout`) und installiert **selbst** keinen In-Memory-Puffer – das macht
  `app/main.py` im `lifespan`. Fällt der Handler aus (kein `lifespan`, z. B.
  `ASGITransport` im Test), fallen die Datei-Kandidaten und sonst `source:
  "none"` zurück.
* `app.logger` hat **keinen** File-Handler ⇒ `/api/logs` nutzt die
  In-Memory-Liste, sonst die jüngsten Logdateien aus einem konfigurierbaren
  Verzeichnis, sonst ehrlich `source: "none"`.
* Die API loggt auf Polling **nichts** (höchstens `DEBUG`), damit das
  Frontend keinen Log-Spam erzeugt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
from pathlib import Path
from typing import Any, Final, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import __version__
from app.config import ENV_FILE
from app.config import JEV_MODES
from app.config import LOG_LEVELS
from app.config import PROJECT_ROOT
from app.config import settings as _module_settings
from app.logger import (
    LOG_LEVEL_NAMES,
    LOGGER_NAMESPACE,
    configure_logging,
    get_logger,
)
from app.pipeline import PipelineState

_LOG: Final[logging.Logger] = get_logger("dashboard")

#: Anzeigename des Managers – bewusst **nicht** aus `app.main` importiert
#: (der Router muss ohne die App importierbar bleiben); spiegelt
#: `app.main.SERVICE_NAME`.
SERVICE_NAME: Final[str] = "wyoming-manager"

#: Startzeitpunkt **dieses Moduls** (monoton) ⇒ `uptime_seconds`. Bewusst kein
#: Zugriff auf den Container-Lifecycle: der Wert ist nach einem Uvicorn-Reload
#: wieder 0, was ehrlicher ist als eine geschätzte Container-Uptime.
MODULE_STARTED_AT: Final[float] = time.monotonic()

#: Der exportierte Router – vom Hauptagen per `include_router` einhängen.
router: Final[APIRouter] = APIRouter(prefix="/api", tags=["dashboard"])

# ── Grenzwerte / Defaults der Endpunkte ────────────────────────────────
DEFAULT_LOGS_LIMIT: Final[int] = 100
MAX_LOGS_LIMIT: Final[int] = 1000
DEFAULT_HISTORY_LIMIT: Final[int] = 50
MAX_HISTORY_LIMIT: Final[int] = 500
DEFAULT_DEPENDENCY_TIMEOUT: Final[float] = 2.0
MAX_DEPENDENCY_TIMEOUT: Final[float] = 10.0
#: Wie viele Bytes am Dateiende gelesen werden (billig, kein Volumen-Scan).
LOG_TAIL_BYTES: Final[int] = 64 * 1024
#: Dateiendungen, die als Logdatei gelten.
LOG_FILE_SUFFIXES: Final[tuple[str, ...]] = (".log", ".txt", ".out")
#: **Harter Cap** des In-Memory-Log-Puffers (P8.D3). `deque(maxlen=…)` ⇒ ältere
#: Records werden verworfen, der Speicher wächst **nie** unbegrenzt.
#: 2000 Zeilen à ≲ 400 B ⇒ grob ≲ 1 MB, davon abhängig wie lang die Zeilen sind.
LOG_BUFFER_MAXLEN: Final[int] = 2000
# ── Wake-Evidenz (P9.T0) ─────────────────────────────────────────────────
#: **Harter Cap** des Wake-Rings – identisch zu
#: `app.wake_word.WAKE_ATTEMPTS_MAXLEN`.  Hier **nicht** neu erfunden, sondern
#: die Zahl für die Antwort durchgereicht (`window`), damit der Nutzer sieht,
#: dass die Liste begrenzt ist und nicht „alles".
WAKE_ATTEMPTS_MAXLEN: Final[int] = 50
DEFAULT_WAKE_LIMIT: Final[int] = 50
MAX_WAKE_LIMIT: Final[int] = 200
#: Allowlist eines Wake-Versuchs – wie `HISTORY_FIELDS`, nur lesend.  Kein
#: Freitext, kein Audio, kein Rohframe (E94 (c) gilt hier unverändert).
WAKE_ATTEMPT_FIELDS: Final[tuple[str, ...]] = (
    "ts",
    "device_id",
    "score",
    "accepted",
    "reason",
    "threshold",
    "chunk_index",
)
#: Herkunft der angezeigten Schwelle – offen benannt, nie getarnt (E94 (d)):
#: ``device`` = die wirksame Schwelle des laufenden Detektors, ``settings`` =
#: nur der Config-Wert (kein Gerät/Detektor), ``none`` = gar nicht bekannt.
THRESHOLD_SOURCE_DEVICE: Final[str] = "device"
THRESHOLD_SOURCE_SETTINGS: Final[str] = "settings"
THRESHOLD_SOURCE_NONE: Final[str] = "none"

# ── Dashboard-Schreibmodus (E96, P9.T1) ───────────────────────────────────
#: **Der einzige** schreibbare Settings-Name.  Wörtlich eine Konstante, damit
#: ein zweiter Schreibversuch im Code **auffällt** statt still zu entstehen.
WRITABLE_SETTING: Final[str] = "oww_threshold"
#: Feldname im Request-Body (`{"threshold": 0.75}`) – bewusst **kürzer** als
#: der Settings-Name, damit der Client nicht `oww_threshold` im Doppelhumpen
#: tippen muss; **beide** Namen stehen hier nebeneinander, damit die
#: Übersetzung an genau **einer** Stelle passiert.
THRESHOLD_FIELD: Final[str] = "threshold"
#: Settings-Feld, das den Schreibmodus schaltet (Default **aus**).
WRITE_ENABLED_FIELD: Final[str] = "dashboard_write_enabled"
#: Erlaubter Wertebereich der Wake-Schwelle.  `0.0` ist **nicht** erlaubt
#: (dann wäre jeder Rausch-Chunk ein Wake) – dasselbe `gt=0.0`, das
#: `app/config.py` für das Feld vorschreibt.
THRESHOLD_MIN: Final[float] = 0.0
THRESHOLD_MAX: Final[float] = 1.0
#: HTTP-Status, wenn der Schreibmodus **aus** ist.  Bewusst **nicht** 2xx und
#: auch nicht 404: die Route existiert, der Betrieb verweigert sie (403) – ein
#: Client erkennt „aus" daran eindeutig.
WRITE_DISABLED_STATUS: Final[int] = 403
#: Quelle der Änderung im Audit-Log („ui" = über das Dashboard geschrieben).
WRITE_SOURCE_UI: Final[str] = "ui"
#: **E110 (P11.T1)** – Settings-Feld der optionalen Auth-Schicht.  Default
#: **leer = offen**; gesetzt ⇒ `POST /api/config/*` verlangt `Bearer <token>`.
API_TOKEN_FIELD: Final[str] = "dashboard_api_token"
#: HTTP-Status, wenn der API-Token fehlt/falsch ist.  Liegt **vor**
#: `WRITE_DISABLED_STATUS` (401 vor 403): wer sich nicht authenifiziert,
#: erfährt nichts über den Schreibmodus.
WRITE_UNAUTHORIZED_STATUS: Final[int] = 401
#: Präfix des `Authorization`-Headers, das die Auth-Schicht akzeptiert.
BEARER_PREFIX: Final[str] = "Bearer "

#: **E111 (P11.T2)** – die schreibbaren Multi-Field-Settings für
#: `PUT /api/config`.  Jede Zeile: `(feld, env-key, live-ohne-neustart)`.
#: Der Env-Key ist das SNAKE_CASE-Feld großgeschrieben (Pydantic-
#: `case_sensitive=False`-Mapping in `app/config.py`).  `live=True` ⇒ der
#: laufende Prozess liest den Wert bei Aufruf aus `app.config.settings`
#: (dem Modul-Singleton) ⇒ wirkt **ohne** Neustart; `live=False` ⇒ der Wert
#: wird erst beim nächsten Start gelesen ⇒ landet in `restarted_required`.
CONFIG_WRITE_FIELDS: Final[tuple[tuple[str, str, bool], ...]] = (
    ("oww_threshold", "OWW_THRESHOLD", True),
    ("log_level", "LOG_LEVEL", True),
    ("audio_dump_enabled", "AUDIO_DUMP_ENABLED", True),
    ("whisper_language", "WHISPER_LANGUAGE", True),
    ("oww_barge_in_threshold", "OWW_BARGE_IN_THRESHOLD", False),
    ("oww_cooldown_ms", "OWW_COOLDOWN_MS", False),
    ("wake_attempt_keep_floor", "WAKE_ATTEMPT_KEEP_FLOOR", False),
    ("router_confidence_gate", "ROUTER_CONFIDENCE_GATE", False),
    ("router_needs_param_threshold", "ROUTER_NEEDS_PARAM_THRESHOLD", False),
    ("jev_mode", "JEV_MODE", False),
    ("ha_entity_domains", "HA_ENTITY_DOMAINS", False),
)
#: Die erlaubten Body-Feldnamen (Allowlist) – alles andere ⇒ **400**.
CONFIG_WRITE_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    name for name, _key, _live in CONFIG_WRITE_FIELDS
)
#: `feld → env-key` (die eine Stelle der Übersetzung).
CONFIG_WRITE_ENV_KEYS: Final[dict[str, str]] = {
    name: key for name, key, _live in CONFIG_WRITE_FIELDS
}
#: `feld → wirkt ohne Neustart` (statisches Mapping, **kein** Read-Back).
CONFIG_WRITE_LIVE_MAP: Final[dict[str, bool]] = {
    name: live for name, _key, live in CONFIG_WRITE_FIELDS
}
#: E111 – Zahlen-Grenzen pro Feld, gespiegelt aus `app/config.py`:
#: `feld → (min, min_inklusive, max, max_inklusive)`.  `bool` ist bewusst
#: **kein** Number (wie bei E96): `true` wäre ein stiller Sonderwert.
_CONFIG_FLOAT_BOUNDS: Final[dict[str, tuple[float, bool, float, bool]]] = {
    "oww_threshold": (0.0, False, 1.0, True),
    "oww_barge_in_threshold": (0.0, False, 1.0, True),
    "wake_attempt_keep_floor": (0.0, True, 1.0, False),
    "router_confidence_gate": (0.0, True, 1.0, True),
    "router_needs_param_threshold": (0.0, True, 1.0, True),
}
#: E111 – ganzzahlige Felder: `feld → (min, min_inkl, max, max_inkl)`.
_CONFIG_INT_BOUNDS: Final[dict[str, tuple[int, bool, Optional[int], bool]]] = {
    "oww_cooldown_ms": (0, True, None, True),
}
#: E111 – Aufzählungs-Felder: `feld → erlaubte Werte` (aus `app/config.py`).
_CONFIG_ENUMS: Final[dict[str, tuple[str, ...]]] = {
    "log_level": LOG_LEVELS,
    "jev_mode": JEV_MODES,
}
#: E111 – erlaubtes HA-Entity-Domain (kleine Buchstaben, Ziffern, `_`).
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+$")

#: Platzhalter für maskierte Geheimnisse (nie der Originalwert).
REDACTED: Final[str] = "***REDACTED***"

#: Env-Variable, mit der das Log-Verzeichnis überschrieben werden kann
#: (`app.config` darf in P8.D1 nicht geändert werden).
LOG_DIR_ENV: Final[str] = "EVA_LOG_DIR"
#: Fallback-Kandidaten, **geprüft** (kein hartkodierter Blindpfad). Im
#: wyoming-manager existiert aktuell keiner davon ⇒ `source: "none"`.
LOG_DIR_CANDIDATES: Final[tuple[Path, ...]] = (
    Path("/app/logs"),
    PROJECT_ROOT / "logs",
    Path("/var/log/eva"),
)

#: Die Pipeline führt **keine** Übergangs-Historie ⇒ Runtime-`previous_state`/
#: `since` bleiben `null`, und das wird im Antwortfeld benannt (ehrlich statt
#: geraten). Die *statische* Graphdarstellung stammt aus `PLAN.md` §5.
TRANSITIONS_TRACKED: Final[bool] = False
#: Herkunft des Zustandsgraphen (für das Frontend nachvollziehbar).
GRAPH_SOURCE: Final[str] = "PLAN.md §5"
#: Warum `/api/history` leer bleibt, wenn das gelesene Objekt keinen Puffer hat.
#: Seit **P8.D3** führt `app.pipeline.Pipeline` den Ringpuffer
#: `Pipeline.turn_history`; dieser Text greift also nur noch, wenn **kein**
#: echter Pipeline-Kontext gelesen wurde (Attrappe/Stub im Test, entferntes
#: Attribut). Er bleibt unverändert stehen, damit „nichts gelesen" ehrlich
#: benannt und **nichts erfunden** wird.
HISTORY_REASON: Final[str] = (
    "Am gelesenen Pipeline-Objekt hängt keine Turn-Historie (keine "
    "Turn-Historie am Objekt, also keine 'turn_history'-Sequenz). Seit P8.D3 "
    "führt der echte app.pipeline.Pipeline einen begrenzten In-Memory-"
    "Ringpuffer; fehlt das Attribut, ist das gelesene Objekt kein Pipeline-"
    "Kontext. Es wird hier nichts erfunden und nichts gespeichert."
)

# ── Herkunft des Settings-**Objekts** (P8.D3, Teil 2) ───────────────────
#: `app.state.settings` wurde gefunden ⇒ die Werte sind die des laufenden
#: Prozesses (kein Fallback, kein `degraded`).
SETTINGS_SOURCE_APP_STATE: Final[str] = "app.state"
#: `app.state.settings` fehlt ⇒ `app.config.settings` (Modul-Singleton) wird
#: **als Fallback** benutzt. Das ist ein echter Mangel ⇒ `degraded: true` mit
#: Begründung, aber die **Werte** werden trotzdem geliefert (nie ein 500).
SETTINGS_SOURCE_MODULE_DEFAULT: Final[str] = "module-default"
#: Text des `degraded`-Hinweises, wenn `app.state.settings` wirklich fehlt.
SETTINGS_MISSING_ISSUE: Final[str] = (
    "app.state.settings fehlt ⇒ Modul-Default aus app.config als Fallback "
    "verwendet (Werte nicht gegen die laufende App verifiziert)."
)

#: Substring-Treffer ⇒ Feld gilt als Secret und wird **nie** ausgegeben.
SECRET_FIELD_HINTS: Final[tuple[str, ...]] = (
    "key",
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
)

#: Keys, deren **Wert** in Log-Zeilen maskiert wird (`redact()`).
_SECRET_KEY_NAMES: Final[str] = (
    r"api[_-]?key|apikey|access[_-]?key|secret[_-]?key|auth[_-]?key"
    r"|ha[_-]?token|llm[_-]?api[_-]?key|llm[_-]?key|opencode[_-]?api[_-]?key"
    r"|access[_-]?token|refresh[_-]?token|id[_-]?token|bearer[_-]?token"
    r"|token|password|passwd|passphrase|secret|credential"
    r"|wifi[_-]?(?:pass|password|key|psk)|psk|cookie|session"
)
_URL_USERINFO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^\s:/@]+):(?P<pw>[^\s/@]+)@"
)
_AUTHORIZATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<key>\bauthorization\b\s*[:=]\s*)(?P<value>[^\r\n]*)", re.IGNORECASE
)
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"\bbearer\s+[^\s]+", re.IGNORECASE)
_SECRET_KV_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<key>[\"']?(?:" + _SECRET_KEY_NAMES + r")[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)",
    re.IGNORECASE,
)
#: `%(asctime)s %(levelname)s %(name)s: %(message)s` (siehe `app.logger`).
_LOG_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Za-z]+) (?P<logger>[^:]+): (?P<message>.*)$"
)


# ── Zustandskatalog (`PLAN.md` §5) ───────────────────────────────────────
@dataclass(frozen=True)
class StateSpec:
    """Beschreibung **eines** kanonischen Zustands (statisch, aus `PLAN.md` §5).

    `next_states` ist der Graph aus §5 – er wird **nicht** aus dem laufenden
    Code abgeleitet (der kennt keine Transition-Tabelle), sondern ist die
    dokumentierte Zustandsmaschine. `terminal` ist überall ``False``: §5 kennt
    keinen Endzustand, jeder Pfad führt nach ``SPEAKING`` zurück zu ``IDLE``.
    """

    label: str
    next_states: tuple[str, ...]
    terminal: bool = False


#: §5-Graph: Connect/Stream → IDLE → LISTENING → TRANSCRIBING → ROUTING →
#: {EXECUTING | ANSWERING | SPEAKING} → SPEAKING → IDLE, Barge-in → LISTENING.
STATE_SPECS: Final[Mapping[str, StateSpec]] = {
    "idle": StateSpec(
        label="Bereit (Stream läuft, Wake-Erkennung aktiv)",
        next_states=("listening",),
    ),
    "listening": StateSpec(
        label="Hört zu (Turn-Puffer, Preroll verworfen)",
        # 0x04 VAD_END → TRANSCRIBING; 0x05 no-speech / Hard-Cap / No-Speech → IDLE
        next_states=("transcribing", "idle"),
    ),
    "transcribing": StateSpec(
        label="Transkribiert (Whisper)",
        # §5 zeigt nur ROUTING; der Code kehrt bei leerem Transkript zusätzlich
        # mit outcome="silence" nach IDLE zurück – beides ist aufgeführt.
        next_states=("routing", "idle"),
    ),
    "routing": StateSpec(
        label="Routing (Jev systemone → DeepSeek)",
        next_states=("executing", "answering", "speaking"),
    ),
    "executing": StateSpec(
        label="Führt HA-Befehl aus (/api/services)",
        next_states=("speaking",),
    ),
    "answering": StateSpec(
        label="Formuliert Antwort (DeepSeek)",
        next_states=("speaking",),
    ),
    "speaking": StateSpec(
        label="Spricht (Piper → SoXR → 0x02/0x03)",
        # Barge-in: score ≥ OWW_BARGE_IN_THRESHOLD → speaker_flush → neuer Turn
        next_states=("idle", "listening"),
    ),
}


# ── Response-Modelle (Pydantic, benannte Felder) ───────────────────────
class HealthInfo(BaseModel):
    """Eigener, billiger API-Zustand – **kein** HTTP-Aufruf auf sich selbst."""

    router_ok: bool
    exception: Optional[str] = None


class DeviceStatus(BaseModel):
    """Betriebsdaten **eines** Geräts (keine Inhalte, keine Geheimnisse).

    `connected_seconds` ist das Alter der `DeviceSession` (aus
    `connected_at`, ein monotoner Zeitpunkt). `last_seen` bleibt `null`: der
    WS-Server führt **keinen** Zeitstempel des letzten Pongs/Frames – ehrlich
    leer statt geschätzt.
    """

    device_id: str
    state: Optional[str] = None
    connected: Optional[bool] = None
    connected_seconds: Optional[float] = None
    last_seen: Optional[str] = None


class StatusResponse(BaseModel):
    """Antwort von `GET /api/status`."""

    service: str
    version: str
    uptime_seconds: float
    mdns_running: Optional[bool] = None
    device_count: int
    devices: list[DeviceStatus]
    health: HealthInfo
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class PairingDevice(BaseModel):
    """Geräte-Zeile in `GET /api/pairing` (P10.T3).

    `last_seen` bleibt bewusst **immer** `null`: der Keepalive führt keinen
    Zeitstempel (nur ein `asyncio.Event`), und ein geschätzter Wert wäre eine
    Erfindung – der Repo-Stil ist „leer statt geraten".  Der Verbindungszeit-
    punkt steckt stattdessen in `connected_seconds` (genau wie `/api/status`).
    """

    device_id: str
    connected: Optional[bool] = None
    state: Optional[str] = None
    last_seen: Optional[str] = None
    connected_seconds: Optional[float] = None
    mic_synced: Optional[bool] = None


class PairingResponse(BaseModel):
    """Antwort von `GET /api/pairing` (P10.T3, additiv – keine neuen Schreibrouten).

    `status` ist eine kleine, klare Ampel für den Onboarding-Wizard:

    * `"paired"`  – mindestens ein Gerät verbunden (mic_sync je Gerät separat),
    * `"waiting"` – mDNS aktiv, aber **kein** Dot verbunden („warte auf deinen Dot…"),
    * `"mdns_off"` – mDNS bewusst deaktiviert (`MANAGER_MDNS_ENABLED=false`) –
      der Dot kann den Manager dann **nicht** finden,
    * `"unknown"` – Zustand nicht ermittelbar (`degraded: true`, `issues` lesen).
    """

    service: str
    version: str
    mdns_enabled: Optional[bool] = None
    mdns_running: Optional[bool] = None
    #: Der **tatsächlich** registrierte mDNS-Instance-Name (z. B. `echomuse`).
    mdns_announced_as: Optional[str] = None
    #: True, wenn Zeroconf den Namen ändern musste (`echomuse-2`) – ein zweiter
    #: Announcer hält `echomuse` schon; der Dot filtert auf den Instanznamen.
    mdns_name_conflict: bool = False
    device_count: int
    devices: list[PairingDevice] = Field(default_factory=list)
    status: str
    hint: Optional[str] = None
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class StateInfo(BaseModel):
    """Ein kanonischer Zustand inkl. §5-Graph (damit das Frontend zeichnen kann)."""

    key: str
    label: str
    terminal: bool
    next: list[str]


class DeviceWorkflow(BaseModel):
    """Zustand **eines** Geräts; `previous_state`/`since` bleiben bewusst `null`."""

    device_id: str
    current_state: Optional[str] = None
    previous_state: Optional[str] = None
    since: Optional[str] = None


class StateResponse(BaseModel):
    """Antwort von `GET /api/state`."""

    states: list[StateInfo]
    devices: list[DeviceWorkflow]
    transitions_tracked: bool
    graph_source: str
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class LogEntry(BaseModel):
    """Eine Log-Zeile – `message` ist **immer** redigiert."""

    ts: Optional[str] = None
    level: str
    logger: str
    message: str


class LogsResponse(BaseModel):
    """Antwort von `GET /api/logs` (`order` sagt die Sortrichtung eindeutig)."""

    entries: list[LogEntry]
    source: str
    redacted: bool
    order: str = "ascending"
    count: int
    log_dir: Optional[str] = None
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    """Ein abgeschlossener Turn – **Allowlist**, kein Freitext.

    P8.D3 hat die Allowlist erweitert (siehe :data:`HISTORY_FIELDS`): neu sind
    `transcript` (**rotiert + gekürzt**), `target_entity_id`, `needs_param`,
    `extracted_param`, `error`, `error_code` und `barge_in`. Der P8.D1-Auftrag
    nennt drei Felder mit anderen Namen; sie sind hier bewusst unter den
    **Leser-Namen** realisiert, weil `app/static/dashboard/app.js` (P8.D2,
    unveränderlich) genau `duration_seconds`/`confidence`/`score` liest:

    ==================================  ==================================
    P8.D3-Auftrag                        Feld hier
    ==================================  ==================================
    ``duration_ms``                      ``duration_seconds`` (float, Sek.)
    ``target_confidence``                ``confidence``
    ``wake_score``                       ``score``
    ==================================  ==================================

    **Nie** enthalten: Audio/Chunks, Rohframes, Secrets, rohe Jev-/DeepSeek-
    Antworten (`raw`, `response_text`), vollständige Systemprompts.

    **P9.T0** hängt neun Phasen-Dauern an (siehe unten).  Das ist ein reines
    Add-on: kein bestehendes Feld wurde umbenannt, entfernt oder in der
    Bedeutung verändert – `duration_seconds` bleibt die **Gesamt**dauer des
    Turns und wird **nicht** in die Summe der Phasen umgerechnet.
    """

    ts: Optional[str] = None
    device_id: Optional[str] = None
    state: Optional[str] = None
    intent: Optional[str] = None
    outcome: Optional[str] = None
    duration_seconds: Optional[float] = None
    score: Optional[float] = None
    confidence: Optional[float] = None
    service: Optional[str] = None
    domain: Optional[str] = None
    target_entity_id: Optional[str] = None
    needs_param: Optional[bool] = None
    extracted_param: Optional[str] = None
    transcript: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    barge_in: Optional[bool] = None
    # ── P9.T0: Phasen-Dauern in **Sekunden** (float, `None` = nicht gemessen).
    # `latency_after_speech_seconds` = HA-Call fertig − Sprechphase Ende (die
    # „Licht ist an"-Latenz), `latency_after_wake_seconds` = HA-Call fertig −
    # Wake-Wort.  `phase_tts_first_audio_seconds` misst bis zum ersten PCM-Chunk
    # **aus** `synthesize`, `phase_tts_first_frame_seconds` bis zum ersten
    # **gesendeten** Binärframe – der Unterschied ist der Puffer dazwischen.
    # Kein TTS/kein HA-Call/Button-Turn ⇒ die betroffenen Felder bleiben `None`
    # (nie `0`, nie geraten).
    latency_after_speech_seconds: Optional[float] = None
    latency_after_wake_seconds: Optional[float] = None
    phase_stt_seconds: Optional[float] = None
    phase_route_seconds: Optional[float] = None
    phase_tts_first_audio_seconds: Optional[float] = None
    phase_tts_total_seconds: Optional[float] = None
    phase_listen_seconds: Optional[float] = None
    phase_execute_seconds: Optional[float] = None
    phase_tts_first_frame_seconds: Optional[float] = None
    # ── P9.T5 (E100): Endpoint-Diagnostik (float/int, `None` = kein
    # manager-seitiges Endpointing, z. B. Button-Turn).  `endpoint_threshold`
    # ist die **wirksame** Schwelle `max(3·noise_floor, 0.004)` am Turn-Ende.
    endpoint_noise_floor: Optional[float] = None
    endpoint_threshold: Optional[float] = None
    endpoint_speech_frames: Optional[int] = None
    endpoint_silence_frames: Optional[int] = None
    endpoint_skip_frames: Optional[int] = None
    endpoint_speech_seconds: Optional[float] = None


class HistoryResponse(BaseModel):
    """Antwort von `GET /api/history`."""

    entries: list[HistoryEntry]
    available: bool
    reason: Optional[str] = None
    count: int
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class WakeAttempt(BaseModel):
    """**Ein** bewerteter Mic-Chunk mit seiner Wake-Entscheidung (P9.T0).

    Quelle ist ausschließlich der Ringpuffer
    `WakeWordDetector.wake_attempts` (begrenzt, nur RAM) – hier wird nichts
    gespeichert, nichts nachgeladen und nichts geschätzt.  **Freitext gibt es
    hier nicht**: nur Score, Grund und Zeitstempel, also **niemals** Audio,
    Rohframes oder ein Transkript.

    ``reason`` ist einer der vier echten Entscheidungszweige in
    `WakeWordDetector.process`: ``accepted`` / ``warmup_gate`` / ``cooldown`` /
    ``below_threshold``.  ``score``/``threshold`` sind `None`, falls der
    Aufrufer sie nicht liefert – der Ring **kann** auch Einträge ohne Score
    enthalten (z. B. ein Stub), und das wird nicht erfunden.
    """

    ts: Optional[str] = None
    device_id: Optional[str] = None
    score: Optional[float] = None
    accepted: Optional[bool] = None
    reason: Optional[str] = None
    threshold: Optional[float] = None
    chunk_index: Optional[int] = None


class WakeStats(BaseModel):
    """Kennzahlen **über genau diese** gelieferten Versuche (P9.T0).

    Alle Zahlen beziehen sich auf das Antwortfenster, **nicht** auf die
    Lebenszeit des Detektors.  `min`/`median`/`max` sind ausschließlich über
    Einträge **mit** Score gebildet; ohne einen solchen Eintrag bleiben sie
    `None` (lieber leer als ein erfundener Wert aus einer leeren Menge).
    `accepted + rejected == count` ist die Zusicherung, die ein Tacho
    braucht.
    """

    count: int
    accepted: int
    rejected: int
    score_min: Optional[float] = None
    score_median: Optional[float] = None
    score_max: Optional[float] = None
    best_rejected_score: Optional[float] = None
    reasons: dict[str, int] = Field(default_factory=dict)


class WakeDevice(BaseModel):
    """Wake-Zustand **eines** Geräts – inkl. der dort geltenden Schwelle."""

    device_id: str
    threshold: Optional[float] = None
    barge_in_threshold: Optional[float] = None
    count: int = 0
    accepted: int = 0
    rejected: int = 0


class WakeResponse(BaseModel):
    """Antwort von `GET /api/wake` (P9.T0/E96).

    ``threshold`` ist die **wirksame** Schwelle aus dem laufenden Detektor
    (`Pipeline.wake_threshold()`), **nicht** blind die `settings`; woher sie
    stammt, steht in ``threshold_source``.  Fällt sie auf die Settings zurück
    (kein Gerät/Detektor), wird das offen benannt statt getarnt.

    **E96:** ``write_enabled`` sagt dem Frontend, ob die **eine** Schreibroute
    (`POST /api/config/oww-threshold`) überhaupt wirksam ist – die UI blendet
    sich ohne diesen Wert aus.  ``keep_floor``/``default_threshold`` machen die
    beiden Filter des Messrings bzw. den Reset-Zielwert sichtbar.
    """

    attempts: list[WakeAttempt]
    count: int
    stats: WakeStats
    devices: list[WakeDevice]
    threshold: Optional[float] = None
    threshold_source: str
    barge_in_threshold: Optional[float] = None
    window: Optional[int] = None
    write_enabled: bool = False
    writable_setting: str = WRITABLE_SETTING
    keep_floor: Optional[float] = None
    default_threshold: Optional[float] = None
    degraded: bool
    issues: list[str] = Field(default_factory=list)


class ThresholdWriteResponse(BaseModel):
    """Antwort von `POST /api/config/oww-threshold` (E96).

    `previous`/`threshold` sind die **beiden** Zahlen des Schritts, `source`
    nennt die Herkunft der Änderung (``ui``) und `persisted_env`, ob der Wert
    zusätzlich in die `.env` geschrieben wurde – im Docker-Betrieb in die
    `.env` **im Container** (siehe die Grenze in
    :func:`write_oww_threshold`: übersteht `restart`, nicht `up --build`).
    `effective_without_restart`
    ist **kein** Werbeversprechen, sondern das Ergebnis des Rücklesens aus der
    Settings-Instanz, die `app/wake_word` beim Vergleich benutzt – ein
    geschriebener Wert, den der laufende Prozess nicht sieht, wäre sonst
    still wirkungslos.  **Kein** Feld enthält ein Secret.
    """

    applied: bool
    setting: str = WRITABLE_SETTING
    previous: Optional[float] = None
    threshold: float
    source: str = WRITE_SOURCE_UI
    persisted_env: bool = False
    env_file: Optional[str] = None
    settings_applied: bool = False
    effective_without_restart: bool = False
    applied_at: str


class ConfigWriteResponse(BaseModel):
    """Antwort von `PUT /api/config` (E111) – der Multi-Field-Write.

    `written` sind die geschriebenen Feldnamen (sortiert),
    `effective_without_restart` sagt **pro Feld**, ob es ohne Neustart wirkt
    (statisches Mapping aus :data:`CONFIG_WRITE_LIVE_MAP`, **kein**
    Read-Back), `restarted_required` sind die geschriebenen Felder, die einen
    Neustart brauchen.  **Kein** Feld enthält ein Secret.
    """

    written: list[str]
    effective_without_restart: dict[str, bool]
    restarted_required: list[str]


class ConfigValue(BaseModel):
    """Ein projizierter Settings-Wert **mit** Herkunft.

    `source` ist die Herkunft **des Wertes** (siehe :func:`_config_source`):
    ``"env"`` = explizit gesetzt, ``"default"`` = Feld-Default aus
    `app.config`, ``"module"`` = Modulkonstante außerhalb der Settings
    (``jev_max_choices``), ``"unknown"`` = nicht ermittelbar (Objekt ohne
    `model_fields_set`). Die Herkunft des **Objekts** steht separat in
    :class:`ConfigResponse.settings_source`.
    """

    name: str
    group: str
    value: bool | int | float | str | list[str] | None
    source: str


class ConfigResponse(BaseModel):
    """Antwort von `GET /api/config` – Lese-Export **ohne** Geheimnisse.

    `settings_source` sagt, **woher das Settings-Objekt** kam
    (:data:`SETTINGS_SOURCE_APP_STATE` = `app.state.settings` vorhanden,
    :data:`SETTINGS_SOURCE_MODULE_DEFAULT` = Modul-Singleton als Fallback ⇒
    `degraded: true`). P8.D3: das wird **über die Verfügbarkeit in
    `app.state`** bestimmt, nicht über einen Objektidentitätsvergleich –
    `app/main.py` vergibt genau das Modul-Singleton an `app.state.settings`,
    ein `is`-Vergleich war deshalb immer wahr und meldete fälschlich
    „settings fehlt".
    """

    service: str
    version: str
    values: list[ConfigValue]
    settings_source: str
    degraded: bool
    issues: list[str] = Field(default_factory=list)
    #: **E110 (P11.T3)** – `True`, wenn `dashboard_api_token` gesetzt ist:
    #: die Schreib-Endpoints verlangen dann `Authorization: Bearer <token>`.
    #: Der Tokenwert selbst bleibt **nie** in der Antwort.
    write_token_required: bool = False
    #: **E111 (P11.T3)** – pro E111-Feld: wirkt ohne Neustart? Statisches
    #: Mapping aus :data:`CONFIG_WRITE_LIVE_MAP` (gleich mit der
    #: `PUT /api/config`-Antwort) – das Dashboard zeigt daraus je Feld ein
    #: „live"/„Neustart"-Badge, **ohne** selbst rechnen zu müssen.
    effective_without_restart: dict[str, bool] = Field(default_factory=dict)


class DependencyProbe(BaseModel):
    """Ergebnis **eines** Health-Pings (`ok: null` = nicht ermittelbar)."""

    name: str
    ok: Optional[bool] = None
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    target: Optional[str] = None
    detail: Optional[str] = None
    reason: Optional[str] = None


class DependenciesResponse(BaseModel):
    """Antwort von `GET /api/dependencies` (HA wird gepingt, Whisper/Piper nicht)."""

    timeout_seconds: float
    dependencies: list[DependencyProbe]
    degraded: bool
    issues: list[str] = Field(default_factory=list)


# ── Redaction (P8.D1: Secrets niemals ausgeben) ────────────────────────
def redact(text: object) -> str:
    """Maskiert Geheimnisse in einem Text (Log-Zeile, Exception-Text, …).

    Maskiert werden (Reihenfolge ist relevant):

    1. **Userinfo in URLs** – ``https://user:pw@host`` ⇒ ``pw`` weg.
    2. **``Authorization:``/``Authorization=``** – der Rest der Zeile.
    3. **``Bearer <token>``** – der Tokenwert.
    4. **``key=value``/``key: value``** für Schlüsselnamen aus
       :data:`SECRET_FIELD_HINTS` plus API-Key-/Token-Varianten (u. a.
       ``api_key``, ``ha_token``, ``password``, ``secret``) – inkl. JSON-Stil
       ``"api_key": "…"``. Der **Schlüssel bleibt sichtbar**, nur der Wert
       wird ersetzt, damit die Log-Zeile diagnostisch bleibt.

    Bewusst *nicht* gemacht: eine generische Mustererkennung auf
    token-ähnliche Strings – das würde normale Log-Texte zerhacken und wäre
    nicht testbar. Grenze: ein Secret, das **ohne** erkennbaren Schlüsselnamen
    und ohne Bearer/Authorization im Log steht, wird nicht erkannt.
    """
    rendered = text if isinstance(text, str) else str(text)
    masked = _URL_USERINFO_RE.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", rendered
    )
    masked = _AUTHORIZATION_RE.sub(lambda m: f"{m.group('key')}{REDACTED}", masked)
    masked = _BEARER_RE.sub(lambda m: f"{m.group(0).split()[0]} {REDACTED}", masked)
    masked = _SECRET_KV_RE.sub(lambda m: f"{m.group('key')}{REDACTED}", masked)
    return masked


def is_secret_field(name: str) -> bool:
    """True, wenn ein Settings-Feldname Secret-Charakter hat (⇒ nie exportieren)."""
    lowered = name.lower()
    return any(hint in lowered for hint in SECRET_FIELD_HINTS)


# ── Filter-/Format-Helfer (rein, ohne HTTP) ─────────────────────────────
def clamp_limit(value: int, *, maximum: int, minimum: int = 1) -> int:
    """Begrenzt ein Query-`limit` auf `[minimum, maximum]`.

    Ein zu großes `limit` wird **geclampt** (kein 4xx ⇒ das Dashboard pollt
    robust weiter), ein `limit <= 0` auf `minimum` gehoben.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def parse_levels(raw: Optional[str]) -> Optional[frozenset[str]]:
    """Level-Filter `?level=WARNING,error` ⇒ `frozenset({"WARNING","ERROR"})`.

    * `None`/leer ⇒ ``None`` = **kein** Level-Filter (alle Level).
    * Unbekannte Levelnamen werden verworfen; sind **alle** unbekannt ⇒
      `frozenset()`, das **nichts** matcht ⇒ leere Liste statt HTTP 500.
    """
    if raw is None:
        return None
    parts = [part.strip().upper() for part in raw.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return frozenset(part for part in parts if part in LOG_LEVEL_NAMES)


def matches_filters(
    entry: LogEntry,
    *,
    levels: Optional[frozenset[str]] = None,
    logger_filter: Optional[str] = None,
    module_filter: Optional[str] = None,
) -> bool:
    """Level-/Logger-Filter auf **einen** Eintrag (case-insensitiver Teilstring)."""
    if levels is not None and entry.level.upper() not in levels:
        return False
    name = entry.logger.lower()
    if logger_filter and logger_filter.lower() not in name:
        return False
    if module_filter and module_filter.lower() not in name:
        return False
    return True


def parse_log_line(line: str) -> LogEntry:
    """Zerlegt eine `LOG_FORMAT`-Zeile; unpassende Zeilen werden nicht verworfen.

    Eine Zeile, die nicht dem Format entspricht, kommt als `level="UNKNOWN"`,
    `ts=None` zurück (der Text ist trotzdem redigiert) – Log-Verlust ist im
    Dashboard schlechter als ein ehrlich als `UNKNOWN` markierter Eintrag.
    """
    match = _LOG_LINE_RE.match(line.strip())
    if match is None:
        return LogEntry(ts=None, level="UNKNOWN", logger="", message=redact(line))
    return LogEntry(
        ts=match.group("ts"),
        level=match.group("level").upper(),
        logger=match.group("logger").strip(),
        message=redact(match.group("message")),
    )


def parse_log_ts(ts: Optional[str]) -> Optional[datetime]:
    """`2026-09-27 12:34:56,789` → `datetime` (naiv, Logger-lokale Zeit)."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def build_state_catalog() -> list[StateInfo]:
    """Kanonische Zustände als **geordnete** Liste (§5-Reihenfolge).

    Die Reihenfolge kommt dynamisch aus `PipelineState` (die Enum-Reihenfolge
    *ist* die §5-Reihenfolge); Label/`next` kommen aus :data:`STATE_SPECS`,
    ein unbekannter Wert bekäme einen generischen Label statt zu fehlen.
    """
    catalog: list[StateInfo] = []
    for member in PipelineState:
        key = member.value
        spec = STATE_SPECS.get(key)
        catalog.append(
            StateInfo(
                key=key,
                label=spec.label if spec else key,
                terminal=spec.terminal if spec else False,
                next=list(spec.next_states) if spec else [],
            )
        )
    return catalog


# ── Settings-Projektion (Allowlist, keine Secrets) ──────────────────────
#: Explizite Allowlist: `(Feldname, Gruppe)`. Bewusst **kein** Reflection-Dump –
#: ein neues Secret-Feld in `app.config` kann so nicht versehentlich auslaufen.
CONFIG_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("manager_host", "server"),
    ("manager_port", "server"),
    ("manager_mdns_enabled", "server"),
    ("oww_enabled", "wake"),
    ("oww_model", "wake"),
    ("oww_threshold", "wake"),
    ("oww_barge_in_enabled", "wake"),
    ("oww_barge_in_threshold", "wake"),
    ("oww_cooldown_ms", "wake"),
    ("oww_preroll_discard_chunks", "wake"),
    ("wake_attempt_keep_floor", "wake"),
    ("dashboard_write_enabled", "dashboard"),
    ("turn_hard_cap_seconds", "turn"),
    ("turn_no_speech_seconds", "turn"),
    ("vad_device_sided", "turn"),
    ("whisper_model", "stt"),
    ("whisper_language", "stt"),
    ("piper_voice", "tts"),
    ("ha_entity_domains", "ha"),
    ("ha_allowed_entities", "ha"),
    ("ha_cache_interval_seconds", "ha"),
    ("ha_request_timeout", "ha"),
    ("jev_mode", "router"),
    ("jev_model", "router"),
    ("jev_timeout", "router"),
    ("router_variant", "router"),
    ("router_confidence_gate", "router"),
    ("router_needs_param_threshold", "router"),
    ("deepseek_model", "router"),
    ("deepseek_timeout", "router"),
    ("log_level", "logging"),
    ("enable_test_hooks", "test"),
    ("run_live", "test"),
)
#: Sonderfälle der Projektion: `list` → CSV als Liste, `count` → nur Anzahl.
_CONFIG_LIST_FIELDS: Final[frozenset[str]] = frozenset({"ha_entity_domains"})
_CONFIG_COUNT_FIELDS: Final[frozenset[str]] = frozenset({"ha_allowed_entities"})


def _csv_count(raw: object) -> int:
    """Anzahl nicht-leerer CSV-Einträge (ohne den Wert selbst preiszugeben)."""
    if isinstance(raw, (list, tuple)):
        return sum(1 for part in raw if str(part).strip())
    return sum(1 for part in str(raw or "").split(",") if part.strip())


def _config_source(settings_obj: Any, name: str) -> str:
    """Herkunft **eines Wertes** – P8.D3, ehrliche Semantik.

    * ``"env"``     – der Wert wurde **explizit gesetzt**, ist also *nicht* der
      Feld-Default aus `app/config.py`.
    * ``"default"`` – der Wert **ist** der Feld-Default aus `app/config.py`.
    * ``"unknown"`` – am Objekt gibt es kein `model_fields_set` (kein
      pydantic-Settings-Objekt) ⇒ die Herkunft ist **nicht** beweisbar und
      wird nicht geraten.
    * ``"module"``  – nur für Werte, die gar nicht aus den Settings kommen
      (``jev_max_choices`` aus `app.llm_client`); wird von :func:`project_config`
      gesetzt, nicht von hier.

    **Warum `model_fields_set` und nicht ein Vergleich?** (die „`Settings()`-
    Falle", ausdrücklich geprüft) `app.config.Settings` ist ein
    `pydantic_settings.BaseSettings` mit `env_file` – **`Settings()` liest
    die Umgebung.** Ein zweites `Settings()` zum Vergleich hätte deshalb
    denselben Env-Stand und könnte „Default" von „env" **nicht** unterscheiden.
    `model_fields_set` ist dagegen exakt die pydantic-Menge der Felder, die ein
    Init-Quell-Driver **gesetzt** hat; sie braucht keinen zweiten Vergleich und
    fasst Prozess-Env **und** `.env`-Datei als „explizit gesetzt" zusammen. Der
    **Wert** selbst wird dafür nie angefasst, und die Zuordnung der Feldnamen
    auf ENV-Keys bleibt Sache von `app.config` (hier wird nur gemeldet, was
    pydantic als gesetzt markiert).

    Konsequenz für das Dashboard: `env` heißt **„explizit gesetzt"**, nicht
    „aus `os.environ`" – ein Wert, der nur in der `.env`-Datei steht, ist
    ebenfalls `env`. Live belegt (P8.D3, `.123`): `router_variant=entity` ist
    `env`, `router_needs_param_threshold=0.5` ist zu Recht `default` (steht in
    keiner Env-Quelle), `ha_allowed_entities=0` ebenso.
    """
    fields_set = getattr(settings_obj, "model_fields_set", None)
    if fields_set is None:
        return "unknown"
    return "env" if name in fields_set else "default"


def project_config(settings_obj: Any) -> tuple[list[ConfigValue], list[str]]:
    """Projiziert die nicht-geheimen Settings als benannte `ConfigValue`-Liste.

    * Nur die Allowlist :data:`CONFIG_FIELDS` – kein Feld mit Secret-Charakter
      (`ha_token`, `llm_api_key`, …) wird betrachtet oder ausgegeben.
    * `ha_allowed_entities` wird **nur als Anzahl** ausgegeben (E17-Allowlist
      enthält volle Entity-IDs; die sind fürs Dashboard nicht nötig).
    * `jev_max_choices` kommt aus `app.llm_client` (lazy, damit dieses Modul
      den Router-Crawl nicht erzwingt).
    """
    issues: list[str] = []
    values: list[ConfigValue] = []
    for name, group in CONFIG_FIELDS:
        if is_secret_field(name):
            issues.append(f"Config-Feld {name} bewusst nicht ausgegeben (Secret)")
            continue
        raw = getattr(settings_obj, name, None)
        if raw is None and not hasattr(settings_obj, name):
            issues.append(f"Config-Feld {name} fehlt in den Settings")
            continue
        source = _config_source(settings_obj, name)
        if name in _CONFIG_LIST_FIELDS:
            values.append(
                ConfigValue(
                    name=name,
                    group=group,
                    value=[part.strip() for part in str(raw).split(",") if part.strip()],
                    source=source,
                )
            )
        elif name in _CONFIG_COUNT_FIELDS:
            values.append(
                ConfigValue(
                    name=name,
                    group=group,
                    value=_csv_count(raw),
                    source=source,
                )
            )
        else:
            values.append(ConfigValue(name=name, group=group, value=raw, source=source))

    max_choices = jev_max_choices()
    if max_choices is None:
        issues.append("JEV_MAX_CHOICES nicht ermittelbar (app.llm_client nicht ladbar)")
    else:
        values.append(
            ConfigValue(
                name="jev_max_choices",
                group="router",
                value=max_choices,
                source="module",
            )
        )
    return values, issues


def jev_max_choices() -> Optional[int]:
    """`JEV_MAX_CHOICES` aus `app.llm_client` (lazy) oder `None`."""
    try:
        from app.llm_client import JEV_MAX_CHOICES
    except Exception:  # pragma: no cover – defensiv, Import ist lokal harmlos
        return None
    try:
        return int(JEV_MAX_CHOICES)
    except (TypeError, ValueError):  # pragma: no cover – defensiv
        return None


# ── Log-Quellen: In-Memory (falls vorhanden) sonst Datei, sonst `none` ───
#: Modulweiter Override des Log-Verzeichnisses (Diagnose/Tests). `app.config`
#: darf in P8.D1 nicht geändert werden ⇒ Konfigurationshook hier.
_log_dir_override: Optional[Path] = None


class MemoryLogBuffer(logging.Handler):
    """In-Memory-Puffer für `/api/logs` (`source: "memory"`) – **begrenzt**.

    `app/logger.py` installiert **bewusst keinen** solchen Handler (keine
    Doppel-Zeilen im Container-Log). P8.D3 hängt jetzt **eine** Instanz im
    `lifespan` von `app/main.py` an (siehe :func:`attach_memory_log_buffer`) –
    der Manager-Logger heißt ``manager`` (``app.logger.LOGGER_NAMESPACE``) und
    hat `propagate = False`, deshalb sieht der Puffer jede Zeile des Managers
    **einmal** und der `stdout`-Handler druckt sie **einmal**.

    * **Begrenzt:** `deque(maxlen=LOG_BUFFER_MAXLEN)` ⇒ die ältesten Records
      werden verworfen, der Speicher wächst nie unbegrenzt.
    * **Redigiert beim Einfügen** (ein zweites Mal beim Auslesen) – ein Log darf
      also auch nach dem Auslesen kein Secret preisgeben. Tracebacks werden
      nicht mitgespeichert (sie enthalten Argumente/Umgebungen), nur der
      Exception-Typname wandert in die Nachricht.
    * **Rekursionsfrei:** der Handler loggt **nie** selbst (weder hier noch in
      :func:`redact`) – ein Log-Handler, der Records erzeugt, würde sich
      endlos selbst aufrufen. Auch der Fehlerpfad unten schreibt **nicht** über
      den `manager`-Logger, sondern nutzt `Handler.handleError` (stderr).
    * **Level `NOTSET`:** der Handler filtert nicht; der Level **des Loggers**
      (`settings.log_level`, Default `INFO`) entscheidet, was ankommt. Bewusst
      **keine** Absenkung auf DEBUG und **kein** Eingriff in `LOG_LEVEL` –
      bei `LOG_LEVEL=INFO` bleiben DEBUG-Zeilen unsichtbar, das ist korrekt.
    """

    def __init__(
        self, *, maxlen: int = LOG_BUFFER_MAXLEN, level: int = logging.NOTSET
    ) -> None:
        super().__init__(level=level)
        self.records: deque[logging.LogRecord] = deque(maxlen=int(maxlen))

    @property
    def maxlen(self) -> int:
        """Der harte Cap (für Tests/Diagnose lesbar)."""
        return int(self.records.maxlen or 0)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(record.getMessage())
            if record.exc_info and record.exc_info[0] is not None:
                message = f"{message} (Ausnahme: {record.exc_info[0].__name__})"
            safe = logging.LogRecord(
                name=record.name,
                level=record.levelno,
                pathname=record.pathname,
                lineno=record.lineno,
                msg=message,
                args=(),
                exc_info=None,
            )
            safe.created = record.created
            self.records.append(safe)
        except Exception:  # pragma: no cover – defensiv, siehe Docstring
            # Ein kaputter Dashboard-Puffer darf die Log-Ausgabe des Managers
            # **nie** mitreißen. `handleError` schreibt (nur wenn
            # `logging.raiseExceptions` steht) nach stderr – **nicht** über den
            # `manager`-Logger, also keine Rekursion.
            self.handleError(record)


def attach_memory_log_buffer(
    *, maxlen: int = LOG_BUFFER_MAXLEN
) -> MemoryLogBuffer:
    """Genau **einen** :class:`MemoryLogBuffer` an den `manager`-Logger hängen.

    Aufrufer ist der `lifespan` in `app/main.py` (Start). **Idempotent:**
    hängt bereits ein `MemoryLogBuffer` am ``manager``-Logger, wird genau
    dieser zurückgegeben und **kein** zweiter angehängt – doppeltes Anhängen
    (Lifespan doppelt, `create_app()` zweimal, Reload) ergäbe sonst jede Zeile
    doppelt im Puffer. Nur :class:`MemoryLogBuffer` wird erkannt, kein
    Duck-Typing – siehe :func:`find_memory_records`.
    """
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    for handler in list(manager_logger.handlers):
        if isinstance(handler, MemoryLogBuffer):
            return handler
    buffer = MemoryLogBuffer(maxlen=int(maxlen))
    buffer.set_name("manager-dashboard-buffer")
    manager_logger.addHandler(buffer)
    return buffer


def detach_memory_log_buffer(buffer: Optional[MemoryLogBuffer]) -> bool:
    """Den Puffer **wieder entfernen** (Aufrufer: `lifespan`/Shutdown).

    `removeHandler` **und** `close()` – beides ist Pflicht: ein nur
    entfernter Handler würde beim Reload/Neustart am Logger hängen bleiben
    (Handler-Leck, doppelte Zeilen), ein nicht geschlossener hält seine
    Ressourcen. Beide Aufrufe sind idempotent.

    Rückgabe: `True`, wenn tatsächlich entfernt wurde, `False` bei `None` oder
    einem schon abgehängten Puffer (damit der Shutdown einen **zweiten**
    Lifespan-Abbruch nicht als Fehler meldet).
    """
    if buffer is None:
        return False
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    was_attached = buffer in manager_logger.handlers
    manager_logger.removeHandler(buffer)
    buffer.close()
    return was_attached


def set_log_dir(path: Optional[Path | str]) -> None:
    """Setzt/löscht das Log-Verzeichnis der Datei-Quelle (`None` = zurück)."""
    global _log_dir_override
    _log_dir_override = Path(path) if path is not None else None


def resolve_log_dir() -> Optional[Path]:
    """Log-Verzeichnis: Override → `EVA_LOG_DIR` → existierende Kandidaten.

    `None`, wenn **kein** Verzeichnis existiert ⇒ `/api/logs` meldet ehrlich
    `source: "none"` (im wyoming-manager gibt es keine Logdateien, der
    Container sammelt `stdout`).
    """
    if _log_dir_override is not None:
        return _log_dir_override if _log_dir_override.is_dir() else None
    env_dir = os.environ.get(LOG_DIR_ENV, "").strip()
    if env_dir:
        candidate = Path(env_dir)
        return candidate if candidate.is_dir() else None
    for candidate in LOG_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


def find_memory_records() -> Optional[Sequence[Any]]:
    """In-Memory-Log-Records am `manager`-Logger, **falls** so ein Puffer hängt.

    Gesucht wird ausschließlich eine :class:`MemoryLogBuffer`-Instanz – **kein**
    Duck-Typing auf „irgendein Handler mit ``.records``". Grund: pytest hängt
    bei `propagate = False` eigene Aufzeichnungs-Handler an den `manager`-Logger;
    ein „nimm alles mit `records`"-Abgriff würde deren Records als
    Live-Historie ausgeben (fremde, testfremde Zeilen). Fehlt der Puffer, ist
    die Datei-Quelle (bzw. `source: "none"`) zuständig.
    """
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    for handler in list(manager_logger.handlers):
        if isinstance(handler, MemoryLogBuffer):
            return list(handler.records)
    return None


def record_to_entry(record: Any) -> LogEntry:
    """`LogRecord` (oder aufzeichnungsähnliches Objekt) → redigierter Eintrag."""
    created = getattr(record, "created", None)
    ts: Optional[str] = None
    if isinstance(created, (int, float)):
        ts = datetime.fromtimestamp(float(created)).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    level = getattr(record, "levelname", None) or getattr(record, "level", "UNKNOWN")
    name = getattr(record, "name", "") or ""
    get_message = getattr(record, "getMessage", None)
    if callable(get_message):
        message = str(get_message())
    else:
        message = str(getattr(record, "msg", record))
    return LogEntry(
        ts=ts,
        level=str(level).upper(),
        logger=str(name),
        message=redact(message),
    )


def _tail_lines(path: Path, *, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    """Die letzten `max_bytes` einer Datei als Zeilen (kein Volumen-Scan)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # halb abgeschnittene erste Zeile verwerfen
            data = handle.read()
    except OSError as exc:  # pragma: no cover – Dateisystem-Fehlfall
        _LOG.debug("Logdatei %s nicht lesbar: %r", path, exc)
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _log_files(log_dir: Path) -> list[Path]:
    """Logdateien im Verzeichnis, älteste zuerst (mtime) – nur direkte Ebene."""
    try:
        candidates = [
            entry
            for entry in log_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in LOG_FILE_SUFFIXES
        ]
    except OSError as exc:  # pragma: no cover – defensiv
        _LOG.debug("Logverzeichnis %s nicht lesbar: %r", log_dir, exc)
        return []
    return sorted(candidates, key=lambda path: path.stat().st_mtime)


def collect_log_entries(
    *,
    limit: int = DEFAULT_LOGS_LIMIT,
    levels: Optional[frozenset[str]] = None,
    logger_filter: Optional[str] = None,
    module_filter: Optional[str] = None,
    since_seconds: Optional[float] = None,
) -> tuple[list[LogEntry], str, Optional[str], list[str]]:
    """Liest die jüngsten Log-Einträge und filtert sie.

    Quellen-Priorität: In-Memory-Puffer (`"memory"`) → Logdateien (`"file"`) →
    nichts (`"none"`). Rückgabe: `(Einträge, source, log_dir, issues)`.
    """
    issues: list[str] = []
    entries: list[LogEntry] = []
    source = "none"
    log_dir: Optional[str] = None

    records = find_memory_records()
    if records is not None:
        source = "memory"
        for record in records:
            entries.append(record_to_entry(record))
    else:
        directory = resolve_log_dir()
        if directory is None:
            issues.append(
                "Keine In-Memory-Log-Liste am 'manager'-Logger und kein "
                f"existierendes Logverzeichnis (Override/{LOG_DIR_ENV}/"
                "Kandidaten) – es werden keine Container-Logs ausgegeben."
            )
        else:
            source = "file"
            log_dir = str(directory)
            files = _log_files(directory)
            if not files:
                issues.append(f"Logverzeichnis {directory} enthält keine Logdatei.")
            for path in files:
                for line in _tail_lines(path):
                    if line.strip():
                        entries.append(parse_log_line(line))

    if since_seconds is not None:
        cutoff = datetime.now() - timedelta(seconds=float(since_seconds))
        fresh: list[LogEntry] = []
        for entry in entries:
            parsed = parse_log_ts(entry.ts)
            # Ohne parsebaren Zeitstempel ist die Frische nicht beweisbar ⇒ raus.
            if parsed is not None and parsed >= cutoff:
                fresh.append(entry)
        entries = fresh

    entries = [
        entry
        for entry in entries
        if matches_filters(
            entry,
            levels=levels,
            logger_filter=logger_filter,
            module_filter=module_filter,
        )
    ]
    entries = entries[-limit:] if limit > 0 else []
    return entries, source, log_dir, issues


# ── Turn-Historie (Allowlist-Projektion auf den Pipeline-Puffer) ────────
#: Allowlist der History-Felder. **Kein** Eject: ein unbekannter Key im Puffer
#: wird ignoriert, ein bekannter nie durchgereicht, ohne hier zu stehen.
#:
#: P8.D1 hatte hier bewusst **kein** `transcript` („kein Textfeld verlässt dieses
#: Modul"). P8.D3 hat das **bewusst** geändert: `app/pipeline.py` legt in den
#: Ringpuffer nur **einen** redigierten und auf 500 Zeichen gekürzten
#: Transkript-Text, damit der Nutzer im Dashboard sieht, **was** er gesagt hat.
#: Weiterhin verboten bleiben: `response_text` (kompletter DeepSeek-Text),
#: `raw` (Jev-Rohantwort), `service_data` (Payload), Audio/Chunks/Rohframes,
#: Secrets, Systemprompts.
HISTORY_FIELDS: Final[tuple[str, ...]] = (
    "ts",
    "device_id",
    "state",
    "intent",
    "outcome",
    "duration_seconds",
    "score",
    "confidence",
    "service",
    "domain",
    "target_entity_id",
    "needs_param",
    "extracted_param",
    "transcript",
    "error_code",
    "error",
    "barge_in",
    # ── P9.T0: Phasen-Timing (reines Add-on, **kein** Feld darüber entfernt
    #    oder umbenannt).  Die Reihenfolge ist identisch zu
    #    `app.pipeline.TURN_LATENCY_FIELDS`, damit ein Vergleich der beiden
    #    Listen direkt aussagekräftig bleibt.  Nicht erreichte Phasen sind
    #    `None` – siehe `app/pipeline.py` (P9.T0/E95).
    "latency_after_speech_seconds",
    "latency_after_wake_seconds",
    "phase_stt_seconds",
    "phase_route_seconds",
    "phase_tts_first_audio_seconds",
    "phase_tts_total_seconds",
    "phase_listen_seconds",
    "phase_execute_seconds",
    "phase_tts_first_frame_seconds",
    # ── P9.T5 (E100): Endpoint-Diagnostik (reines Add-on).  `None` = kein
    #    manager-seitiges Endpointing (Button-Turn, K6).  `noise_floor`/Frames
    #    sind keine personenbezogenen Daten – keine Redaction nötig.
    "endpoint_noise_floor",
    "endpoint_threshold",
    "endpoint_speech_frames",
    "endpoint_silence_frames",
    "endpoint_skip_frames",
    "endpoint_speech_seconds",
)


def collect_history(limit: int, pipeline_obj: Any) -> tuple[list[HistoryEntry], bool, Optional[str]]:
    """Turn-Historie – **nur** aus einem vorhandenen Pipeline-Puffer.

    Quelle ist ausschließlich ein optionales, sequenzielles Attribut
    `turn_history` am Pipeline-Objekt. Seit **P8.D3** ist das der echte
    `collections.deque(maxlen=100)` aus `app/pipeline.py`; fehlt es (Attrappe
    im Test, entferntes Attribut), kommt `( [], False, HISTORY_REASON )`.
    Hier wird **keine** Speicherung eingeführt, nichts nachgeladen und nichts
    erfunden – die Allowlist :data:`HISTORY_FIELDS` sorgt dafür, dass der Puffer
    nicht mehr preisgibt als diese Liste, auch nicht maskiert.

    **P8.D3 – behobener Fehler:** ein **vorhandener, aber leerer** Puffer
    (``deque()`` direkt nach dem Start) ist ``available=True`` mit
    ``reason=None`` – er war live genau die Ursache für „keine Turn-Historie",
    obwohl die Historie durchaus vorhanden war. Nur wenn der Puffer
    **vorhanden, aber nicht auswertbar** ist (kein Mapping-Eintrag), gilt
    weiterhin ``available=False`` mit Begründung: das ist dann ein echter
    Befund und kein „noch nichts passiert".
    """
    history = getattr(pipeline_obj, "turn_history", None) if pipeline_obj is not None else None
    if history is None:
        return [], False, HISTORY_REASON
    if isinstance(history, (str, bytes)) or not isinstance(history, Iterable):
        return [], False, "Pipeline-Attribut 'turn_history' ist keine Sequenz – ignoriert."
    usable = [item for item in history if isinstance(item, Mapping)]
    if not usable:
        if len(list(history)) == 0:
            # P8.D3: Puffer da, noch kein Turn – **verfügbar**, einfach leer.
            return [], True, None
        return [], False, "Pipeline-Puffer 'turn_history' enthält keine auswertbaren Einträge."
    entries: list[HistoryEntry] = []
    for item in usable[-limit:]:
        projected: dict[str, Any] = {}
        for name in HISTORY_FIELDS:
            if name not in item:
                continue
            value = item[name]
            if name == "ts" and isinstance(value, (int, float)):
                value = datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
            projected[name] = value
        entries.append(HistoryEntry(**projected))
    return entries, True, None


# ── Wake-Evidenz (P9.T0 – **nur** lesend) ────────────────────────────────
def _optional_number(value: object) -> Optional[float]:
    """Zahl → `float`; alles andere (inkl. `bool`) → `None`.

    `bool` ist bewusst ausgeschlossen: `True` als Score wäre eine erfundene
    Zahl.  `NaN`/Inf fliegen raus – sie würden als JSON kaputt oder als
    "unendlich langsam" erscheinen.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    return None


def _median(values: list[float]) -> Optional[float]:
    """Median – bei gerader Länge der **mittlere** der beiden, auf 3 gerundet."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 3)


def _wake_stats(attempts: list[WakeAttempt]) -> WakeStats:
    """Kennzahlen über **diese** Antwort – keine Historie, keine Schätzung."""
    scores = [a.score for a in attempts if a.score is not None]
    accepted = sum(1 for a in attempts if a.accepted is True)
    rejected = sum(1 for a in attempts if a.accepted is False)
    reasons: dict[str, int] = {}
    for attempt in attempts:
        if attempt.reason:
            reasons[attempt.reason] = reasons.get(attempt.reason, 0) + 1
    # `best_rejected_score` = der höchste abgelehnte Score: die Zahl, die sagt
    # „so knapp war es".  Für die Wake-Diagnose die aussagekräftigste Kennzahl
    # überhaupt; `None`, wenn gar nichts abgelehnt wurde.
    rejected_scores = [
        a.score for a in attempts if a.accepted is False and a.score is not None
    ]
    return WakeStats(
        count=len(attempts),
        accepted=accepted,
        rejected=rejected,
        score_min=round(min(scores), 3) if scores else None,
        score_median=_median(scores),
        score_max=round(max(scores), 3) if scores else None,
        best_rejected_score=(
            round(max(rejected_scores), 3) if rejected_scores else None
        ),
        reasons=reasons,
    )


def _optional_int(value: object) -> Optional[int]:
    """Ganzzahl → `int`; alles andere (inkl. `bool`/Fließkomma) → `None`."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    return None


def _optional_text(value: object) -> Optional[str]:
    """Text → `str`; leeres/fehlendes → `None`.  Nichts wird **erfunden**."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value or None


def _wake_attempt_from(item: object, device_id: str) -> Optional[WakeAttempt]:
    """Ein Ring-Eintrag → `WakeAttempt`, **ohne** Ausnahme zu werfen.

    Bewusst tolerant: der Ring ist ein **Diagnose**puffer, und ein einzelner
    kaputter Wert darf nicht die ganze Antwort leeren.  Statt einen
    Validierungsfehler nach außen zu geben, wird der unbrauchbare Wert zu
    `None` – der Endpunkt meldet also ehrlich „unbekannt" statt zu raten.
    """
    if not isinstance(item, Mapping):
        return None
    raw_ts = item.get("ts")
    ts: Optional[str]
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        ts = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat()
    else:
        ts = _optional_text(raw_ts)
    accepted = item.get("accepted")
    return WakeAttempt(
        ts=ts,
        device_id=_optional_text(item.get("device_id")) or device_id,
        score=_optional_number(item.get("score")),
        accepted=accepted if isinstance(accepted, bool) else None,
        reason=_optional_text(item.get("reason")),
        threshold=_optional_number(item.get("threshold")),
        chunk_index=_optional_int(item.get("chunk_index")),
    )


def collect_wake(
    limit: int, pipeline_obj: Any, settings_obj: Any = None
) -> tuple[list[WakeAttempt], list[WakeDevice], Optional[float], str, Optional[int], list[str]]:
    """Wake-Versuche – **ausschließlich** aus dem Ringpuffer des Detektors.

    Quelle ist `Pipeline.wake_attempts(device_id)` je Gerät, darunter
    `WakeWordDetector.wake_attempts` (begrenzter `deque`, nur RAM).  Es wird
    **nichts** gespeichert, nichts nachgeladen, nichts gerechnet und nichts
    erfunden: fehlt das Attribut (Stub im Test, entfernte Pipeline), ist das
    Ergebnis eine **leere** Liste mit offener Begründung in `issues` – der
    Endpunkt antwortet trotzdem mit 200 und `count: 0`.

    Der Aufrufer wird dabei **nicht** blockiert: die Pipeline liest hier nur den
    bereits fertigen Ringpuffer (Datenstruktur, keine Modell-Auswertung).

    `settings_obj` kommt per `Depends(get_settings)` und dient **ausschließlich**
    der Herkunftsangabe (`threshold_source`) sowie der Barge-in-Schwelle – es
    wird **nie** ein Settings-Wert verwendet, um eine Entscheidung zu treffen
    oder eine fehlende Messung zu ersetzen (E94 (d)).

    Rückgabe: `(attempts, devices, threshold, threshold_source, window, issues)`.
    """
    issues: list[str] = []
    attempts: list[WakeAttempt] = []
    devices: list[WakeDevice] = []

    if pipeline_obj is None:
        issues.append(
            "Kein Pipeline-Objekt in app.state – es wurden keine Wake-Versuche gelesen."
        )
        return [], [], None, THRESHOLD_SOURCE_NONE, None, issues

    getter = getattr(pipeline_obj, "wake_attempts", None)
    if not callable(getter):
        issues.append(
            "Am gelesenen Pipeline-Objekt gibt es 'wake_attempts' nicht –"
            " es wurden keine Wake-Versuche gelesen."
        )
        return [], [], None, THRESHOLD_SOURCE_NONE, None, issues

    id_getter = getattr(pipeline_obj, "wake_device_ids", None)
    device_ids: list[str] = []
    if callable(id_getter):
        try:
            raw_ids = id_getter()
        except Exception as exc:  # pragma: no cover – Diagnose darf nicht werfen
            issues.append(f"wake_device_ids() nicht lesbar: {exc}")
            raw_ids = []
        if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, (str, bytes)):
            device_ids = [str(d) for d in raw_ids]
    else:
        issues.append(
            "Am gelesenen Pipeline-Objekt gibt es 'wake_device_ids' nicht –"
            " die Versuche lassen sich keinem Gerät zuordnen."
        )

    for device_id in device_ids:
        try:
            raw_entries = getter(device_id)
        except Exception as exc:  # pragma: no cover – Diagnose darf nicht werfen
            issues.append(f"Wake-Versuche von {device_id} nicht lesbar: {exc}")
            continue
        if not isinstance(raw_entries, Iterable) or isinstance(raw_entries, (str, bytes)):
            issues.append(
                f"Wake-Versuche von {device_id} sind keine Sequenz – ignoriert."
            )
            continue
        for item in list(raw_entries)[-limit:]:
            attempt = _wake_attempt_from(item, device_id)
            if attempt is not None:
                attempts.append(attempt)
        device_attempts = [a for a in attempts if a.device_id == device_id]
        devices.append(
            WakeDevice(
                device_id=device_id,
                threshold=_device_threshold(pipeline_obj, device_id),
                barge_in_threshold=_optional_number(
                    getattr(settings_obj, "oww_barge_in_threshold", None)
                ),
                count=len(device_attempts),
                accepted=sum(1 for a in device_attempts if a.accepted is True),
                rejected=sum(1 for a in device_attempts if a.accepted is False),
            )
        )

    # Alt → neu, damit die Antwort wie das Dashboard-History „neueste zuerst"
    # liest und die gekürzten Versuche die jüngsten sind.
    attempts.reverse()

    threshold_getter = getattr(pipeline_obj, "wake_threshold", None)
    threshold: Optional[float] = None
    threshold_source = THRESHOLD_SOURCE_NONE
    if callable(threshold_getter):
        try:
            threshold = _optional_number(threshold_getter())
        except Exception as exc:  # pragma: no cover – Diagnose darf nicht werfen
            issues.append(f"wake_threshold() nicht lesbar: {exc}")
        if threshold is not None:
            threshold_source = THRESHOLD_SOURCE_DEVICE
    if threshold is None:
        # Kein Gerät/kein Detektor: die **Settings**-Schwelle nennen, aber offen
        # als Settings-Herkunft kennzeichnen – nicht als „wirkt gerade".
        fallback = _optional_number(getattr(settings_obj, "oww_threshold", None))
        if fallback is not None:
            threshold = fallback
            threshold_source = THRESHOLD_SOURCE_SETTINGS
    return attempts, devices, threshold, threshold_source, WAKE_ATTEMPTS_MAXLEN, issues


def _device_threshold(pipeline_obj: Any, device_id: str) -> Optional[float]:
    """Wirksame Schwelle **eines** Geräts, falls der Detektor sie meldet.

    `Pipeline.wake_threshold()` liefert die wirksame Schwelle des repräsentativen
    Geräts; für eine Einzelabfrage wird sie – falls vorhanden – über
    `wake_attempts`-Einträge dieses Geräts bevorzugt, weil dort die **tatsächlich
    beim Versuch geltende** Schwelle steht (die ändert sich mit Barge-in).
    """
    getter = getattr(pipeline_obj, "wake_attempts", None)
    if callable(getter):
        try:
            entries = getter(device_id)
        except Exception:  # pragma: no cover – Diagnose darf nicht werfen
            entries = None
        if isinstance(entries, Iterable) and not isinstance(entries, (str, bytes)):
            # Der Ring ist aufsteigend sortiert (ältester zuerst).  Die
            # **jüngste** Schwelle ist die gerade geltende: der Cooldown senkt
            # sie auf `OWW_BARGE_IN_THRESHOLD` ab, und genau die soll das
            # Dashboard zeigen.  Deshalb von hinten suchen.
            for item in reversed(list(entries)):
                if isinstance(item, Mapping):
                    value = _optional_number(item.get("threshold"))
                    if value is not None:
                        return value
    threshold_getter = getattr(pipeline_obj, "wake_threshold", None)
    if callable(threshold_getter):
        try:
            return _optional_number(threshold_getter())
        except Exception:  # pragma: no cover – Diagnose darf nicht werfen
            return None
    return None


# ── Abhängigkeits-Health (HA wird gepingt, Whisper/Piper nicht) ─────────
async def _http_get(url: str, *, timeout: float) -> httpx.Response:
    """Ein einzelner HTTP-GET **ohne** Credential-Header (frischer Client).

    Es wird bewusst **nicht** der geteilte `ha_client` benutzt: dort steckt
    der Bearer-Token im Header, und der Health-Ping braucht kein Secret, um
    Erreichbarkeit zu beweisen.
    """
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        return await client.get(url)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def probe_url(
    url: str,
    *,
    timeout: float = DEFAULT_DEPENDENCY_TIMEOUT,
    opener: Optional[Any] = None,
) -> DependencyProbe:
    """GET auf `url`, hart begrenzt auf `timeout` ⇒ `DependencyProbe`.

    Jede HTTP-Antwort gilt als **erreichbar** (auch `401` – beweist, dass der
    Host antwortet; der Ping sendet keine Credentials). Timeout/Netzfehler ⇒
    `ok=False` mit `reason`, **kein** Hänger.
    """
    opener = opener or _http_get
    target = _safe_target(url)
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            opener(url, timeout=timeout),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return DependencyProbe(
            name="home_assistant",
            ok=False,
            target=target,
            reason=f"Timeout nach {timeout:.1f}s",
        )
    except httpx.HTTPError as exc:
        return DependencyProbe(
            name="home_assistant",
            ok=False,
            target=target,
            reason=type(exc).__name__,
        )
    except Exception as exc:  # pragma: no cover – defensiv, nie 500 durchlassen
        return DependencyProbe(
            name="home_assistant",
            ok=False,
            target=target,
            reason=type(exc).__name__,
        )
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    status = getattr(response, "status_code", None)
    detail: Optional[str] = None
    if status == 401:
        detail = "erreichbar, 401 erwartet (Ping ohne Credential)"
    elif status is not None and 200 <= int(status) < 400:
        detail = "erreichbar"
    elif status is not None:
        detail = f"erreichbar, unerwarteter Status {status}"
    return DependencyProbe(
        name="home_assistant",
        ok=True,
        status=int(status) if status is not None else None,
        latency_ms=latency_ms,
        target=target,
        detail=detail,
    )


def _safe_target(url: str) -> Optional[str]:
    """`host:port` aus einer URL – **ohne** Userinfo, Pfad und Query."""
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover – defensiv
        return None
    host = parts.hostname
    if not host:
        return None
    if parts.port:
        return f"{host}:{parts.port}"
    return host


async def ha_health_dependency(
    request: Request,
    timeout_seconds: float = Query(
        DEFAULT_DEPENDENCY_TIMEOUT,
        gt=0.0,
        le=MAX_DEPENDENCY_TIMEOUT,
        description="Zeitlimit des HA-Pings in Sekunden (hart, kein Hänger).",
    ),
) -> DependencyProbe:
    """FastAPI-Dependency: **der einzige** ausgehende Aufruf der API.

    Läuft ohne `ha_client` in `app.state` ohne Netzwerk und liefert dann ein
    `ok: null` mit Begründung. Tests überschreiben genau diese Dependency
    (`app.dependency_overrides[ha_health_dependency]`), ohne den echten Pfad
    zu berühren.
    """
    ha_client = getattr(request.app.state, "ha_client", None)
    base_url = getattr(ha_client, "base_url", None) if ha_client is not None else None
    if not base_url:
        return DependencyProbe(
            name="home_assistant",
            ok=None,
            reason="ha_client fehlt in app.state (kein Health-Ping möglich).",
        )
    url = f"{str(base_url).rstrip('/')}/api/"
    return await probe_url(url, timeout=float(timeout_seconds))


def _whisper_probe() -> DependencyProbe:
    """Whisper: **kein** Ping – Wyoming-TCP hat keinen Health-Endpunkt."""
    return DependencyProbe(
        name="whisper_stt",
        ok=None,
        reason=(
            "nicht I/O-frei ermittelbar: das Wyoming-Protokoll bietet keinen "
            "Health-Endpunkt; ein Test wäre ein Handshake mit Audio (I/O) – "
            "bewusst nicht implementiert."
        ),
    )


def _piper_probe() -> DependencyProbe:
    """Piper: **kein** Ping – Wyoming-TCP hat keinen Health-Endpunkt."""
    return DependencyProbe(
        name="piper_tts",
        ok=None,
        reason=(
            "nicht I/O-frei ermittelbar: das Wyoming-Protokoll bietet keinen "
            "Health-Endpunkt; ein Test wäre ein Handshake mit Audio (I/O) – "
            "bewusst nicht implementiert."
        ),
    )


# ── DI-Helfer (app.state) ──────────────────────────────────────────────
def get_settings(request: Request) -> Any:
    """`app.state.settings` – mit Modul-Default als Rückfall (nie `None`).

    Nur der **Wert**-Lieferant. Die Frage „ist es der Fallback?" beantwortet
    :func:`settings_source_for`; ein Objektidentitätsvergleich ist dafür
    unbrauchbar (siehe :func:`read_config`).
    """
    value = getattr(request.app.state, "settings", None)
    return _module_settings if value is None else value


def settings_source_for(request: Request) -> str:
    """Herkunft des Settings-**Objekts**: `app.state` oder `module-default`.

    Bewusst eine **Verfügbarkeits**-Prüfung (``getattr(..., None)``) und **kein**
    `is`-Vergleich: `app/main.py` vergibt dasselbe Modul-Singleton an
    `app.state`, ein Identitätsvergleich war also immer wahr. `None`/fehlend ⇒
    :data:`SETTINGS_SOURCE_MODULE_DEFAULT`; alles andere (auch ein Attrappen-
    Settings im Test) ⇒ :data:`SETTINGS_SOURCE_APP_STATE`.
    """
    value = getattr(request.app.state, "settings", None)
    if value is None:
        return SETTINGS_SOURCE_MODULE_DEFAULT
    return SETTINGS_SOURCE_APP_STATE


def get_state_attr(request: Request, name: str) -> Any:
    """`app.state.<name>` oder `None` (fehlende Komponente ⇒ graceful defaults)."""
    return getattr(request.app.state, name, None)


# ── Schreibmodus-Helfer (E96) ────────────────────────────────────────────
def write_enabled(settings_obj: Any) -> bool:
    """Ist der Dashboard-Schreibmodus wirksam?  **Nur** die eine Variable.

    Fehlt das Attribut (Stub im Test, uraltes Settings-Objekt) ⇒ `False`:
    ein Schreibpfad, dessen Freigabe nicht beweisbar ist, bleibt **zu**.
    """
    return getattr(settings_obj, WRITE_ENABLED_FIELD, False) is True


def bearer_token_ok(settings_obj: Any, request: Request) -> bool:
    """Ist die optionale API-Token-Schicht für `POST /api/config/*` erfüllt?

    **E110 (P11.T1).**  `dashboard_api_token` **leer** ⇒ Auth ist **aus**
    (open) ⇒ `True` (backward-kompatibel zu E96: ohne Token ändert sich
    nichts).  **Gesetzt** ⇒ der `Authorization`-Header muss exakt
    ``Bearer <token>`` sein; der Vergleich ist **konstantenzeitig**
    (`hmac.compare_digest`), damit keine Timing-Seite entsteht.  Fehlender
    Header, falsches Präfix oder falsch Token ⇒ `False` ⇒ die Route
    antwortet **401** – **vor** der Schreibmodus-Prüfung (403).  Der
    Tokenwert wird nie ausgegeben (nur Ja/Nein).
    """
    expected = getattr(settings_obj, API_TOKEN_FIELD, "") or ""
    if not expected:
        return True
    header = request.headers.get("authorization", "")
    if not header.startswith(BEARER_PREFIX):
        return False
    provided = header[len(BEARER_PREFIX):]
    return hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


def write_env_value(key: str, value: str, path: Optional[Path] = None) -> tuple[bool, str]:
    """**Genau einen** Key in der `.env` setzen – sonst keine Zeile anfassen.

    Bewusst **kein** Rewrite der ganzen Datei aus einer Dict-Repräsentation:
    Kommentare, Leerzeilen, Reihenfolge und die **Secrets** (`HA_TOKEN`,
    `LLM_API_KEY`) bleiben bytegleich, weil nur die eine gefundene Zeile
    ersetzt wird.  Ein Inline-Kommentar (`OWW_THRESHOLD=0.9  # …`) bleibt
    erhalten.  Gibt es die Zeile noch nicht, wird sie **angehängt**.

    Schreibweise: temporäre Datei im selben Verzeichnis + `os.replace` ⇒ kein
    halb geschriebener Zustand; Dateimodus der Vorlage wird übernommen, eine
    **neue** Datei bekommt `0600` (die `.env` enthält Secrets).

    Rückgabe `(ok, detail)`.  `detail` nennt **nie** einen Dateiinhalt, nur
    Pfadname/Aktion bzw. die Fehlerklasse – die Meldung wandert ins Audit-Log
    und darf dort kein Secret enthalten.
    """
    target = ENV_FILE if path is None else Path(path)
    try:
        existed = target.is_file()
        text = target.read_text(encoding="utf-8") if existed else ""
        mode = target.stat().st_mode & 0o777 if existed else 0o600
        lines = text.splitlines()
        rendered = f"{key}={value}"
        replaced = False
        for index, line in enumerate(lines):
            if line.split("=", 1)[0].strip() != key:
                continue
            comment = ""
            marker = line.find("#")
            if marker >= 0:
                comment = "  " + line[marker:].rstrip()
            lines[index] = rendered + comment
            replaced = True
            break
        if not replaced:
            lines.append(rendered)
        payload = "\n".join(lines) + "\n"
        temporary = target.with_name(target.name + ".p9t1.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError as exc:
        return False, f"{type(exc).__name__}"
    return True, ("ersetzt" if replaced else "angehängt") + f" in {target.name}"


def _validated_threshold(payload: object) -> float:
    """Body → Schwellenwert.  Wirft `HTTPException` (400/422) – nie `None`.

    * kein JSON-Objekt bzw. falscher Top-Level-Typ ⇒ **400**,
    * unbekanntes Feld oder fehlendes `threshold` ⇒ **400**,
    * `threshold` ist kein endlicher Float bzw. außerhalb **(0.0, 1.0]**
      ⇒ **422**.

    `bool` ist bewusst **kein** Float: `true` ⇒ 1,0 wäre ein stiller
    Höchstwert, und eine fehlende/verdrehte Anfrage soll **laut** scheitern.
    """
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=400, detail="Body muss ein JSON-Objekt sein.")
    keys = {str(k) for k in payload}
    if keys != {THRESHOLD_FIELD}:
        extra = sorted(keys - {THRESHOLD_FIELD})
        if not keys & {THRESHOLD_FIELD}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Body muss genau das Feld '{THRESHOLD_FIELD}' enthalten"
                    f"{'; unbekannt: ' + ', '.join(extra) if extra else ''}."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unbekannte Felder im Body: {', '.join(extra)} –"
                f" schreibbar ist ausschließlich '{THRESHOLD_FIELD}'"
                f" (Settings '{WRITABLE_SETTING}')."
            ),
        )
    raw = payload[THRESHOLD_FIELD]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HTTPException(
            status_code=422,
            detail=f"'{THRESHOLD_FIELD}' muss eine Zahl (float) sein.",
        )
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        raise HTTPException(
            status_code=422, detail=f"'{THRESHOLD_FIELD}' muss endlich sein."
        )
    if not (THRESHOLD_MIN < value <= THRESHOLD_MAX):
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{THRESHOLD_FIELD}' muss in ({THRESHOLD_MIN}, {THRESHOLD_MAX}]"
                f" liegen, ist {value}."
            ),
        )
    return value


def _apply_threshold(settings_obj: Any, value: float) -> tuple[Optional[float], bool]:
    """Settings-Instanz(en) setzen und **zurücklesen** (E96, ohne Neustart).

    `app/wake_word` liest `app.config.settings.oww_threshold` **bei jedem
    Vergleich**; das Modul-Singleton ist deshalb das eine Objekt, das wirklich
    zählt.  Das `app.state`-Objekt wird mitgesetzt, damit der Wert im
    laufenden Prozess **überall** derselbe ist (im Manager sind beide
    identisch).  Ein Objekt, das den Wert nicht annimmt (Stub), wird
    **übersprungen**, nicht ersetzt – und genau das meldet `settings_applied`
    zurück, damit nichts „erfolgreich" heißt, was der Prozess nicht sieht.
    """
    previous: Optional[float] = None
    applied = False
    current = _optional_number(getattr(settings_obj, WRITABLE_SETTING, None))
    try:
        setattr(settings_obj, WRITABLE_SETTING, value)
        previous = current
    except Exception:  # pragma: no cover – Settings-Objekt ohne Schreibzugriff
        pass
    if settings_obj is not _module_settings:
        try:
            setattr(_module_settings, WRITABLE_SETTING, value)
        except Exception:  # pragma: no cover – defensiv
            pass
    read_back = _optional_number(getattr(_module_settings, WRITABLE_SETTING, None))
    if read_back is not None and abs(read_back - value) < 1e-9:
        applied = True
    return previous, applied


def _render_env_value(value: Any) -> str:
    """Value → `.env`-Zeilenwert.  `bool` → `true`/`false`, sonst `str()`.

    Floats werden **nicht** formatiert (`0.2` bleibt `0.2`), damit die
    `.env` den eingegebenen Wert trägt und `app.config` ihn unverändert
    zurückliest (E96 hatte auf 2 Stellen formatiert; E111 belässt den Wert).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_config_field(field: str, raw: Any) -> Any:
    """Ein einzelnes E111-Feld prüfen und normalisieren.  Wirft **422**.

    Der Typ/Bereich spiegelt `app/config.py` (die `Field`-Constraints und die
    `field_validator`-Normalisierung).  `None` (explizite `null`) fällt in
    **jedem** Ast auf `422` – ein Feld, das „nichts" setzen will, wird nicht
    still ignoriert.
    """
    if field in _CONFIG_FLOAT_BOUNDS:
        lo, lo_incl, hi, hi_incl = _CONFIG_FLOAT_BOUNDS[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HTTPException(status_code=422, detail=f"'{field}' muss eine Zahl sein.")
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise HTTPException(status_code=422, detail=f"'{field}' muss endlich sein.")
        low_ok = value > lo if not lo_incl else value >= lo
        high_ok = value < hi if not hi_incl else value <= hi
        if not (low_ok and high_ok):
            lo_s = f"{'[' if lo_incl else '('}{lo}"
            hi_s = f"{hi}{']' if hi_incl else ')'}"
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' muss in {lo_s}{hi_s} liegen, ist {value}.",
            )
        return value
    if field in _CONFIG_INT_BOUNDS:
        lo, lo_incl, hi, hi_incl = _CONFIG_INT_BOUNDS[field]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise HTTPException(status_code=422, detail=f"'{field}' muss eine ganze Zahl sein.")
        value = int(raw)
        low_ok = value > lo if not lo_incl else value >= lo
        high_ok = True if hi is None else (value < hi if not hi_incl else value <= hi)
        if not (low_ok and high_ok):
            lo_s = f"{'[' if lo_incl else '('}{lo}"
            hi_s = "∞" if hi is None else f"{'[' if hi_incl else '('}{hi}{']' if hi_incl else ')'}"
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' muss in {lo_s}{hi_s} liegen, ist {value}.",
            )
        return value
    if field in _CONFIG_ENUMS:
        allowed = _CONFIG_ENUMS[field]
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"'{field}' muss ein String sein.")
        normalized = raw.strip()
        if field == "log_level":
            normalized = normalized.upper()
        elif field == "jev_mode":
            normalized = normalized.lower()
        if not normalized:
            raise HTTPException(status_code=422, detail=f"'{field}' darf nicht leer sein.")
        if normalized not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' muss eines von {', '.join(allowed)} sein, ist {raw!r}.",
            )
        return normalized
    if field == "audio_dump_enabled":
        if not isinstance(raw, bool):
            raise HTTPException(status_code=422, detail=f"'{field}' muss true/false sein.")
        return raw
    if field == "whisper_language":
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"'{field}' muss ein String sein.")
        normalized = raw.strip()
        if not normalized:
            raise HTTPException(status_code=422, detail=f"'{field}' darf nicht leer sein.")
        return normalized
    if field == "ha_entity_domains":
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail=f"'{field}' muss ein String sein.")
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        for part in parts:
            if not _DOMAIN_RE.match(part):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{field}' enthält ungültiges Domain {part!r}"
                        " (erlaubt: a-z, 0-9, _)."
                    ),
                )
        return ",".join(parts)
    raise HTTPException(status_code=422, detail=f"'{field}' ist nicht schreibbar.")


def _validated_config_payload(payload: object) -> dict[str, Any]:
    """Body → geprüfte Multi-Field-Config (E111).  Wirft `HTTPException`.

    * kein JSON-Objekt bzw. leeres Objekt ⇒ **400**,
    * unbekanntes Feld (außerhalb der E111-Allowlist) ⇒ **400**,
    * Typ-/Bereichs-/Wertefehler pro Feld ⇒ **422**
      (:func:`_validate_config_field`).

    Rückgabe ist ein Dict `feld → normalisierter Wert` (nur die angebotenen
    Felder; die Sortierung übernimmt der Aufrufer).
    """
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=400, detail="Body muss ein JSON-Objekt sein.")
    fields = {str(key) for key in payload}
    if not fields:
        raise HTTPException(
            status_code=400, detail="Body ist leer – mindestens ein Feld angeben."
        )
    unknown = fields - CONFIG_WRITE_FIELD_NAMES
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unbekannte Felder im Body: "
                + ", ".join(sorted(unknown))
                + " – schreibbar sind ausschließlich: "
                + ", ".join(sorted(CONFIG_WRITE_FIELD_NAMES))
                + "."
            ),
        )
    return {field: _validate_config_field(field, payload[field]) for field in fields}


def _apply_live_setting(settings_obj: Any, field: str, value: Any) -> bool:
    """Live-Feld in die Settings-Instanz(en) schreiben und **zurücklesen**.

    Wie :func:`_apply_threshold`, aber für **beliebige** E111-Felder, die
    ohne Neustart wirken.  Die meisten `app/*`-Module lesen diese Werte bei
    Aufruf aus `app.config.settings` (dem Modul-Singleton) ⇒ genau das Objekt
    zählt.  Das `app.state`-Objekt wird mitgesetzt, damit der Wert im
    laufenden Prozess **überall** derselbe ist.  Ein Objekt, das den Wert
    nicht annimmt (Stub), wird **übersprungen**, nicht ersetzt – und genau
    das meldet der Rückgabewert zurück (ehrlich statt „erfolgreich").
    """
    try:
        setattr(settings_obj, field, value)
    except Exception:  # pragma: no cover – Settings-Objekt ohne Schreibzugriff
        pass
    if settings_obj is not _module_settings:
        try:
            setattr(_module_settings, field, value)
        except Exception:  # pragma: no cover – defensiv
            pass
    return getattr(_module_settings, field, None) == value


def _state_name(value: Any) -> Optional[str]:
    """`PipelineState`/str → String; unbekannt ⇒ `None` statt Exception."""
    raw = getattr(value, "value", value)
    return str(raw) if isinstance(raw, str) else None


async def _device_sessions(request: Request) -> tuple[dict[str, Any], list[str]]:
    """Registry-Snapshot (`device_id` → Session) aus `app.state.ws_server`.

    `snapshot()` ist der einzige Zugriff – er läuft unter dem Registry-Lock,
    macht aber **keine** I/O. Fehlt die Registry, kommt `({}, [issue])`.
    """
    issues: list[str] = []
    ws_server = get_state_attr(request, "ws_server")
    registry = getattr(ws_server, "registry", None) if ws_server is not None else None
    if registry is None:
        issues.append("ws_server/registry fehlt in app.state (keine Gerätedaten).")
        return {}, issues
    try:
        snapshot = getattr(registry, "snapshot", None)
        if snapshot is not None:
            return dict(await snapshot()), issues
        ids = await registry.ids()  # Fallback für eine Registry ohne `snapshot()`
        return {str(device_id): None for device_id in ids}, issues
    except Exception as exc:  # pragma: no cover – defensiv, nie 500
        issues.append(f"Registry nicht lesbar: {type(exc).__name__}.")
        return {}, issues


def _device_status(device_id: str, session: Any) -> DeviceStatus:
    """Geräte-Zeile: nur Betriebsdaten, kein Audioinhalt, kein Token."""
    connected: Optional[bool] = None
    connected_seconds: Optional[float] = None
    if session is not None:
        dead = getattr(session, "dead", None)
        if isinstance(dead, bool):
            connected = not dead
        connected_at = getattr(session, "connected_at", None)
        if isinstance(connected_at, (int, float)):
            connected_seconds = round(max(0.0, time.monotonic() - float(connected_at)), 1)
    return DeviceStatus(
        device_id=device_id,
        connected=connected,
        connected_seconds=connected_seconds,
    )


# ── Endpunkte ───────────────────────────────────────────────────────────
@router.get("/status", response_model=StatusResponse)
async def read_status(request: Request, settings_obj: Any = Depends(get_settings)) -> StatusResponse:
    """Live-Status: Dienst, mDNS, Geräte (ID/State/Verbundenheit), Uptime, API-Health."""
    issues: list[str] = []
    try:
        sessions, session_issues = await _device_sessions(request)
        issues.extend(session_issues)
        pipeline_obj = get_state_attr(request, "pipeline")
        devices: list[DeviceStatus] = []
        for device_id, session in sessions.items():
            status = _device_status(device_id, session)
            if pipeline_obj is not None:
                try:
                    status.state = _state_name(pipeline_obj.state_of(device_id))
                except Exception as exc:
                    issues.append(f"state_of({device_id}): {type(exc).__name__}.")
            devices.append(status)
        devices.sort(key=lambda item: item.device_id)

        mdns = get_state_attr(request, "mdns")
        mdns_running = getattr(mdns, "is_running", None) if mdns is not None else None
        if mdns_running is None:
            issues.append("mdns fehlt in app.state (is_running unbekannt).")

        return StatusResponse(
            service=SERVICE_NAME,
            version=__version__,
            uptime_seconds=round(max(0.0, time.monotonic() - MODULE_STARTED_AT), 3),
            mdns_running=mdns_running if isinstance(mdns_running, bool) else None,
            device_count=len(devices),
            devices=devices,
            health=HealthInfo(router_ok=True, exception=None),
            degraded=bool(issues),
            issues=issues,
        )
    except Exception as exc:  # pragma: no cover – Dashboard darf nie weiß ausfallen
        _LOG.debug("GET /api/status degradiert: %r", exc)
        return StatusResponse(
            service=SERVICE_NAME,
            version=__version__,
            uptime_seconds=round(max(0.0, time.monotonic() - MODULE_STARTED_AT), 3),
            mdns_running=None,
            device_count=0,
            devices=[],
            health=HealthInfo(router_ok=False, exception=type(exc).__name__),
            degraded=True,
            issues=[f"Status nicht ermittelbar: {type(exc).__name__}."],
        )


@router.get("/pairing", response_model=PairingResponse)
async def read_pairing(request: Request, settings_obj: Any = Depends(get_settings)) -> PairingResponse:
    """Pairing-Status (P10.T3): mDNS + Geräte + klare Wizard-Ampel.

    Rein lesend und additiv – das Verhalten des Managers wird nicht berührt.
    `mic_synced` kommt (wenn vorhanden) aus `Pipeline.mic_synced` (Details
    §3.3: „verbunden" erst nach Mic-Seq-Sync); `last_seen` wird ehrlich nicht
    geführt (siehe `PairingDevice`).
    """
    issues: list[str] = []
    try:
        sessions, session_issues = await _device_sessions(request)
        issues.extend(session_issues)
        pipeline_obj = get_state_attr(request, "pipeline")
        devices: list[PairingDevice] = []
        for device_id, session in sessions.items():
            connected: Optional[bool] = None
            connected_seconds: Optional[float] = None
            if session is not None:
                dead = getattr(session, "dead", None)
                if isinstance(dead, bool):
                    connected = not dead
                connected_at = getattr(session, "connected_at", None)
                if isinstance(connected_at, (int, float)):
                    connected_seconds = round(
                        max(0.0, time.monotonic() - float(connected_at)), 1
                    )
            state: Optional[str] = None
            if pipeline_obj is not None:
                try:
                    state = _state_name(pipeline_obj.state_of(device_id))
                except Exception as exc:
                    issues.append(f"state_of({device_id}): {type(exc).__name__}.")
            mic_synced: Optional[bool] = None
            mic_reader = getattr(pipeline_obj, "mic_synced", None)
            if callable(mic_reader):
                try:
                    mic_synced = mic_reader(device_id)
                except Exception as exc:
                    issues.append(f"mic_synced({device_id}): {type(exc).__name__}.")
            devices.append(
                PairingDevice(
                    device_id=device_id,
                    connected=connected,
                    state=state,
                    last_seen=None,
                    connected_seconds=connected_seconds,
                    mic_synced=mic_synced if isinstance(mic_synced, bool) else None,
                )
            )
        devices.sort(key=lambda item: item.device_id)

        mdns = get_state_attr(request, "mdns")
        mdns_running = getattr(mdns, "is_running", None) if mdns is not None else None
        mdns_enabled = getattr(mdns, "enabled", None) if mdns is not None else None
        if mdns_running is None:
            issues.append("mdns fehlt in app.state (is_running unbekannt).")
        if mdns_enabled is None:
            issues.append("mdns.enabled unbekannt (Announcer ohne enabled-Attribut?).")
        announced = getattr(mdns, "announced_name", None) if mdns is not None else None
        name_conflict = bool(getattr(mdns, "name_conflict", False))
        configured_name = None
        settings_like = get_state_attr(request, "settings")
        if settings_like is not None:
            configured_name = getattr(settings_like, "manager_mdns_name", None)

        # Ampel (Doku siehe PairingResponse): paired > waiting > mdns_off > unknown.
        any_connected = any(item.connected is True for item in devices)
        if mdns_enabled is False:
            status = "mdns_off"
            hint = (
                "mDNS ist deaktiviert (MANAGER_MDNS_ENABLED=false) – der Dot kann "
                "den Manager nicht selbst finden. Zum Pairing auf true stellen."
            )
        elif any_connected:
            status = "paired"
            hint = "Dot verbunden – Handshake läuft automatisch ab (config → mic_start)."
        elif mdns_running is True:
            status = "waiting"
            hint = (
                "mDNS aktiv – warte auf den Dot (er verbindet sich selbst, "
                "üblicherweise binnen ~1 Minute)."
            )
        else:
            status = "unknown"
            hint = "Pairing-Zustand nicht ermittelbar – issues lesen."

        return PairingResponse(
            service=SERVICE_NAME,
            version=__version__,
            mdns_enabled=mdns_enabled if isinstance(mdns_enabled, bool) else None,
            mdns_running=mdns_running if isinstance(mdns_running, bool) else None,
            mdns_announced_as=announced if isinstance(announced, str) else None,
            mdns_name_conflict=name_conflict
            or (announced is not None and configured_name is not None and announced != configured_name),
            device_count=len(devices),
            devices=devices,
            status=status,
            hint=hint,
            degraded=bool(issues),
            issues=issues,
        )
    except Exception as exc:  # pragma: no cover – Dashboard darf nie weiß ausfallen
        _LOG.debug("GET /api/pairing degradiert: %r", exc)
        return PairingResponse(
            service=SERVICE_NAME,
            version=__version__,
            mdns_enabled=None,
            mdns_running=None,
            mdns_announced_as=None,
            mdns_name_conflict=False,
            device_count=0,
            devices=[],
            status="unknown",
            hint="Pairing-Zustand nicht ermittelbar – issues lesen.",
            degraded=True,
            issues=[f"Pairing-Status nicht ermittelbar: {type(exc).__name__}."],
        )


@router.get("/state", response_model=StateResponse)
async def read_state(request: Request) -> StateResponse:
    """Zustandsmaschine: kanonische Zustände (§5) + Zustand je Gerät."""
    issues: list[str] = []
    try:
        sessions, session_issues = await _device_sessions(request)
        issues.extend(session_issues)
        pipeline_obj = get_state_attr(request, "pipeline")
        devices: list[DeviceWorkflow] = []
        for device_id in sorted(sessions):
            current: Optional[str] = None
            if pipeline_obj is not None:
                try:
                    current = _state_name(pipeline_obj.state_of(device_id))
                except Exception as exc:
                    issues.append(f"state_of({device_id}): {type(exc).__name__}.")
            devices.append(
                DeviceWorkflow(
                    device_id=device_id,
                    current_state=current,
                    # Der Pipeline-Code führt weder Previous-State noch Übergangs-
                    # Zeitpunkt ⇒ ehrlich `null` statt eines geschätzten Werts.
                    previous_state=None,
                    since=None,
                )
            )
        if pipeline_obj is None:
            issues.append("pipeline fehlt in app.state (keine Zustände je Gerät).")
        return StateResponse(
            states=build_state_catalog(),
            devices=devices,
            transitions_tracked=TRANSITIONS_TRACKED,
            graph_source=GRAPH_SOURCE,
            degraded=bool(issues),
            issues=issues,
        )
    except Exception as exc:  # pragma: no cover – defensiv
        _LOG.debug("GET /api/state degradiert: %r", exc)
        return StateResponse(
            states=build_state_catalog(),
            devices=[],
            transitions_tracked=TRANSITIONS_TRACKED,
            graph_source=GRAPH_SOURCE,
            degraded=True,
            issues=[f"Zustand nicht ermittelbar: {type(exc).__name__}."],
        )


@router.get("/logs", response_model=LogsResponse)
async def read_logs(
    limit: int = Query(
        DEFAULT_LOGS_LIMIT,
        description="Maximale Zahl Einträge; wird auf das Maximum geklemmt.",
    ),
    level: Optional[str] = Query(
        None,
        description="Level-Filter, kommagetrennt (z. B. 'WARNING,error'); unbekannt ⇒ leer.",
    ),
    logger: Optional[str] = Query(
        None, description="Teilstring des Logger-Namens (case-insensitiv)."
    ),
    module: Optional[str] = Query(
        None, description="Alias für `logger` (z. B. 'pipeline')."
    ),
    since_seconds: Optional[float] = Query(
        None, gt=0.0, description="Nur Einträge, die jünger als X Sekunden sind."
    ),
) -> LogsResponse:
    """Log-Historie (redigiert): Memory-Puffer, sonst Logdateien, sonst ehrlich leer.

    Ein **unbekannter** `level`-Filter ist kein Fehler: die Antwort ist dann
    eine leere Liste (nie ein 4xx/5xx) und `degraded` bleibt unberührt – der
    Filter ist eine Eingabe des Clients, kein Ausfall der Datenquelle.
    """
    issues: list[str] = []
    try:
        effective_limit = clamp_limit(limit, maximum=MAX_LOGS_LIMIT)
        if effective_limit != limit:
            issues.append(f"limit={limit} auf {effective_limit} geklemmt.")
        levels = parse_levels(level)
        entries, source, log_dir, source_issues = collect_log_entries(
            limit=effective_limit,
            levels=levels,
            logger_filter=logger,
            module_filter=module,
            since_seconds=since_seconds,
        )
        issues.extend(source_issues)
        return LogsResponse(
            entries=entries,
            source=source,
            redacted=True,
            count=len(entries),
            log_dir=log_dir,
            degraded=bool(issues),
            issues=issues,
        )
    except Exception as exc:  # pragma: no cover – defensiv
        _LOG.debug("GET /api/logs degradiert: %r", exc)
        return LogsResponse(
            entries=[],
            source="none",
            redacted=True,
            count=0,
            log_dir=None,
            degraded=True,
            issues=[f"Logs nicht lesbar: {type(exc).__name__}."],
        )


@router.get("/history", response_model=HistoryResponse)
async def read_history(
    request: Request,
    limit: int = Query(
        DEFAULT_HISTORY_LIMIT,
        description="Maximale Zahl Turns; wird auf das Maximum geklemmt.",
    ),
) -> HistoryResponse:
    """Turn-Historie – leer mit `available: false`, solange die Pipeline keinen Puffer hält."""
    issues: list[str] = []
    try:
        effective_limit = clamp_limit(limit, maximum=MAX_HISTORY_LIMIT)
        if effective_limit != limit:
            issues.append(f"limit={limit} auf {effective_limit} geklemmt.")
        pipeline_obj = get_state_attr(request, "pipeline")
        if pipeline_obj is None:
            issues.append("pipeline fehlt in app.state (Historie nicht prüfbar).")
        entries, available, reason = collect_history(effective_limit, pipeline_obj)
        return HistoryResponse(
            entries=entries,
            available=available,
            reason=reason,
            count=len(entries),
            degraded=bool(issues),
            issues=issues,
        )
    except Exception as exc:  # pragma: no cover – defensiv
        _LOG.debug("GET /api/history degradiert: %r", exc)
        return HistoryResponse(
            entries=[],
            available=False,
            reason=f"Historie nicht ermittelbar: {type(exc).__name__}.",
            count=0,
            degraded=True,
            issues=[type(exc).__name__],
        )


@router.get("/config", response_model=ConfigResponse)
async def read_config(
    request: Request, settings_obj: Any = Depends(get_settings)
) -> ConfigResponse:
    """Konfiguration als Lese-Export: Allowlist, **ohne** jedes Secret-Feld.

    P8.D3 – **behobener Fehler.** Vorher stand hier
    ``if settings_obj is _module_settings:`` als „fehlt"-Erkennung. Das ist
    **immer wahr**: `app/dashboard.py:76` bindet das Modul-Singleton
    ``from app.config import settings as _module_settings`` ein, und
    `app/main.py:284` vergibt **genau dieses Objekt** an
    ``application.state.settings``. Folge war der live sichtbare gelbe Balken
    „app.state.settings fehlt" obwohl die Werte korrekt waren.

    Die Verfügbarkeit wird jetzt **an der Quelle** festgestellt – über
    ``request.app.state`` (:func:`settings_source_for`), **nicht** über
    Objektidentität. Damit gilt:

    * ``app.state.settings`` **vorhanden** ⇒ ``settings_source="app.state"``,
      **kein** `degraded` (nur was die Projektion selbst meldet, z. B. ein
      fehlendes Feld).
    * ``app.state.settings`` **fehlt** ⇒ Rückfall auf den Modul-Singleton über
      :func:`get_settings`, ``settings_source="module-default"``,
      ``degraded: true`` + :data:`SETTINGS_MISSING_ISSUE`. Die Werte werden
      trotzdem geliefert (nie ein 500 aus dem Poller heraus).

    Die **Werte** selbst ändert der Fix nicht – dieselbe
    :func:`project_config`, dieselbe Allowlist, dieselbe Secret-Prüfung.
    """
    issues: list[str] = []
    try:
        settings_source = settings_source_for(request)
        if settings_source == SETTINGS_SOURCE_MODULE_DEFAULT:
            issues.append(SETTINGS_MISSING_ISSUE)
        values, projection_issues = project_config(settings_obj)
        issues.extend(projection_issues)
        return ConfigResponse(
            service=SERVICE_NAME,
            version=__version__,
            values=values,
            settings_source=settings_source,
            degraded=bool(issues),
            issues=issues,
            write_token_required=bool(
                getattr(settings_obj, API_TOKEN_FIELD, "") or ""
            ),
            effective_without_restart=dict(CONFIG_WRITE_LIVE_MAP),
        )
    except Exception as exc:  # pragma: no cover – defensiv
        _LOG.debug("GET /api/config degradiert: %r", exc)
        return ConfigResponse(
            service=SERVICE_NAME,
            version=__version__,
            values=[],
            settings_source=settings_source_for(request),
            degraded=True,
            issues=[f"Konfiguration nicht lesbar: {type(exc).__name__}."],
            write_token_required=bool(
                getattr(settings_obj, API_TOKEN_FIELD, "") or ""
            ),
            effective_without_restart=dict(CONFIG_WRITE_LIVE_MAP),
        )


@router.get("/dependencies", response_model=DependenciesResponse)
async def read_dependencies(
    probe: DependencyProbe = Depends(ha_health_dependency),
    timeout_seconds: float = Query(
        DEFAULT_DEPENDENCY_TIMEOUT,
        gt=0.0,
        le=MAX_DEPENDENCY_TIMEOUT,
        description="Zeitlimit des HA-Pings (hart geklemmt).",
    ),
) -> DependenciesResponse:
    """Abhängigkeits-Health: HA (einziger Ping, ohne Credentials), Whisper/Piper `null`."""
    dependencies = [probe, _whisper_probe(), _piper_probe()]
    issues: list[str] = []
    if probe.ok is not True:
        issues.append(f"home_assistant: {probe.reason or 'nicht erreichbar'}")
    return DependenciesResponse(
        timeout_seconds=float(timeout_seconds),
        dependencies=dependencies,
        degraded=any(item.ok is not True for item in dependencies),
        issues=issues,
    )


@router.get("/wake", response_model=WakeResponse)
async def read_wake(
    request: Request,
    limit: int = Query(
        DEFAULT_WAKE_LIMIT,
        ge=1,
        le=MAX_WAKE_LIMIT,
        description="Höchstzahl der Wake-Versuche (neueste zuerst).",
    ),
    settings_obj: Any = Depends(get_settings),
) -> WakeResponse:
    """**Reine Lese-Ansicht** auf den Wake-Ringpuffer (P9.T0/E96).

    Kein Schreibpfad, keine Parameter, die etwas ändern könnten, **kein**
    Eingriff in die Erkennung: gelesen wird nur der bereits fertige Ringpuffer
    `WakeWordDetector.wake_attempts` (begrenzt, nur RAM).  Der Endpunkt ist
    damit auch im laufenden Betrieb gefahrlos abrufbar.

    **Ohne Versuch ist `count: 0`**, und `score_min`/`score_median`/
    `score_max`/`best_rejected_score` bleiben `null` – keine erfundenen 0.0.
    Fehlt die Pipeline ganz, antwortet der Endpunkt trotzdem **200** mit
    `degraded: true` und einer Begründung in `issues` (ehrlich statt 500).

    **E96:** Der Ring enthält nur Versuche **über** `keep_floor` (die Stille-
    Chunks mit score 0,0 haben ihn live zugesetzt); die Felder `keep_floor`
    und `default_threshold` machen das sichtbar.  `write_enabled` entscheidet,
    ob das Frontend die Schreib-UI zeigt.
    """
    pipeline_obj = get_state_attr(request, "pipeline")
    limit = clamp_limit(limit, maximum=MAX_WAKE_LIMIT, minimum=1)
    try:
        attempts, devices, threshold, source, window, issues = collect_wake(
            limit, pipeline_obj, settings_obj
        )
    except Exception as exc:  # pragma: no cover – defensiv
        _LOG.debug("GET /api/wake degradiert: %r", exc)
        return WakeResponse(
            attempts=[],
            count=0,
            stats=_wake_stats([]),
            devices=[],
            threshold=_optional_number(
                getattr(settings_obj, "oww_threshold", None)
            ),
            threshold_source=THRESHOLD_SOURCE_SETTINGS
            if getattr(settings_obj, "oww_threshold", None) is not None
            else THRESHOLD_SOURCE_NONE,
            window=WAKE_ATTEMPTS_MAXLEN,
            write_enabled=write_enabled(settings_obj),
            keep_floor=_optional_number(
                getattr(settings_obj, "wake_attempt_keep_floor", None)
            ),
            default_threshold=_optional_number(
                getattr(settings_obj, "oww_threshold", None)
            ),
            degraded=True,
            issues=[f"Wake-Versuche nicht lesbar: {type(exc).__name__}."],
        )
    return WakeResponse(
        attempts=attempts,
        count=len(attempts),
        stats=_wake_stats(attempts),
        devices=devices,
        threshold=threshold,
        threshold_source=source,
        barge_in_threshold=_optional_number(
            getattr(settings_obj, "oww_barge_in_threshold", None)
        ),
        window=window,
        write_enabled=write_enabled(settings_obj),
        keep_floor=_optional_number(
            getattr(settings_obj, "wake_attempt_keep_floor", None)
        ),
        default_threshold=_optional_number(
            getattr(settings_obj, "oww_threshold", None)
        ),
        degraded=bool(issues),
        issues=issues,
    )


@router.post("/config/oww-threshold", response_model=ThresholdWriteResponse)
async def write_oww_threshold(
    request: Request, settings_obj: Any = Depends(get_settings)
) -> ThresholdWriteResponse:
    """**Die einzige** Schreibroute des Dashboards (E96) – eine Zahl, sonst nichts.

    * **Auth-Schicht (E110), geprüft als erste:** solange `DASHBOARD_API_TOKEN`
      gesetzt ist, verlangt die Route `Authorization: Bearer <token>`
      (exakt, konstantenzeitig).  Fehlend/falsch ⇒ **401** – **vor** der
      Schreibmodus-Prüfung, damit ein Nicht-Authentizierter nichts über den
      Schreibmodus erfährt.  `DASHBOARD_API_TOKEN` **leer** ⇒ offen (401 entfällt).
    * **Gesperrt, solange `DASHBOARD_WRITE_ENABLED` nicht `true` ist** ⇒
      **403** (nicht 2xx, nicht 404: der Betrieb verweigert, die Route
      existiert).  Der Body wird dann gar nicht erst gelesen.
    * Schreibbar ist **ausschließlich** `oww_threshold` als Float in
      **(0.0, 1.0]**; Fremdfeld/fehlendes Feld ⇒ **400**, unbrauchbarer oder
      außerhalb liegender Wert ⇒ **422**.  Ein Versuch, irgendetwas anderes zu
      setzen, wird **nicht** stillschweigend ignoriert, sondern abgewiesen.
    * **Kein** HA-Call, **kein** `/api/services`, **kein** TTS, **kein** LED:
      das Dashboard schaltet **nie** ein Gerät.  Diese Route bewegt genau eine
      Zahl in der Wake-Erkennung.
    * **Ohne Neustart wirksam:** der Wert wird in die Settings-Instanz
      geschrieben, die `app/wake_word._idle_threshold` **bei jedem Vergleich**
      liest, und **zurückgelesen** (`settings_applied`).  Ein nicht
      zurücklesbarer Wert wird als solcher gemeldet, nicht als Erfolg.
    * **Persistenz:** zusätzlich wird genau **eine** Zeile der `.env`
      (`app.config.ENV_FILE`, die Datei hinter `env_file:`) gesetzt – ohne
      andere Zeile anzufassen, ohne ein Secret zu loggen.  Fehlt die Datei,
      wird sie mit `0600` neu angelegt (dann ohne die übrigen Defaults: die
      Defaults stehen im Code).
      **Grenze der Persistenz (E96, live geprüft):** im Docker-Betrieb ist
      das die `.env` **im Container** (Writable-Layer, `Dockerfile` kopiert
      nur `app/`, es gibt **keinen** Bind-Mount).  Sie übersteht damit
      `docker restart` / `docker compose restart`, **nicht** aber
      `docker compose up -d --build` (Recreate) – danach liest der Prozess
      wieder die **Host**-`.env`, weil `env_file:` dort auf sie zeigt.  Für
      einen dauerhaften Wert muss die Host-`.env` geändert werden
      (`OWW_THRESHOLD=`); diese bewusst **nicht** zu mounten, ist eine
      Folge von E17 (keine Schreibfläche auf der Secret-Datei).
    * **Audit:** jede Änderung auf `INFO` (Logger `manager.dashboard`, im
      Dashboard-Log-Stream sichtbar): alter Wert, neuer Wert, Quelle `ui`,
      Persistenz-Ergebnis.  **Kein** Secret im Log.
    """
    if not bearer_token_ok(settings_obj, request):
        raise HTTPException(
            status_code=WRITE_UNAUTHORIZED_STATUS,
            detail="Nicht autorisiert – gültiger API-Token fehlt.",
        )
    if not write_enabled(settings_obj):
        raise HTTPException(
            status_code=WRITE_DISABLED_STATUS,
            detail=(
                "Dashboard-Schreibmodus ist aus"
                f" ({WRITE_ENABLED_FIELD} ist nicht true) – es wurde nichts"
                " geändert."
            ),
        )
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body ist kein gültiges JSON.") from None
    value = _validated_threshold(payload)
    previous, applied = _apply_threshold(settings_obj, value)
    persisted, detail = write_env_value(WRITABLE_SETTING.upper(), f"{value:.2f}")
    stamp = datetime.now(timezone.utc).isoformat()
    _LOG.info(
        "Dashboard-Schreibmodus: %s %.3f -> %.3f (Quelle=%s, Laufzeit=%s,"
        " .env=%s: %s)",
        WRITABLE_SETTING,
        previous if previous is not None else float("nan"),
        value,
        WRITE_SOURCE_UI,
        "ja" if applied else "nein",
        "ok" if persisted else "fehlgeschlagen",
        detail,
    )
    return ThresholdWriteResponse(
        applied=True,
        previous=previous,
        threshold=value,
        persisted_env=persisted,
        env_file=ENV_FILE.name if persisted else None,
        settings_applied=applied,
        effective_without_restart=applied,
        applied_at=stamp,
    )


@router.put("/config", response_model=ConfigWriteResponse)
async def write_config(
    request: Request, settings_obj: Any = Depends(get_settings)
) -> ConfigWriteResponse:
    """**Multi-Field-Schreibroute** des Dashboards (E111) – die E111-Kern-Felder.

    * **Auth-Schicht (E110), geprüft als erste:** solange `DASHBOARD_API_TOKEN`
      gesetzt ist, verlangt die Route `Authorization: Bearer <token>`
      (exakt, konstantenzeitig).  Fehlend/falsch ⇒ **401** – **vor** der
      Schreibmodus-Prüfung, damit ein Nicht-Authentizierter nichts über den
      Schreibmodus erfährt.  `DASHBOARD_API_TOKEN` **leer** ⇒ offen (401 entfällt).
    * **Gesperrt, solange `DASHBOARD_WRITE_ENABLED` nicht `true` ist** ⇒
      **403** (nicht 2xx, nicht 404: der Betrieb verweigert, die Route
      existiert).  Der Body wird dann gar nicht erst gelesen.
    * Schreibbar sind **ausschließlich** die E111-Felder
      (:data:`CONFIG_WRITE_FIELDS`); Fremdfeld oder leerer Body ⇒ **400**,
      Typ-/Bereichs-/Wertefehler ⇒ **422**.  Ein Versuch, irgendetwas anderes
      zu setzen, wird **nicht** stillschweigend ignoriert, sondern abgewiesen.
    * **Live-Felder** (`oww_threshold`, `log_level`, `audio_dump_enabled`,
      `whisper_language`) werden in die Settings-Instanz geschrieben und
      wirken **ohne** Neustart; alle anderen Felder brauchen einen Neustart
      und landen in `restarted_required`.
    * **Persistenz:** pro Feld **eine** Zeile der `.env` via
      :func:`write_env_value` – ohne andere Zeile anzufassen, ohne ein Secret
      zu loggen.  Fehlt die Datei, wird sie mit `0600` neu angelegt.
    * **Audit:** jede Änderung auf `INFO` (im Dashboard-Log-Stream sichtbar):
      Felder mit neuem Wert, Quelle `ui`, Persistenz-Ergebnis.  **Kein**
      Secret im Log.
    """
    if not bearer_token_ok(settings_obj, request):
        raise HTTPException(
            status_code=WRITE_UNAUTHORIZED_STATUS,
            detail="Nicht autorisiert – gültiger API-Token fehlt.",
        )
    if not write_enabled(settings_obj):
        raise HTTPException(
            status_code=WRITE_DISABLED_STATUS,
            detail=(
                "Dashboard-Schreibmodus ist aus"
                f" ({WRITE_ENABLED_FIELD} ist nicht true) – es wurde nichts"
                " geändert."
            ),
        )
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body ist kein gültiges JSON.") from None
    fields = _validated_config_payload(payload)
    written: list[str] = sorted(fields)
    effective: dict[str, bool] = {}
    restarted: list[str] = []
    env_results: list[str] = []
    for field in written:
        value = fields[field]
        live = CONFIG_WRITE_LIVE_MAP[field]
        if live:
            _apply_live_setting(settings_obj, field, value)
        persisted, _detail = write_env_value(
            CONFIG_WRITE_ENV_KEYS[field], _render_env_value(value)
        )
        effective[field] = live
        if not live:
            restarted.append(field)
        env_results.append(f"{field}={'ok' if persisted else 'fehlgeschlagen'}")
    if "log_level" in fields:
        # Live-Wirkung ohne Neustart: configure_logging() ist idempotent
        # (Installierter Handler bleibt; nur der Level wird neu aufgelöst —
        # aus dem soeben gesetzten Settings-Attribut). Ohne diesen Re-Apply
        # bliebe der laufende Logger bis zum Neustart auf dem alten Level
        # (Live-Befund P11.T4 auf .106: DEBUG-Zeilen erschienen nicht).
        configure_logging()
    _LOG.info(
        "Dashboard-Config-Write (E111): %s (Quelle=%s, .env: %s)",
        ", ".join(f"{field}={_render_env_value(fields[field])}" for field in written),
        WRITE_SOURCE_UI,
        "; ".join(env_results),
    )
    return ConfigWriteResponse(
        written=written,
        effective_without_restart=effective,
        restarted_required=restarted,
    )


__all__ = [
    "API_TOKEN_FIELD",
    "BEARER_PREFIX",
    "CONFIG_WRITE_ENV_KEYS",
    "CONFIG_WRITE_FIELD_NAMES",
    "CONFIG_WRITE_FIELDS",
    "CONFIG_WRITE_LIVE_MAP",
    "GRAPH_SOURCE",
    "HISTORY_FIELDS",
    "HISTORY_REASON",
    "LOG_BUFFER_MAXLEN",
    "MAX_HISTORY_LIMIT",
    "MAX_LOGS_LIMIT",
    "MAX_WAKE_LIMIT",
    "REDACTED",
    "SERVICE_NAME",
    "SETTINGS_MISSING_ISSUE",
    "SETTINGS_SOURCE_APP_STATE",
    "SETTINGS_SOURCE_MODULE_DEFAULT",
    "STATE_SPECS",
    "THRESHOLD_SOURCE_DEVICE",
    "THRESHOLD_SOURCE_NONE",
    "THRESHOLD_SOURCE_SETTINGS",
    "TRANSITIONS_TRACKED",
    "WAKE_ATTEMPT_FIELDS",
    "WAKE_ATTEMPTS_MAXLEN",
    "ConfigResponse",
    "ConfigValue",
    "ConfigWriteResponse",
    "DependenciesResponse",
    "DependencyProbe",
    "DeviceStatus",
    "DeviceWorkflow",
    "HealthInfo",
    "HistoryEntry",
    "HistoryResponse",
    "LogEntry",
    "LogsResponse",
    "MemoryLogBuffer",
    "StateInfo",
    "StateResponse",
    "StateSpec",
    "StatusResponse",
    "WakeAttempt",
    "WakeDevice",
    "WakeResponse",
    "WakeStats",
    "attach_memory_log_buffer",
    "bearer_token_ok",
    "build_state_catalog",
    "clamp_limit",
    "collect_history",
    "collect_log_entries",
    "collect_wake",
    "detach_memory_log_buffer",
    "find_memory_records",
    "get_settings",
    "get_state_attr",
    "ha_health_dependency",
    "is_secret_field",
    "parse_levels",
    "parse_log_line",
    "parse_log_ts",
    "probe_url",
    "project_config",
    "read_config",
    "read_dependencies",
    "read_history",
    "read_logs",
    "read_state",
    "read_status",
    "redact",
    "record_to_entry",
    "resolve_log_dir",
    "router",
    "set_log_dir",
    "settings_source_for",
    "THRESHOLD_MAX",
    "THRESHOLD_MIN",
    "ThresholdWriteResponse",
    "WRITE_DISABLED_STATUS",
    "WRITE_ENABLED_FIELD",
    "WRITE_SOURCE_UI",
    "WRITE_UNAUTHORIZED_STATUS",
    "WRITABLE_SETTING",
    "write_config",
    "write_enabled",
    "write_env_value",
    "write_oww_threshold",
]
