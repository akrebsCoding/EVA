"""Home-Assistant-REST-Client (P4.T0, `PLAN.md:455`) – reine Client-Logik.

Auftrag (wörtlich aus `PLAN.md:455`): ``httpx.AsyncClient``,
``start()/stop()``-Lifecycle, Refresh-Loop (600 s), Entity-Cache mit
``asyncio.Lock``, ``call_service()`` (POST ``/api/services/{domain}/{service}``,
``entity_id`` **im Body**) und Domain-Filter aus Settings.

**Konstanten – belegt, nicht erfunden** (alle aus `app.config`, PLAN §4/§4-HA):

* ``ha_base_url`` ⇒ ``HA_BASE_URL`` (Default ``http://10.0.0.10:8123``).
* ``ha_request_timeout`` (5,0 s) ⇒ Gültigkeit für **jeden** HTTP-Aufruf
  (httpx-Client-Timeout, gilt für Connect/Read/Write/Pool).
* ``ha_cache_interval_seconds`` (600 s) ⇒ Schlafintervall des Refresh-Loops.
  **Nicht** hart 600 – der Wert kommt aus den Settings.
* ``entity_domains`` ⇒ **16** Domains (**E113**, P12.T2): ``light,switch,cover,
  climate,media_player,scene,script,button,number,select,input_boolean,
  input_select,input_number,fan,update,automation`` – nur Entities mit einer
  dieser Domains werden gecacht.  Live-Beleg 2026-09-30 (``.123:8123``,
  ``GET /api/states`` ∩ ``GET /api/services``, HA 2026.8.2): **209 → 375**
  Entities (von 931 gesamt; ``update`` 27 + ``automation`` 47 + ``button`` 45 +
  ``number`` 23 + ``select`` 13 + ``input_boolean`` 4 + ``input_select`` 3 +
  ``input_number`` 1 + ``fan`` 3 = **+166**).
* **Sicherheitsboden (E113)** ⇒ :data:`HA_BLOCKED_DOMAINS` = ``lock``,
  ``alarm_control_panel``, ``vacuum``: **immer** ausgeschlossen, **zuerst**
  geprüft, **kein** Env-Key (Block schlägt immer Erlaubnis) – siehe dort und
  ``P12_DESIGN.md`` A-2.
* ``allowed_entities`` (**E17**) ⇒ kommagetrennte statische Allowlist.
  Ist die Liste **leer**, gibt es **keinen** zusätzlichen Vorfilter (Default).
  Ist sie **nicht leer**, gilt sie als Allowlist: nur Entities, die zusätzlich
  in dieser Liste stehen, werden gecacht (Area-/Label-Vorfilter ist per
  HA-REST nicht umsetzbar, STATE §3/P0.T1). Domain-Filter **und** Allowlist
  wirken dabei **UND**-verknüpft (E17: Allowlist *ergänzend* zum Domain-Filter).
* **Read-only-Domains** (``sensor``, ``binary_sensor``, ``device_tracker``,
  ``person``, ``zone``, ``sun``, ``weather``, ``event``, ``image``,
  ``conversation``; live **425** Entities) sind **kein** Schaltziel und bleiben
  **draußen** – der Lese-Pfad für Sensorfragen ist P12.T3 (siehe unten).

**Fehlertext (E19).** Jeder Netzwerk-/HTTP-Fehler gegen HA – beim Cache-Refresh
*und* bei ``call_service`` – wird als ``HaUnavailableError`` geworfen, dessen
``str()`` **exakt** ``"Home Assistant antwortet nicht."`` lautet (der technische
Grund steht in ``.detail`` und in der ``__cause__``-Kette). Damit fangen
Router/Pipeline (P4.T4/P5) alle „Gerät antwortet nicht"-Fälle unter **einem**
festen Text ab – unabhängig davon, ob eine Domain (z. B. ``scene``/``media_player``,
überwiegend ``unavailable``) oder die ganze HA-Instanz nicht antwortet.

**Refresh-Loop.** ``start()`` führt **einmal** einen Refresh aus (damit ist der
Cache unmittelbar nach ``await start()`` befüllt) und startet dann den Loop, der
alle ``ha_cache_interval_seconds`` erneut ``GET /api/states`` holt. Fehler im
Loop werden **geloggt und geschluckt** – der Loop läuft weiter (kein Task-Tod).
``stop()`` bricht den Loop **sauber** ab (``cancel()`` + ``await``) und schließt
den ``httpx.AsyncClient``; ``start()``/``stop()`` sind **idempotent** und
mehrfach hintereinander aufrufbar.

**Lock-Semantik.** Der Entity-Cache kapselt ein ``asyncio.Lock``; **alle**
Lese- und Schreibzugriffe (``get``/``snapshot``/``ids``/``size``/``counts``/
``replace``/``clear``) laufen darüber. Der Cache ist damit für konkurrierende
Tasks (Refresh-Loop ↔ Router-Anfragen) konsistent lesbar.

**Reine Client-Logik:** Dieses Modul importiert **nichts** aus ``app.pipeline``
oder ``app.router``; nur stdlib, ``httpx``, ``app.config`` und ``app.logger``.

**Lese-Pfad für Sensor-/Zustandsfragen (P12.T3, ``P12_DESIGN.md`` Block C).**
Der Entity-Cache beantwortet nur „welches Gerät kann ich schalten".  Fragen wie
„wie warm ist es im Wohnzimmer?" brauchen **Zustände** – und die kamen bis hier
**nicht** aus Home Assistant, sodass das LLM aus dem Weltwissen riet.  Dafür
gibt es jetzt einen zweiten, schlanken Cache :class:`ReadableCache`:

* **Kein zusätzlicher HTTP-Call (C-1).**  :meth:`HomeAssistantClient._refresh`
  füllt **beide** Caches aus **derselben** ``/api/states``-Antwort: ein
  Request, zwei einsortierte Sichten.  ``/api/states`` liefert live **931**
  States, davon **425** read-only – die sortiert ``EntityCache.replace()`` bisher
  weg.
* **Was lesbar ist (C-1):** :data:`READABLE_DOMAINS` = ``sensor``,
  ``binary_sensor``, ``weather``, ``sun``, ``person``.  ``device_tracker``
  (live **85** MAC-/Gerätenamen) bleibt **bewusst draußen** – Präsenz läuft über
  ``person.*`` (live 5).  ``sensor``/``binary_sensor`` kommen nur mit einem
  ``device_class`` aus der jeweiligen Whitelist
  (:data:`READABLE_SENSOR_DEVICE_CLASSES` /
  :data:`READABLE_BINARY_DEVICE_CLASSES`), damit 344 Sensoren nicht den Prompt
  fluten.
* **Harte Frische (C-1/C-2):** ``state ∈ {unknown, unavailable}``, ein Wert
  **älter als 24 h** (:data:`READABLE_MAX_AGE_SECONDS`) und ein fehlendes/
  unlesbares ``last_updated`` ⇒ **nicht** im Cache.  Live waren 15 Werte
  ``>24 h`` alt (zwei davon ~19 Tage, weil der State-Wert unverändert blieb) –
  genau solche Werte dürfen nicht als „aktuell" behauptet werden.
* **Wert + Zeitbezug (C-3):** :func:`readable_value` formuliert den Wert
  deutsch und TTS-tauglich (Dezimalkomma, Einheit, Wetterlage, Anwesenheit,
  Sonnenstand), :func:`readable_timestamp` liefert den ``Stand HH:MM`` in der
  von HA gemeldeten Zeitzone.  Ein Wert **ohne** Zeitbezug ist genau das, was C
  vermeiden will – deshalb ist ein fehlender Zeitstempel kein Default, sondern
  ein Ausschlussgrund.

**Nicht** hier: Tests (`tests/test_ha_client.py`, P4.T1), LLM-Client (P4.T2) und
Router (P4.T4).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional, Sequence

import httpx

from app.config import settings
from app.logger import get_logger

__all__ = [
    "HaClientError",
    "HaUnavailableError",
    "EntityCache",
    "ReadableCache",
    "HomeAssistantClient",
    "entity_domain",
    "is_target_entity",
    "is_readable_entity",
    "readable_value",
    "readable_timestamp",
    "readable_age_seconds",
    "HA_BASE_URL",
    "HA_REQUEST_TIMEOUT",
    "HA_CACHE_INTERVAL_SECONDS",
    "HA_ENTITY_DOMAINS",
    "HA_ALLOWED_ENTITIES",
    "HA_BLOCKED_DOMAINS",
    "HA_UNAVAILABLE_MESSAGE",
    "HA_STATES_PATH",
    "HA_SERVICE_PATH_TEMPLATE",
    "READABLE_DOMAINS",
    "READABLE_SENSOR_DEVICE_CLASSES",
    "READABLE_BINARY_DEVICE_CLASSES",
    "READABLE_MAX_AGE_SECONDS",
    "READABLE_DEAD_STATES",
]

_LOG: Final = get_logger("ha_client")

# ── Verbindungs-/Cache-Parameter (alle aus `app.config`, PLAN §4) ────────────
#: Basis-URL der HA-Instanz (Default `.123:8123`).
HA_BASE_URL: Final[str] = settings.ha_base_url
#: HTTP-Timeout je Aufruf (httpx-Client-Timeout).
HA_REQUEST_TIMEOUT: Final[float] = settings.ha_request_timeout
#: Schlafintervall des Refresh-Loops – aus Settings, **nicht** hart 600.
HA_CACHE_INTERVAL_SECONDS: Final[int] = settings.ha_cache_interval_seconds
#: Ziel-Domains des Entity-Caches (E113: 16 Domains).
HA_ENTITY_DOMAINS: Final[tuple[str, ...]] = settings.entity_domains
#: Statische Allowlist (E17); leer = kein Vorfilter.
HA_ALLOWED_ENTITIES: Final[tuple[str, ...]] = settings.allowed_entities

#: **E113 – Sicherheitsboden, hart im Code und bewusst KEIN Env-Key.**
#:
#: Diese Domains werden **immer** aus dem Entity-Cache ausgeschlossen –
#: **unabhängig** von ``ha_entity_domains`` und ``ha_allowed_entities``.
#: Steht eine dieser Domains (oder eine Entity daraus) trotzdem in der `.env` /
#: in ``HA_ALLOWED_ENTITIES``, gewinnt **der Block**: *Block schlägt immer
#: Erlaubnis* (fail-safe, kein „letzter Gewinn").
#:
#: Warum kein Env-Key (E113, `deploy/docs/P12_DESIGN.md` A-2): ein
#: Konfigurationsschlüssel, der die Sicherheitsgrenze aushebeln kann, ist
#: schlechter als ein Hardcode – der Fehler beim Deploy bleibt so auffällig
#: (Diff/Wizard) statt still zu wirken.
#:
#: * ``lock`` – ein Sprach-Fehlgriff dürfte die Haustür **öffnen** (in dieser
#:   HA-Instanz aktuell 0 Entities, die *Services* existieren aber).
#: * ``alarm_control_panel`` – live mit **2** Entities belegt (Scharfschalt-/
#:   Sensorik-Pfade); ein Fehl-Trigger schaltet die Anlage scharf/unscharf.
#: * ``vacuum`` – 9 Services (u. a. ``start``, ``return_to_base``); ein
#:   Fehl-Trigger lähmt das Gerät.
#:
#: **Read-only-Domains** (``sensor``, ``binary_sensor``, ``device_tracker``,
#: ``person``, ``zone``, ``sun``, ``weather``, ``event``, ``image``,
#: ``conversation``) stehen **nicht** hier – sie werden ganz normal vom
#: Domain-Filter ausgeschlossen und sind **kein** Schaltziel.  Für Fragen gibt
#: es seit P12.T3 den separaten :class:`ReadableCache`
#: (:data:`READABLE_DOMAINS`); er ändert **nichts** daran, was schaltbar ist.
HA_BLOCKED_DOMAINS: Final[tuple[str, ...]] = (
    "lock",
    "alarm_control_panel",
    "vacuum",
)

# ── Lese-Pfad (P12.T3, `P12_DESIGN.md` C-1) ───────────────────────────────
#: **Read-only**-Domains, aus denen Zustände in Frage-Prompts wandern dürfen.
#: ``device_tracker`` steht **bewusst nicht** darin: live 85 MAC-/Gerätenamen
#: („HandySamsung", „WLAN-Fritz") – für Präsenz gibt es ``person.*`` (live 5).
READABLE_DOMAINS: Final[tuple[str, ...]] = (
    "sensor",
    "binary_sensor",
    "weather",
    "sun",
    "person",
)
#: ``device_class``-Whitelist für ``sensor`` (HA-Standardklassen).  Live: 344
#: Sensoren, davon 267 brauchbar, **118** mit einer Klasse aus diesem Satz.  Ein
#: Sensor **ohne** ``device_class`` fällt durch – ein Systemsensor ohne Aussage
#: über die Art des Werts gehört nicht in einen Antwort-Prompt.
#: ``timestamp`` fehlt **bewusst**: der Roh-Unixzeit-Wert ist nicht vorlesbar.
READABLE_SENSOR_DEVICE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "aqi", "atmospheric_pressure", "atmospheric_pressure_altitude", "battery",
        "carbon_dioxide", "carbon_monoxide", "conductivity", "current", "distance",
        "energy", "energy_storage", "gas", "humidity", "illuminance", "moisture",
        "mould", "ph", "pm1", "pm10", "pm25", "power", "power_factor",
        "precipitation", "precipitation_intensity", "pressure", "signal_strength",
        "sound_pressure", "speed", "temperature", "visibility",
        "volatile_organic_compounds", "volatile_organic_compounds_parts",
        "voltage", "volume", "water", "weight", "wind_direction", "wind_gust",
        "wind_speed", "uv_index",
    }
)
#: ``device_class``-Whitelist für ``binary_sensor`` (live 74) – nur Zustände, die
#: eine Frage beantworten („ist das Fenster offen?").  Verbindungsmeldungen und
#: Batterie-Flag einzelner Geräte bleiben draußen, sonst gewinnt die Geräteflut.
READABLE_BINARY_DEVICE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "battery", "carbon_monoxide", "cold", "connectivity", "door", "garage_door",
        "gas", "heat", "light", "lock", "moisture", "motion", "occupancy",
        "opening", "presence", "problem", "safety", "smoke", "sound", "tamper",
        "vibration", "water", "window",
    }
)
#: **Harte** Frische-Grenze: Werte, die älter sind, werden nicht behauptet
#: (Live-Beleg T1: 15 Werte ``>24 h``, zwei davon ~19 Tage).  Die Grenze ist eine
#: Modulkonstante, **kein** Env-Key – ein Wert ohne Zeitbezug ist keine
#: Konfigurationsfrage, sondern eine Zusage.
READABLE_MAX_AGE_SECONDS: Final[float] = 24.0 * 3600.0
#: Zustandswerte ohne Aussage – nie in einen Antwort-Prompt.
READABLE_DEAD_STATES: Final[frozenset[str]] = frozenset({"unknown", "unavailable"})

#: Deutsche Umschreibungen für Zustandswerte (TTS-tauglich, ohne Sonderzeichen).
_READABLE_BINARY_TEXT: Final[Mapping[str, str]] = {
    "on": "an",
    "off": "aus",
    "open": "offen",
    "closed": "geschlossen",
    "locked": "verriegelt",
    "unlocked": "entriegelt",
    "detected": "erkannt",
    "clear": "unauffällig",
}
_READABLE_PERSON_TEXT: Final[Mapping[str, str]] = {
    "home": "anwesend",
    "not_home": "abwesend",
}
_READABLE_SUN_TEXT: Final[Mapping[str, str]] = {
    "above_horizon": "über dem Horizont",
    "below_horizon": "unter dem Horizont",
}
_READABLE_WEATHER_TEXT: Final[Mapping[str, str]] = {
    "clear-night": "klare Nacht",
    "cloudy": "bewölkt",
    "exceptional": "außergewöhnlich",
    "fog": "neblig",
    "hail": "Hagel",
    "lightning": "Gewitter",
    "lightning-rainy": "Gewitter mit Regen",
    "partlycloudy": "teilweise bewölkt",
    "pouring": "starker Regen",
    "rainy": "regnerisch",
    "snowy": "verschneit",
    "snowy-rainy": "Schneeregen",
    "sunny": "klar",
    "windy": "windig",
    "windy-variant": "windig, wechselnd",
}

#: Fester Fehlertext für alle HA-Ausfälle (E19) – **wörtlich**.
HA_UNAVAILABLE_MESSAGE: Final[str] = "Home Assistant antwortet nicht."
#: REST-Pfad der Entity-States.
HA_STATES_PATH: Final[str] = "/api/states"
#: REST-Pfad eines Service-Aufrufs (Domain/Service werden eingesetzt).
HA_SERVICE_PATH_TEMPLATE: Final[str] = "/api/services/{domain}/{service}"


# ── Fehler ────────────────────────────────────────────────────────────────
class HaClientError(Exception):
    """Basisklasse aller HA-Client-Fehler (auch Programmierfehler)."""


class HaUnavailableError(HaClientError):
    """HA nicht erreichbar oder hat mit Fehler geantwortet (E19).

    ``str(exc)`` ist **immer** ``"Home Assistant antwortet nicht."``; der
    technische Grund steht in ``detail`` (und als ``__cause__``).
    """

    def __init__(self, detail: Optional[str] = None) -> None:
        super().__init__(HA_UNAVAILABLE_MESSAGE)
        self.detail = detail


# ── Filter-Helfer ─────────────────────────────────────────────────────────
def entity_domain(entity_id: str) -> str:
    """Domain eines ``entity_id`` (``"light.wohnzimmer"`` ⇒ ``"light"``)."""
    if not isinstance(entity_id, str) or not entity_id:
        raise HaClientError(
            f"entity_id muss ein nicht-leerer str sein, ist {entity_id!r}"
        )
    return entity_id.split(".", 1)[0]


def is_target_entity(
    entity_id: str,
    domains: Sequence[str],
    allowed_entities: Sequence[str] = (),
    *,
    blocked_domains: Optional[Sequence[str]] = None,
) -> bool:
    """True, wenn ``entity_id`` gecacht werden darf (Block → Domain → E17).

    Reihenfolge **ist der Vertrag** (E113, `P12_DESIGN.md` A-2):

    1. **Block zuerst** – liegt die Domain in ``blocked_domains``, ist die
       Entity **immer** draußen, egal was ``domains``/``allowed_entities``
       sagen.  *Block schlägt immer Erlaubnis.*
    2. **Domain-Filter** – die Domain muss in ``domains`` liegen.
    3. **E17-Allowlist** – ist ``allowed_entities`` **nicht leer**, muss die
       Entity **zusätzlich** darin stehen.  Ist sie leer, gibt es **keinen**
       Vorfilter.

    ``blocked_domains=None`` (Default) ⇒ :data:`HA_BLOCKED_DOMAINS` – der
    Sicherheitsboden des Moduls.  Der Parameter existiert, damit **Tests** die
    alte Semantik und leere Blocklisten prüfen können; im Betrieb wird er
    **nie** gesetzt (kein Env-Key, s. :data:`HA_BLOCKED_DOMAINS`).
    """
    blocked = HA_BLOCKED_DOMAINS if blocked_domains is None else tuple(blocked_domains)
    if entity_domain(entity_id) in blocked:
        return False
    if entity_domain(entity_id) not in domains:
        return False
    if allowed_entities and entity_id not in allowed_entities:
        return False
    return True


# ── Lese-Helfer (P12.T3, `P12_DESIGN.md` C-1/C-3) ─────────────────────────
def _state_attributes(state: Any) -> Mapping[str, Any]:
    """``attributes`` eines States als Mapping (leer, wenn kaputt)."""
    if not isinstance(state, Mapping):
        return {}
    attributes = state.get("attributes")
    return attributes if isinstance(attributes, Mapping) else {}


def _format_readable_number(value: Any) -> str:
    """Zahl **deutsch** für TTS/Prompt: Dezimalkomma, höchstens 1 Nachkommastelle.

    ``22.4`` ⇒ ``"22,4"``, ``21.0`` ⇒ ``"21"``.  Nicht-numerische Werte werden
    **unverändert** als Text durchgereicht (z. B. ``"1.234 kWh"``).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(round(number)))
    return f"{number:.1f}".replace(".", ",")


def _parse_updated(state: Any) -> Optional[datetime]:
    """``last_updated`` als timezone-aware ``datetime`` – sonst ``None``.

    HA liefert ISO-8601 **mit** Offset (``2026-09-30T14:20:31+02:00``); ein
    fehlender Offset wird als UTC gelesen.  Bewusst tolerant, aber **nicht**
    errät: ein unlesbares Feld liefert ``None`` und die Entity wird damit
    **nicht** als lesbar geführt.
    """
    if not isinstance(state, Mapping):
        return None
    raw = state.get("last_updated")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def readable_age_seconds(
    state: Any, *, now: Optional[datetime] = None
) -> Optional[float]:
    """Alter des States in Sekunden – ``None``, wenn kein Zeitstempel lesbar ist.

    ``now`` ist **injizierbar** (Tests bleiben deterministisch).  Ein Zeitstempel
    aus der Zukunft (Uhrendelta) zählt als **0 s**, nicht als negativ.
    """
    parsed = _parse_updated(state)
    if parsed is None:
        return None
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - parsed.astimezone(timezone.utc)).total_seconds())


def readable_timestamp(state: Any) -> Optional[str]:
    """``Stand HH:MM`` in der **von HA gemeldeten** Zeitzone (kein UTC-Umrechnen).

    Der Offset steht in ``last_updated`` selbst; ihn umzurechnen würde die im
    Raum gesprochene Zeit verbiegen (Berlin 14:20 → 12:20).  ``None``, wenn der
    Zeitstempel fehlt oder unlesbar ist.
    """
    parsed = _parse_updated(state)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None).strftime("%H:%M")


def readable_value(state: Any, *, entity_id: Optional[str] = None) -> str:
    """Wert eines States als **deutscher, TTS-tauglicher** Text (``""`` = keiner).

    * ``sensor`` ⇒ Zustand + ``unit_of_measurement`` (``"22,4 °C"``)
    * ``weather`` ⇒ Lage + Temperatur/Feuchte/Druck/Wind
      (``"bewölkt, 24,3 °C, 46 %, 1019,5 hPa, 10,4 m/s"``)
    * ``sun`` ⇒ Sonnenstand + Sonnenaufgang/-untergang
    * ``person`` ⇒ Anwesenheit
    * ``binary_sensor`` ⇒ ``on/off`` ⇒ ``an/aus`` usw.
    * sonst ⇒ der Rohzustand als Text

    ``entity_id`` darf **extern** übergeben werden (``app/router.py`` macht das):
    die Formatierung braucht den **Domain**, und ein Cache-Eintrag trägt sie
    nicht zwingend selbst (der :class:`EntityCache` legt nur ``state`` +
    ``attributes`` ab).  Ohne auffindbare ``entity_id`` ist der Domain nicht
    bestimmbar ⇒ ``""`` statt einer geratenen Darstellung.
    """
    if not isinstance(state, Mapping):
        return ""
    if entity_id is None:
        entity_id = state.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        return ""
    raw = state.get("state")
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    domain = entity_domain(entity_id)
    attributes = _state_attributes(state)

    if domain == "sensor":
        unit = attributes.get("unit_of_measurement")
        if isinstance(unit, str) and unit.strip():
            return f"{_format_readable_number(text)} {unit.strip()}"
        return _format_readable_number(text)

    if domain == "weather":
        parts = [_READABLE_WEATHER_TEXT.get(text.lower(), text)]
        for key, unit in (
            ("temperature", "°C"),
            ("humidity", "%"),
            ("pressure", "hPa"),
            ("wind_speed", "m/s"),
        ):
            value = attributes.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parts.append(f"{_format_readable_number(value)} {unit}")
        return ", ".join(parts)

    if domain == "sun":
        parts = [_READABLE_SUN_TEXT.get(text.lower(), text)]
        for key in ("sunrise", "sunset"):
            stamp = _parse_updated(
                {
                    "entity_id": "sun.probe",
                    "last_updated": attributes.get(key),
                }
            )
            if stamp is not None:
                label = "Sonnenaufgang" if key == "sunrise" else "Sonnenuntergang"
                parts.append(f"{label} {stamp.strftime('%H:%M')}")
        return ", ".join(parts)

    if domain == "person":
        return _READABLE_PERSON_TEXT.get(text.lower(), text)

    if domain == "binary_sensor":
        return _READABLE_BINARY_TEXT.get(text.lower(), text)

    return text


def is_readable_entity(
    state: Any,
    *,
    domains: Sequence[str] = READABLE_DOMAINS,
    max_age_seconds: float = READABLE_MAX_AGE_SECONDS,
    now: Optional[datetime] = None,
) -> bool:
    """True, wenn der State als **lesbarer Wert** in einen Frage-Prompt darf.

    Die Kette ist der Vertrag (C-1/C-2) und wird Schritt für Schritt geprüft:

    1. **Form** – Mapping mit nicht-leerer ``entity_id`` und skalarer ``state``.
    2. **Domain** – in :data:`READABLE_DOMAINS` (``device_tracker`` nie).
    3. **Aussage** – ``state`` nicht in :data:`READABLE_DEAD_STATES`.
    4. **Semantik** – ``sensor``/``binary_sensor`` brauchen einen
       ``device_class`` aus der jeweiligen Whitelist; ``weather``/``sun``/
       ``person`` nicht (HA liefert dort keinen).
    5. **Zeitbezug** – ``last_updated`` lesbar **und** nicht älter als
       ``max_age_seconds`` (fehlender Zeitstempel ⇒ **kein** Wert: ein Wert ohne
       „Stand" wäre die stille Falschaussage, die C verhindern soll).
    """
    if not isinstance(state, Mapping):
        return False
    entity_id = state.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        return False
    raw = state.get("state")
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return False
    if str(raw).strip().lower() in READABLE_DEAD_STATES:
        return False
    domain = entity_domain(entity_id)
    if domain not in tuple(domains):
        return False
    attributes = _state_attributes(state)
    if domain == "sensor":
        if attributes.get("device_class") not in READABLE_SENSOR_DEVICE_CLASSES:
            return False
    elif domain == "binary_sensor":
        if attributes.get("device_class") not in READABLE_BINARY_DEVICE_CLASSES:
            return False
    if not readable_value(state):
        return False
    age = readable_age_seconds(state, now=now)
    if age is None or age > max_age_seconds:
        return False
    return True


# ── Readable-Cache (P12.T3, gleiche Lock-Semantik wie `EntityCache`) ──────
class ReadableCache:
    """Task-sicherer Cache der **lesbaren Zustände** (``entity_id`` → State).

    Zweiter, schlanker Cache neben :class:`EntityCache` – **gleiches** Muster
    (``asyncio.Lock``, atomares :meth:`replace`, Kopien nach außen), andere
    Auswahl: nur :data:`READABLE_DOMAINS`, ``device_class``-Whitelist und
    harte Frische (C-1).  Gefüllt wird er aus **derselben**
    ``/api/states``-Antwort (kein zweiter HTTP-Call).

    Vor dem ersten :meth:`replace` ist er **leer** – :meth:`snapshot` liefert
    dann ``{}`` (kein Absturz, siehe C-12).
    """

    def __init__(
        self,
        *,
        domains: Optional[Sequence[str]] = None,
        max_age_seconds: float = READABLE_MAX_AGE_SECONDS,
    ) -> None:
        self.domains: tuple[str, ...] = (
            tuple(domains) if domains is not None else READABLE_DOMAINS
        )
        self.max_age_seconds: float = float(max_age_seconds)
        self._lock: Final[asyncio.Lock] = asyncio.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self._last_total: int = 0

    @property
    def lock(self) -> asyncio.Lock:
        """Das schützende Lock (für Diagnose/Tests)."""
        return self._lock

    async def replace(
        self, states: Sequence[Any], *, now: Optional[datetime] = None
    ) -> int:
        """Cache aus **derselben** ``/api/states``-Antwort neu aufbauen.

        ``now`` ist injizierbar (deterministische Tests).  Liefert die Zahl der
        lesbaren Werte; ``counts()`` hält zusätzlich die Zahl der **gesehenen**
        gültigen States fest (Diagnose, wie beim :class:`EntityCache`).
        """
        selected: dict[str, dict[str, Any]] = {}
        total = 0
        for state in states:
            if not isinstance(state, Mapping):
                continue
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            total += 1
            if is_readable_entity(
                state,
                domains=self.domains,
                max_age_seconds=self.max_age_seconds,
                now=now,
            ):
                selected[entity_id] = dict(state)
        async with self._lock:
            self._states = selected
            self._last_total = total
        return len(selected)

    async def get(self, entity_id: str) -> Optional[dict[str, Any]]:
        """State einer Entity als Kopie oder ``None`` (unter dem Lock)."""
        async with self._lock:
            state = self._states.get(entity_id)
            return dict(state) if state is not None else None

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        """Flache Kopie des gesamten Lese-Caches (unter dem Lock)."""
        async with self._lock:
            return {key: dict(value) for key, value in self._states.items()}

    async def ids(self) -> tuple[str, ...]:
        """Alle lesbaren ``entity_id`` (unter dem Lock)."""
        async with self._lock:
            return tuple(self._states)

    async def size(self) -> int:
        """Anzahl lesbarer Werte (unter dem Lock)."""
        async with self._lock:
            return len(self._states)

    async def counts(self) -> tuple[int, int]:
        """``(gesehene States, lesbare Werte)`` (unter dem Lock)."""
        async with self._lock:
            return self._last_total, len(self._states)

    async def clear(self) -> None:
        """Cache leeren (unter dem Lock)."""
        async with self._lock:
            self._states = {}
            self._last_total = 0


# ── Entity-Cache (asyncio.Lock-geschützt) ─────────────────────────────────
class EntityCache:
    """Task-sicherer Cache der gefilterten HA-Entities (``entity_id`` → State).

    Sämtliche Zugriffe laufen über ein ``asyncio.Lock``.  ``replace`` ersetzt
    den Bestand atomar (Refresh-Loop), reads liefern **Kopien**, damit Aufrufer
    den Cache nicht von außen mutieren können.
    """

    def __init__(
        self,
        *,
        domains: Optional[Sequence[str]] = None,
        allowed_entities: Optional[Sequence[str]] = None,
        blocked_domains: Optional[Sequence[str]] = None,
    ) -> None:
        self.domains: tuple[str, ...] = (
            tuple(domains) if domains is not None else HA_ENTITY_DOMAINS
        )
        self.allowed_entities: tuple[str, ...] = (
            tuple(allowed_entities)
            if allowed_entities is not None
            else HA_ALLOWED_ENTITIES
        )
        #: Sicherheitsboden (E113) – Default :data:`HA_BLOCKED_DOMAINS`, **nicht**
        #: aus Settings.  ``[]``/``()`` ist ausschließlich für Tests erlaubt.
        self.blocked_domains: tuple[str, ...] = (
            tuple(blocked_domains)
            if blocked_domains is not None
            else HA_BLOCKED_DOMAINS
        )
        self._lock: Final[asyncio.Lock] = asyncio.Lock()
        self._entities: dict[str, dict[str, Any]] = {}
        self._last_total: int = 0

    @property
    def lock(self) -> asyncio.Lock:
        """Das schützende Lock (für Diagnose/Tests)."""
        return self._lock

    async def replace(self, states: Sequence[Any]) -> int:
        """Cache aus einer ``/api/states``-Antwort neu aufbauen.

        Filtert nach Blockliste (E113, **zuerst**), Domain (und ggf. Allowlist),
        ersetzt den Bestand atomar unter dem Lock und liefert die Zahl der
        gecachten Entities.  Die Gesamtzahl der gesehenen (gültigen) States wird
        als ``_last_total`` festgehalten (Diagnose, ``counts()``).
        """
        selected: dict[str, dict[str, Any]] = {}
        total = 0
        for state in states:
            if not isinstance(state, Mapping):
                continue
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            total += 1
            if is_target_entity(
                entity_id,
                self.domains,
                self.allowed_entities,
                blocked_domains=self.blocked_domains,
            ):
                selected[entity_id] = dict(state)
        async with self._lock:
            self._entities = selected
            self._last_total = total
        return len(selected)

    async def get(self, entity_id: str) -> Optional[dict[str, Any]]:
        """State einer Entity als Kopie oder ``None`` (unter dem Lock)."""
        async with self._lock:
            state = self._entities.get(entity_id)
            return dict(state) if state is not None else None

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        """Flache Kopie des gesamten Caches (unter dem Lock)."""
        async with self._lock:
            return {key: dict(value) for key, value in self._entities.items()}

    async def ids(self) -> tuple[str, ...]:
        """Alle gecachten ``entity_id`` (unter dem Lock)."""
        async with self._lock:
            return tuple(self._entities)

    async def size(self) -> int:
        """Anzahl gecachter Entities (unter dem Lock)."""
        async with self._lock:
            return len(self._entities)

    async def counts(self) -> tuple[int, int]:
        """``(gesehene States, gecachte Entities)`` (unter dem Lock)."""
        async with self._lock:
            return self._last_total, len(self._entities)

    async def clear(self) -> None:
        """Cache leeren (unter dem Lock)."""
        async with self._lock:
            self._entities = {}
            self._last_total = 0


# ── Home-Assistant-Client ─────────────────────────────────────────────────
class HomeAssistantClient:
    """Asynchroner HA-REST-Client mit Cache, Refresh-Loop und Service-Aufruf.

    ``start()`` baut (falls nötig) den ``httpx.AsyncClient`` auf, befüllt den
    Cache einmalig über ``GET /api/states`` und startet den Refresh-Loop.
    ``stop()`` bricht den Loop ab und schließt den Client.  Beide sind
    **idempotent**.
    """

    def __init__(
        self,
        *,
        base_url: str = HA_BASE_URL,
        token: Optional[str] = None,
        request_timeout: float = HA_REQUEST_TIMEOUT,
        cache_interval: float = HA_CACHE_INTERVAL_SECONDS,
        domains: Optional[Sequence[str]] = None,
        allowed_entities: Optional[Sequence[str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        #: Token wird **nie** geloggt/ausgegeben; Default aus `.env` (E41).
        self._token: str = token if token is not None else settings.ha_token
        self.request_timeout: float = float(request_timeout)
        #: Schlafintervall des Loops – Wert kommt aus den Settings.
        self.cache_interval: float = float(cache_interval)
        self.cache: Final[EntityCache] = EntityCache(
            domains=domains, allowed_entities=allowed_entities
        )
        #: **Zweiter** Cache für lesbare Zustände (P12.T3/C-1).  Er wird aus
        #: **derselben** ``/api/states``-Antwort gefüllt – die zusätzliche
        #: Auswahl kostet **keinen** zusätzlichen HTTP-Call.  Bewusst **nicht**
        #: vom ``domains``-Parameter abhängig: der Lese-Pfad ist unabhängig von
        #: der Schaltbarkeit (ein ``sensor`` ist nie ein Schaltziel).
        self.readable_cache: Final[ReadableCache] = ReadableCache()
        self._provided_client: Optional[httpx.AsyncClient] = client
        self._client: Optional[httpx.AsyncClient] = client
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._started: bool = False

    # ── Zustand ────────────────────────────────────────────────────────
    @property
    def started(self) -> bool:
        """True zwischen ``start()`` und ``stop()``."""
        return self._started

    @property
    def client(self) -> Optional[httpx.AsyncClient]:
        """Der aktive ``httpx.AsyncClient`` (oder ``None``)."""
        return self._client

    @property
    def loop_task(self) -> Optional[asyncio.Task[None]]:
        """Der laufende Refresh-Task (oder ``None``)."""
        return self._loop_task

    # ── Lebenszyklus (idempotent) ──────────────────────────────────────
    async def start(self) -> None:
        """Client aufbauen, Cache einmalig füllen, Refresh-Loop starten.

        Idempotent: ein zweiter Aufruf ist ein No-op.
        """
        if self._started:
            return
        if self._client is None or self._client.is_closed:
            if self._provided_client is not None and not self._provided_client.is_closed:
                self._client = self._provided_client
            else:
                self._client = self._build_client()
        # Bearer-Auth auch für injizierte Clients garantieren (idempotent).
        self._client.headers["Authorization"] = f"Bearer {self._token}"
        self._started = True
        # Erstbefüllung: Fehler dürfen den Start nicht verhindern; der Loop
        # versucht es später erneut (Ausfall der HA-Instanz beim Start).
        await self._safe_refresh()
        self._loop_task = asyncio.create_task(
            self._refresh_loop(), name="ha-refresh-loop"
        )
        cached = await self.cache.size()
        readable = await self.readable_cache.size()
        _LOG.info(
            "HA-Client gestartet (base=%s, Intervall=%.0fs, Cache=%d Entities, "
            "Domains=%d, blockiert=%d, lesbar=%d)",
            self.base_url,
            self.cache_interval,
            cached,
            len(self.cache.domains),
            len(self.cache.blocked_domains),
            readable,
        )

    async def stop(self) -> None:
        """Refresh-Loop abbrechen (cancel + await) und Client schließen.

        Idempotent und mehrfach aufrufbar.
        """
        task = self._loop_task
        self._loop_task = None
        self._started = False
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - Loop darf hier nie hochbluten.
                _LOG.warning("HA-Refresh-Loop endete mit Fehler: %r", exc)
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - Close ist Best-effort.
                _LOG.warning("HA-AsyncClient konnte nicht geschlossen werden: %r", exc)
        _LOG.info("HA-Client gestoppt")

    async def __aenter__(self) -> HomeAssistantClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ── Refresh ────────────────────────────────────────────────────────
    async def refresh(self) -> int:
        """``GET /api/states`` holen und den Cache neu aufbauen.

        Bei jedem Netzwerk-/HTTP-Fehler ⇒ ``HaUnavailableError``
        („Home Assistant antwortet nicht.").
        """
        return await self._refresh()

    async def _safe_refresh(self) -> None:
        """Refresh ohne Propagieren (für ``start()``) – Fehler werden geloggt."""
        try:
            await self._refresh()
        except Exception as exc:  # noqa: BLE001 - Start darf nicht crashen.
            _LOG.warning("Erster HA-Refresh fehlgeschlagen: %s", exc)

    async def _refresh(self) -> int:
        client = self._client
        if client is None:
            raise HaUnavailableError("Client nicht gestartet")
        try:
            response = await client.get(HA_STATES_PATH)
        except httpx.TimeoutException as exc:
            raise HaUnavailableError(
                f"Timeout nach {self.request_timeout:g}s auf {HA_STATES_PATH}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HaUnavailableError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise HaUnavailableError(
                f"HTTP {response.status_code} auf {HA_STATES_PATH}"
            )
        try:
            states = response.json()
        except ValueError as exc:
            raise HaUnavailableError("Antwort ist kein gültiges JSON") from exc
        if not isinstance(states, list):
            raise HaUnavailableError("Antwort ist keine JSON-Liste")
        # **Ein** Request, **beide** Caches: erst die Schalt-Entities, dann –
        # aus derselben Liste – die lesbaren Zustände (P12.T3/C-1).  Bewusst
        # keine zweite HTTP-Anfrage: `GET /api/states` liefert beides.
        count = await self.cache.replace(states)
        readable = await self.readable_cache.replace(states)
        _LOG.debug(
            "HA-Caches aufgebaut: %d Entities, %d lesbare Werte", count, readable
        )
        return count

    async def _refresh_loop(self) -> None:
        """Periodischer Refresh; Fehler werden geloggt, der Loop läuft weiter."""
        while True:
            try:
                await asyncio.sleep(self.cache_interval)
                count = await self._refresh()
                _LOG.debug("HA-Cache aktualisiert: %d Entities", count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Loop darf nie sterben.
                _LOG.warning(
                    "HA-Refresh fehlgeschlagen (%s); Loop läuft weiter", exc
                )

    # ── Service-Aufruf ─────────────────────────────────────────────────
    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        **data: Any,
    ) -> Any:
        """Service aufrufen: ``POST /api/services/{domain}/{service}``.

        ``entity_id`` wird **im Body** mitgesendet (nicht in der URL); weitere
        ``data``-Felder werden in denselben Body gemergt (überschreiben
        ``entity_id`` **nicht**).  Fehler ⇒ ``HaUnavailableError``.
        """
        for name, value in (
            ("domain", domain),
            ("service", service),
            ("entity_id", entity_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise HaClientError(f"{name} muss ein nicht-leerer str sein")

        client = self._client
        if client is None:
            raise HaUnavailableError("Client nicht gestartet")

        payload: dict[str, Any] = {"entity_id": entity_id}
        payload.update(data)
        path = HA_SERVICE_PATH_TEMPLATE.format(domain=domain, service=service)

        try:
            response = await client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise HaUnavailableError(
                f"Timeout nach {self.request_timeout:g}s auf {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HaUnavailableError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise HaUnavailableError(f"HTTP {response.status_code} auf {path}")

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ── Cache-Komfort ──────────────────────────────────────────────────
    async def get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Gecachten State einer Entity (Kopie) oder ``None``."""
        return await self.cache.get(entity_id)

    async def cached_entities(self) -> dict[str, dict[str, Any]]:
        """Kopie des gesamten Entity-Caches."""
        return await self.cache.snapshot()

    async def readable_snapshot(self) -> dict[str, dict[str, Any]]:
        """Kopie des **Lese**-Caches (P12.T3) – Quelle für Frage-Prompts.

        Liest **ausschließlich** den Cache (kein Refresh, kein HTTP): der
        Entity-Cache zeigt denselben Refresh-Zyklus, und ein zweiter Request pro
        Frage wäre genau die Latenz, die C nicht kosten darf.
        """
        return await self.readable_cache.snapshot()

    # ── Intern ─────────────────────────────────────────────────────────
    def _build_client(self) -> httpx.AsyncClient:
        """``httpx.AsyncClient`` mit Bearer-Auth und Request-Timeout bauen.

        Der Token wird ausschließlich im Header gesetzt – **nie** geloggt.
        """
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self.request_timeout,
        )
