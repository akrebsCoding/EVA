"""Intent-/Entity-Router (P4.T4, `PLAN.md:459`) – **Variante A** (E1).

Auftrag (wörtlich `PLAN.md:459`): ``app/router.py`` (Variante A): Jev →
``intent`` + ``target_class``; DeepSeek → Entity-Auflösung + Service +
``service_data`` + ``response_text``; **Allowlist** (Entity muss im Cache
stehen), **Confidence-Gate 0.75** (E3), **Fallback-Texte** aus v4 §7.2,
``RouterError``.

**Routing-Logik (E1/A):**

1. **Entity-Katalog** aus dem HA-Cache (``ha_client.cached_entities()``) –
   optional per ``entities=`` injizierbar. Ist der Katalog leer, wird nach
   v4 §7.2 **sofort ein Refresh** versucht; bleibt er leer, greift der
   definierte Fehler ``EmptyEntityCacheError`` („Keine Geräte gefunden.").
2. **Jev** klassifiziert ``intent`` (``noul``) und – bei ``JEV_MODE=intent`` –
   ``target_class`` (``choice``). Nur **ein** ``systemone``-Call.
3. **Gate (E3, 0.75):** ``score < 0.5`` ⇒ QUESTION (DeepSeek antwortet);
   ``0.5 ≤ score < 0.75`` ⇒ COMMAND wird abgelehnt (``ConfidenceGateError``,
   **kein** Service-Call); ``score ≥ 0.75`` ⇒ COMMAND.
4. **DeepSeek** löst bei COMMAND Entity + Service + ``service_data`` +
   ``response_text`` auf (JSON-Modus, ``temperature=0``); bei QUESTION liefert
   es nur die Antwort.
5. **Allowlist:** Die aufgelöste ``entity_id`` **muss** im Katalog stehen –
   sonst ``AllowlistViolationError`` (**kein** Service-Call). Die E17-Allowlist
   (`HA_ALLOWED_ENTITIES`) wirkt bereits beim Cache-Aufbau (P4.T0/E55); der
   Router prüft zusätzlich gegen genau den Katalog, den er verwendet.

**``JEV_MODE``-Semantik (E1/E58):** ``intent`` (A, Default) und ``gate`` (B)
nutzen Jev; ``off`` (C) ruft Jev **nicht** auf – dann entscheidet DeepSeek in
**einem** JSON-Aufruf über COMMAND/QUESTION und die Auflösung. Im ``off``-Modus
existiert **kein** Jev-Score, also ist das E3-Gate dort **nicht anwendbar**
(dokumentierte Folge von Variante C).

**``ROUTER_VARIANT`` (E90, User-Entscheidung 2026-09-27):** ``class`` (Default)
= **Variante A** – unverändert; ``entity`` = **Variante D** (2-stufiges Jev):

1. **Jev #1** — ``state`` = **nur Transkript** (ohne Entity-Liste),
   ``questions={intent:noul}`` ⇒ COMMAND/TEXT.  ``intent < 0.5`` ⇒ TEXT ⇒
   DeepSeek (bestehender Frage-Pfad), **kein** Jev #2.
2. **Jev #2** (nur bei COMMAND) — ``state`` = Entity-Liste + Transkript,
   ``questions={target:choice, service:choice}``; ``target``-criteria =
   ``{<entity_id>: <friendly_name>}`` **plus** Pflicht-Option ``none``.
3. **Sicherungen:** ``target == "none"`` ⇒ **kein** HA-Call
   (:class:`EntityNotFoundError`); ``target``-Konfidenz < ``router_confidence_gate``
   ⇒ **kein** HA-Call (:class:`ConfidenceGateError`); Domain autoritativ aus der
   ``entity_id``; Entity muss im Katalog stehen.
4. **255-Cap-Guard:** Katalog > 255 ⇒ **WARN** + Rückfall auf Variante A
   (``class``), **kein** HTTP-400-Crash.
5. **Text:** Jev erzeugt keinen Text ⇒ Bestätigung aus
   :data:`ENTITY_RESPONSE_TEMPLATES` (Dienst→Verb + Anzeigename).

**Grenze v1 (E90, behoben durch v2):** Jev #2 trug **kein** ``service_data`` –
nur ``turn_on``/``turn_off``/``toggle`` („dimme auf 40 %" verliert die Zahl).

**``service_data`` v2 (E92, User-Entscheidung 2026-09-27):** Jev #2 bekommt im
**selben** Call eine dritte ``noul``-Frage ``needs_param`` („muss für diesen
Befehl ein Zahlenwert angegeben werden?").  Nur wenn Jev **ja** sagt **und** das
Paar ``(domain, service)`` in :data:`ENTITY_PARAM_ALLOWLIST` steht, fragt
DeepSeek **ausschließlich** den Zahlenwert.  Die Trennung bleibt sauber:

* **Jev** = *was / welches Gerät / welcher Dienst / ob eine Zahl nötig ist*,
* **DeepSeek** = **nur** der Zahlenwert (kein Entity, kein Dienst, kein Text).

Sicherungen: **genau ein** erlaubter Param-Key je ``(domain, service)``; alles
andere im DeepSeek-JSON wird **verworfen**; die Domain bleibt autoritativ aus
der ``entity_id``; ``target == "none"``/``target.confidence < Gate`` ⇒ **kein**
DeepSeek-Param-Aufruf und **kein** HA-Call.  Fehlt der Wert oder ist er
ungültig ⇒ :class:`EntityParamMissingError`, **kein** Service-Call und der
bestehende „Ich habe dich nicht verstanden."-Ton – **nie** ersatzweise ein
parameterloser ``turn_on``.

**E98 – Negations-Guard (Verneinungsschutz, Grundregel „im Zweifel nicht
schalten"):** eine Verneinung darf **nie** als Schalt-Auftrag ausgeführt werden.
Live-Bug wörtlich: „Schalte Wohnzimmer nicht an." ⇒ ``turn_off`` (Licht **aus**
geschaltet, obwohl „nichts tun" gemeint war), während „Schalte das
Wohnzimmerlicht **nicht** aus." nur zufällig am Jev-Score (``GATE_REJECTED``)
scheiterte – die Verneinung war damit **inkonsistent** behandelt.  Der Guard
sitzt an **zwei** Stellen: als Vorabcheck in :meth:`Router.route` (nur
befehlsförmige Sätze, **vor** jedem LLM-Aufruf) und – verbindlich – in
:meth:`Router.execute` **vor** ``ha_client.call_service``, dem **einzigen**
Call-Punkt.  Ergebnis: **kein** Service-Call + der deutsche Satz
„Ich habe nichts geschaltet. Bitte sag mir klar, was ich tun soll." Details:
:data:`NEGATION_TOKENS`, :func:`detect_negation`, :class:`NegationGuardError`.

**Fallback-Texte – wörtlich v4 §7.2 (`WyomingPlan.txt`):**

* ``FALLBACK_NOT_UNDERSTOOD`` = „Ich habe dich nicht verstanden." (`:871`)
* ``FALLBACK_HA_UNAVAILABLE`` = „Home Assistant antwortet nicht." (`:872`)
* ``FALLBACK_DEEPSEEK`` = „Ich konnte keine Antwort finden." (`:873`)
* ``FALLBACK_NO_DEVICES`` = „Keine Geräte gefunden." (`:875`)

(`FALLBACK_SPEECH` = „Spracherkennung fehlgeschlagen." (`:874`) gehört zum
Whisper-Pfad der Pipeline, nicht zum Router; hier nur als Konstante belegt.)

**Reine Logik (L0):** Dieses Modul macht **keinen** direkten Netz-Aufruf und
importiert **nichts** aus ``app.pipeline``. Die Clients (``jev_client``,
``deepseek_client``, ``ha_client``) werden **injiziert**; ohne Injektion werden
die Default-Clients aus P4.T2/P4.T0 lazily erzeugt (Konstruktion ohne I/O).
Damit ist der Router mit Fakes vollständig netzfrei testbar (P4.T5).

**E113/A-5 (P12.T2) – Katalog-Begrenzung:** mit 16 Cache-Domains lebt die
Instanz (live **375** Entities) über dem 255er-Cap der Jev-``choice``-Frage
(:data:`JEV_MAX_CHOICES`) und der Entity-Prompt wuchs um ~166 Zeilen.  Fix ist
**ein** deterministischer, relevanzbasierter Filter
(:func:`_select_relevant_entities`, **ohne** Zusatz-LLM-Call):

* Ranking = Token-Overlap Transkript ↔ ``friendly_name`` **+** ``entity_id``
  (Gewichte :func:`_relevance_score`) **+** Domain-Aliase („Lampe" ⇒ ``light``),
  Umlaute gefaltet (``ä``→``ae``), Kleinbuchstaben, Füllwörter entfernt.
* Gleichstand ⇒ aufsteigend nach ``entity_id`` ⇒ **deterministisch**.
* **Leere Treffer ⇒ voller Katalog** (kein „nichts gefunden"); der gelieferte
  Satz ist nie kleiner als die Treffer; alles andere scheitert **fail-closed** an
  ``AllowlistViolationError`` (:meth:`_route_command`).
* Grenzen: Jev-``criteria`` ≤ :data:`JEV_CATALOG_LIMIT` (254),
  Prompt-``state``/Entity-Liste ≤ :data:`PROMPT_CATALOG_LIMIT` (120).
* **Beobachtbarkeit:** greift die Begrenzung, loggt
  :func:`_select_relevant_entities` eine **INFO**-Zeile (Kandidaten →
  durchgelassen, Treffer, Modus) und der Turn trägt
  ``raw["catalog_selection"]`` (:data:`CATALOG_SELECTION_KEY`).
* Der Filter ist eine **Begrenzung, kein Vorschaufilter**: passt der Katalog
  ohnehin unter das Limit, wird er **unverändert** durchgereicht.

**P12.T2b / Block B (E114/O-2) – Service-Parameter:** mit 16 Cache-Domains und
``ROUTER_VARIANT=class`` als live wirksamer Pfad war die Parameter-Allowlist
**wirkungslos** – im class-/C-Pfad lief jedes von DeepSeek gelieferte Feld
ungefiltert zu ``ha_client.call_service``, und der Dienst selbst wurde nur auf
Leerheit geprüft.  Block B schließt das mit **einer** Spezifikation und
**einem** Validator für **alle** Varianten:

* :data:`DOMAIN_SERVICE_CRITERIA` (B-1) – harte Domain→Service-Tabelle für die
  **beiden** class-Pfade; ein unbekannter Dienst ⇒
  :class:`RouterProtocolError` (kein Call), **kein** stilles Abschneiden.
* :data:`ENTITY_PARAM_ALLOWLIST` (B-2) – **korrigiert** auf echte
  HA-Service-Namen (``climate.set_temperature`` statt ``climate.turn_on``,
  ``media_player.volume_set`` statt ``turn_on``/``toggle``,
  ``cover.set_cover_position``, ``number``/``input_number.set_value``,
  ``select``/``input_select.select_option``, ``fan.set_percentage``) plus
  :data:`ENTITY_PARAM_KIND` und Entity-Bereiche (``min_temp``/``max_temp``,
  ``min``/``max``/``step``, ``options``).
* :func:`_apply_typed_service_data` (B-3) – **ein** Validator, drei
  Aufrufstellen (Variante D, ``class``, ``off``).  Er filtert auf die
  Allowlist, prüft Enum/Bereich/``step`` und wendet das **Feature-Gate** an.
* B-4 – die Zahlen-**Extraktion** (Variante D) läuft nur, wenn das Paar einen
  Parameter erlaubt, der Satz einen Zahlenwert nennt
  (:func:`_mentions_level_value`) und das Feld kein Enum ist.
* B-5 – semantische Werte (Prozent/Grad) bleiben so in ``service_data`` und der
  Anzeige; die **Wire**-Skalierung (``volume_level`` ÷ 100 ⇒ 0.0–1.0) passiert
  **genau einmal**, am Call-Punkt (:func:`_wire_service_data` in
  :meth:`Router.execute`).
* **E114/O-2 (User-Abtsicherung 2026-09-30):** ein ``light`` **ohne**
  ``SUPPORT_BRIGHTNESS`` kann ``brightness_pct`` nicht (live 21/21).  Statt den
  Wert formal durchzureichen und „Mach das Licht auf 40 Prozent" als
  *Einschalten* auszuführen, wird **ehrlich abgelehnt** – eigener Satz
  :data:`LIGHT_NO_BRIGHTNESS_TEXT`, **kein** Service-Call.
* B-6 – Prompt-Härtung: erlaubte Dienste und erlaubte ``service_data``-Schlüssel
  stehen **ausgeschrieben** im Aufgaben-Prompt
  (:func:`_service_data_hint`) statt „z. B. turn_on|turn_off|toggle", plus
  Anti-Raten-Regel in beiden COMMAND-System-Prompts.
* **B-1 / P12.T4-4 – nackt vs. qualifiziert:** Live-Abnahme T4-3a zeigte
  ``PROTOCOL_ERROR light.turn_on nicht erlaubt`` – DeepSeek liefert den
  **qualifizierten** Dienst, die Kriterien sind **nackt** (HA-Notation), damit
  war die **gesamte** ``light``-Domain live blockiert (bei allen 16 Domains
  möglich).  :func:`_normalize_service_name` ist die **eine** Stelle, an der
  die Form an der LLM-Grenze vereinheitlicht wird (class-/off-/D-Pfad);
  ``light.turn_on`` + Domain-Mismatch ⇒ weiterhin fail-closed **ohne** Call.

**P12.T3 / Block C (E113 i/j) – Sensor- und Zustandsfragen:** bis hierher
bekam das LLM im Frage-Pfad **nur das Transkript** (`_route_question`,
`app/router.py:1376` in T1) – HA-Zustände erreichten DeepSeek nie, also
beantwortete es „wie warm ist es im Wohnzimmer?" aus dem Weltwissen.  Der
letzte blinde Fleck aus `HA_CAPABILITIES.md` §6 ist damit geschlossen:

* **Quelle – kein zweiter Request (C-1):** ``ha_client.readable_snapshot()``
  liest den :class:`app.ha_client.ReadableCache`, der aus **derselben**
  ``/api/states``-Antwort gefüllt wurde (``ReadableCache`` + ``/api/states``
  ⇒ **ein** Request, zwei Caches).  :meth:`Router._load_readable` refresht
  **nie** – ein zweiter Roundtrip pro Frage wäre genau die Latenz, die C nicht
  kosten darf.
* **Auswahl (C-2):** :func:`_select_relevant_states` ⇒ **≤12** Zeilen,
  Ranking über ``friendly_name``/``entity_id``/``device_class``-Messbegriffe/
  Domain-Aliase gegen das gefaltete Transkript, **+** Frische-Bonus; Gleichstand
  ⇒ ``last_updated`` absteigend, dann ``entity_id`` ⇒ deterministisch.  Passt
  **kein** Wert ⇒ **leere Liste** (kein „nimm irgendeinen Sensor").
* **Prompt (C-3):** derselbe Block aus einem Helfer in ``_route_question`` und
  im QUESTION-Zweig von ``_route_deepseek_only``; jede Zeile mit
  ``(entity_id, Stand HH:MM)`` – der ``Stand`` ist **Pflicht**, weil
  ``HA_CACHE_INTERVAL_SECONDS=600`` Werte bis zu 10 min alt sein können.
  ``_FACT_RULE`` verpflichtet das Modell, sich **ausschließlich** auf diese
  Werte zu stützen.
* **Ehrlicher Fallback (C-4):** leere Liste ⇒ :data:`QUESTION_NO_DATA_TEXT`
  statt einer erfundenen Zahl, ``raw["facts_available"] = False`` macht den Fall
  im Log sichtbar.  Im ``off``-Pfad ist der Intent erst **nach** dem einen
  DeepSeek-Call bekannt ⇒ dort wird die Antwort des Modells **verworfen** und
  derselbe Text geliefert.
* **Zaun:** Sensorwerte gehen **nur** in Frage-Prompts.  Der COMMAND-Pfad bleibt
  unverändert (kein Wert im ``state``, kein Wert im Befehls-Prompt), und der
  Negations-Guard (E98) bleibt **vor** jedem LLM-Aufruf wirksam – eine Sensor-
  Frage löst nie einen Service-Call aus.

**Nicht** hier: ``tests/test_router.py`` (P4.T5), ``app/pipeline.py`` (P5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Final, Mapping, Optional, Sequence

from app.config import JEV_MODES, ROUTER_VARIANTS, settings
from app.ha_client import (
    READABLE_MAX_AGE_SECONDS,
    HaClientError,
    HaUnavailableError,
    entity_domain,
    readable_age_seconds,
    readable_timestamp,
    readable_value,
)
from app.llm_client import (
    EntityChoiceResult,
    JEV_MAX_CHOICES,
    JEV_SERVICE_CRITERIA,
    JEV_TARGET_NONE_KEY,
    JevDisabledError,
    LlmClientError,
    LlmProtocolError,
    NoulResult,
    SystemOneResult,
)
from app.logger import get_logger

__all__ = [
    # Intent-Werte (§1.4/v4: „COMMAND" | „QUESTION" | „ERROR")
    "INTENT_COMMAND",
    "INTENT_QUESTION",
    "INTENT_ERROR",
    # Fallback-Texte (wörtlich v4 §7.2)
    "FALLBACK_NOT_UNDERSTOOD",
    "FALLBACK_HA_UNAVAILABLE",
    "FALLBACK_DEEPSEEK",
    "FALLBACK_SPEECH",
    "FALLBACK_NO_DEVICES",
    # Konstanten aus Settings (nicht erfunden)
    "ROUTER_CONFIDENCE_GATE",
    "DEFAULT_JEV_MODE",
    "DEFAULT_ROUTER_VARIANT",
    "COMMAND_SCORE_THRESHOLD",
    "NEEDS_PARAM_THRESHOLD",
    # E92: Param-Pfad (Allowlist, Bereiche, Template)
    "ENTITY_PARAM_ALLOWLIST",
    "ENTITY_PARAM_KIND",
    "ENTITY_PARAM_RANGES",
    "ENTITY_PARAM_WIRE_SCALE",
    "ENTITY_PARAM_RESPONSE_TEMPLATES",
    "NEEDS_PARAM_THRESHOLD",
    # P12.T2b / Block B: Dienst-Whitelist + gemeinsamer Validator (B-1 … B-6)
    "DOMAIN_SERVICE_CRITERIA",
    "LIGHT_NO_BRIGHTNESS_TEXT",
    "SUPPORT_BRIGHTNESS",
    "_normalize_service_name",
    "_apply_typed_service_data",
    "_wire_service_data",
    "_mentions_level_value",
    "_service_data_hint",
    # E98: Negations-Guard (Verneinungsschutz)
    "NEGATION_GUARD_TEXT",
    "NEGATION_GUARD_CODE",
    "NEGATION_TOKENS",
    "detect_negation",
    # E113/A-5: Katalog-Begrenzung (relevanzbasiert)
    "JEV_CATALOG_LIMIT",
    "PROMPT_CATALOG_LIMIT",
    "CATALOG_SELECTION_KEY",
    "CatalogSelection",
    "_select_relevant_entities",
    "_relevance_score",
    "_relevance_tokens",
    # P12.T3/Block C: Lese-Pfad für Sensor-/Zustandsfragen (C-1 … C-4)
    "READABLE_STATE_LIMIT",
    "QUESTION_NO_DATA_TEXT",
    "FACTS_AVAILABLE_KEY",
    "_select_relevant_states",
    "_readable_facts_block",
    "_readable_tokens",
    # Fehler
    "RouterError",
    "RouterConfigError",
    "RouterTurnError",
    "JevError",
    "DeepSeekError",
    "HaError",
    "EmptyEntityCacheError",
    "AllowlistViolationError",
    "ConfidenceGateError",
    "EntityNotFoundError",
    "EntityParamMissingError",
    "NegationGuardError",
    "RouterProtocolError",
    # Ergebnis
    "RouteDecision",
    "Router",
    # Prompts (für P4.T5/Diagnose sichtbar)
    "DEEPSEEK_COMMAND_SYSTEM_PROMPT",
    "DEEPSEEK_QUESTION_SYSTEM_PROMPT",
    "DEEPSEEK_FULL_SYSTEM_PROMPT",
    "DEEPSEEK_PARAM_SYSTEM_PROMPT",
]

_LOG: Final = get_logger("router")

# ── Intent-Werte ──────────────────────────────────────────────────────────
#: Nutzer will ein Gerät steuern (Jev ``noul`` ≥ Gate).
INTENT_COMMAND: Final[str] = "COMMAND"
#: Nutzer stellt eine Frage (Jev ``noul`` < 0.5).
INTENT_QUESTION: Final[str] = "QUESTION"
#: Definierter Fehler-/Fallback-Turn (v4 §7.2).
INTENT_ERROR: Final[str] = "ERROR"

# ── Fallback-Texte – wörtlich v4 §7.2, nicht erfunden ─────────────────────
#: Jev-Timeout/-Fehler (`WyomingPlan.txt:871`), inkl. Gate-Ablehnung/Allowlist.
FALLBACK_NOT_UNDERSTOOD: Final[str] = "Ich habe dich nicht verstanden."
#: HA-API-Fehler (`WyomingPlan.txt:872`); == `HA_UNAVAILABLE_MESSAGE` (E19/E55).
FALLBACK_HA_UNAVAILABLE: Final[str] = "Home Assistant antwortet nicht."
#: DeepSeek-Fehler (`WyomingPlan.txt:873`).
FALLBACK_DEEPSEEK: Final[str] = "Ich konnte keine Antwort finden."
#: Whisper-Fehler (`WyomingPlan.txt:874`) – Pipeline, hier nur belegt.
FALLBACK_SPEECH: Final[str] = "Spracherkennung fehlgeschlagen."
#: Leerer Entity-Cache (`WyomingPlan.txt:875`).
FALLBACK_NO_DEVICES: Final[str] = "Keine Geräte gefunden."

# ── Konfiguration (Werte aus `app.config`, nicht hart kodiert) ────────────
#: E3: unterhalb dieses Werts wird COMMAND abgelehnt (Default 0.75).
ROUTER_CONFIDENCE_GATE: Final[float] = settings.router_confidence_gate
#: Default-``JEV_MODE`` (E1: ``intent``).
DEFAULT_JEV_MODE: Final[str] = settings.jev_mode
#: Default-``ROUTER_VARIANT`` (E90: ``class`` = Variante A, keine Änderung).
DEFAULT_ROUTER_VARIANT: Final[str] = settings.router_variant
#: Semantik von ``NoulResult.value`` (P4.T2/E58): ``score ≥ 0.5`` ⇒ COMMAND-Kandidat.
COMMAND_SCORE_THRESHOLD: Final[float] = 0.5

# ── Variante D (E90) ──────────────────────────────────────────────────────
#: ``target``-Wert „kein passendes Gerät" (aus :mod:`app.llm_client`).
ENTITY_TARGET_NONE: Final[str] = JEV_TARGET_NONE_KEY
#: Antwort-Template je Dienst (Jev erzeugt **keinen** Text).  ``{name}`` =
#: Anzeigename der Entity; keine erfundene Semantik, nur Dienst→Verb.
#: **Schlüssel sind nackt** (``turn_on``) – sie werden mit dem normalisierten
#: Dienst (:func:`_normalize_service_name`) nachgeschlagen, nicht mit dem, was
#: das Modell geliefert hat (``light.turn_on`` ⇒ sonst ``KeyError``).
ENTITY_RESPONSE_TEMPLATES: Final[Mapping[str, str]] = {
    "turn_on": "Okay, {name} eingeschaltet.",
    "turn_off": "Okay, {name} ausgeschaltet.",
    "toggle": "Okay, {name} umgeschaltet.",
}

# ── P12.T2b / Block B: Service-Parameter (B-1 … B-6) ─────────────────────
#: **B-1 – harte Domain→Service-Whitelist.**  Vor B-1 prüfte der live aktive
#: Pfad (``ROUTER_VARIANT=class``) den Dienst nur auf Leerheit
#: (``if not service.strip()``) – jeder Dienst, den DeepSeek nannte, ging an HA.
#: Die Tabelle ist aus live ``GET /api/services`` abgeleitet und
#: **konservativ** geschnitten: nur Dienste, die ein Sprachassistent je
#: sinnvoll senden soll (Parameterdienste inklusive, damit „auf 21 Grad"
#: überhaupt möglich ist).
#:
#: **Absicht eines unbekannten Dienstes:** ein Modell-Fehler ⇒
#: :class:`RouterProtocolError` (kein Call, ehrlicher Fehler-Turn) – **kein**
#: stilles Abschneiden, das den Auftrag halb ausführen würde.
#:
#: **Schlüssel sind nackt** (``turn_on``), weil HA genau das beim Call erwartet.
#: Ein LLM liefert die qualifizierte Form (``light.turn_on``) – deshalb wird
#: jeder gelieferte Dienst **vor** dieser Tabelle über
#: :func:`_normalize_service_name` vereinheitlicht (P12.T4-4, live belegt:
#: ``PROTOCOL_ERROR light.turn_on nicht erlaubt`` ⇒ die ganze Domain blockiert).
#: Ein Präfix, das **nicht** zur Domain der Entity passt, wird verworfen.
#:
#: Der Schnitt deckt **alle 16 Cache-Domains** aus E113/A-1 ab – eine Domain
#: ohne Eintrag hieße, jeder Befehl für sie scheitert grundsätzlich (inkl. der
#: vom User gewählten ``update``/``automation``).
DOMAIN_SERVICE_CRITERIA: Final[Mapping[str, frozenset[str]]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "cover": frozenset(
        {"open_cover", "close_cover", "stop_cover", "set_cover_position"}
    ),
    "climate": frozenset({"turn_on", "turn_off", "set_temperature", "set_hvac_mode"}),
    "media_player": frozenset({"turn_on", "turn_off", "volume_set", "volume_mute"}),
    "scene": frozenset({"turn_on", "turn_off"}),
    "script": frozenset({"turn_on", "turn_off", "toggle"}),
    "button": frozenset({"press"}),
    "number": frozenset({"set_value"}),
    "select": frozenset({"select_option"}),
    "input_boolean": frozenset({"turn_on", "turn_off", "toggle"}),
    "input_select": frozenset({"select_option"}),
    "input_number": frozenset({"set_value"}),
    "fan": frozenset({"turn_on", "turn_off", "set_percentage"}),
    "update": frozenset({"install"}),
    "automation": frozenset({"turn_on", "turn_off", "toggle"}),
}

#: **E92 – Param-Pfad hinter ``needs_param``** (``service_data`` v2).
#: **Sicherheits-Allowlist:** pro ``(domain, service)`` **genau ein** erlaubter
#: Param-Key.  Alles andere – insbesondere jeder Key, den DeepSeek liefert – wird
#: **verworfen**; es gibt **kein** freies Durchreichen von DeepSeek-JSON.  Die
#: Domain ist weiterhin **autoritativ** aus der ``entity_id`` abgeleitet
#: (:func:`app.ha_client.entity_domain`) und wird **nie** aus DeepSeek/Jev
#: übernommen.
#:
#: **Kein** Eintrag für ``*_turn_off``: Ausschalten braucht keinen Helligkeits-/
#: Temperatur-/Lautstärkewert.  Steht ein ``(domain, service)``-Paar nicht in
#: dieser Tabelle, wird für dieses Paar **kein** DeepSeek-Param-Aufruf gemacht –
#: der Aufruf könnte nichts Erlaubtes liefern (und würde nur ein Feld erzeugen,
#: das anschließend verworfen müsste).
#:
#: **Die Paare sind nackt geschlüsselt** – der Dienst kommt an jeder Aufrufstelle
#: bereits durch :func:`_normalize_service_name` (HA-Notation, s.
#: :data:`DOMAIN_SERVICE_CRITERIA`); ein ``light.turn_on`` würde sonst kein
#: Paar finden und der Wert stillschweigend verworfen.
#:
#: =======================================  ====================  ==========
#: | (domain, service)                      | erlaubter Key       | Art      |
#: =======================================  ====================  ==========
#: | ``("light", "turn_on")``               | ``brightness_pct``  | percent  |
#: | ``("light", "toggle")``                | ``brightness_pct``  | percent  |
#: | ``("climate", "set_temperature")``     | ``temperature``     | degrees  |
#: | ``("cover", "set_cover_position")``    | ``position``        | percent  |
#: | ``("media_player", "volume_set")``     | ``volume_level``    | fraction |
#: | ``("fan", "set_percentage")``          | ``percentage``      | percent  |
#: | ``("number", "set_value")``            | ``value``           | number   |
#: | ``("input_number", "set_value")``      | ``value``           | number   |
#: | ``("select", "select_option")``        | ``option``          | enum     |
#: | ``("input_select", "select_option")``   | ``option``          | enum     |
#: =======================================  ====================  ==========
#:
#: **Warum die Korrektur nötig war:** ``climate.turn_on`` **ignoriert**
#: ``temperature`` (nur ``climate.set_temperature`` nimmt den Wert),
#: ``media_player.turn_on``/``toggle`` **starten nur** (nur
#: ``media_player.volume_set`` setzt ``volume_level``), und
#: ``brightness_pct`` kann bei **21 von 21** Live-Lights gar nicht wirken
#: (``supported_features = 4`` ⇒ Bit 1 ``SUPPORT_BRIGHTNESS`` fehlt) ⇒ dafür
#: das Feature-Gate (B-3/:data:`LIGHT_NO_BRIGHTNESS_TEXT`).
ENTITY_PARAM_ALLOWLIST: Final[Mapping[tuple[str, str], str]] = {
    ("light", "turn_on"): "brightness_pct",
    ("light", "toggle"): "brightness_pct",
    ("climate", "set_temperature"): "temperature",
    ("cover", "set_cover_position"): "position",
    ("media_player", "volume_set"): "volume_level",
    ("fan", "set_percentage"): "percentage",
    ("number", "set_value"): "value",
    ("input_number", "set_value"): "value",
    ("select", "select_option"): "option",
    ("input_select", "select_option"): "option",
}
#: **B-2 – Art je erlaubtem Key.**  ``percent``/``degrees``/``fraction``/
#: ``number`` sind numerisch, ``enum`` ist ein **Textwert** aus
#: ``attributes.options`` und kommt deshalb **nie** aus der Zahlen-Extraktion
#: (B-4c).
ENTITY_PARAM_KIND: Final[Mapping[str, str]] = {
    "brightness_pct": "percent",
    "temperature": "degrees",
    "position": "percent",
    "volume_level": "fraction",
    "percentage": "percent",
    "value": "number",
    "option": "enum",
}
#: **Plausibilitätsbereich je numerischem Key** (in der **semantischen**
#: Einheit, s. B-5).  Für ``temperature``/``value`` ist das nur der
#: **Fallback**: liefert die Entity ``min_temp``/``max_temp`` bzw. ``min``/
#: ``max``, gilt der Entity-Bereich (B-2/B-7/B-8).
ENTITY_PARAM_RANGES: Final[Mapping[str, tuple[float, float]]] = {
    "brightness_pct": (0.0, 100.0),
    "position": (0.0, 100.0),
    "percentage": (0.0, 100.0),
    "volume_level": (0.0, 100.0),
    "temperature": (5.0, 40.0),
    "value": (-1000.0, 1000.0),
}
#: Antwort-Template **mit** Zahlenwert (E92).  ``{name}`` = Anzeigename,
#: ``{value}`` = der **validierte semantische** Wert, deutsch formatiert.
#: Bewusst ein Template: Jev erzeugt keinen Text, DeepSeek wird **kein**
#: Freitext erlaubt.  Für **jeden** erlaubten Key existiert ein Eintrag – sonst
#: liefe die Variante D in einen ``KeyError``, sobald ein Parameterdienst
#: dazukommt.
ENTITY_PARAM_RESPONSE_TEMPLATES: Final[Mapping[str, str]] = {
    "brightness_pct": "Okay, {name} auf {value} Prozent.",
    "temperature": "Okay, {name} auf {value} Grad.",
    "volume_level": "Okay, {name} auf {value} Prozent.",
    "position": "Okay, {name} auf {value} Prozent.",
    "percentage": "Okay, {name} auf {value} Prozent.",
    "value": "Okay, {name} auf {value} gestellt.",
    "option": "Okay, {name} auf {value} gestellt.",
}
#: **Welche Attribute** den Entity-Bereich liefern (Bereichs-Validierung pro
#: Entity, B-7/B-8).
ENTITY_PARAM_RANGE_ATTRS: Final[Mapping[str, tuple[str, str]]] = {
    "temperature": ("min_temp", "max_temp"),
    "value": ("min", "max"),
}
#: Attribut für ``step`` (nur ``number``/``input_number``) – der Wert wird auf
#: das Raster gerundet, damit HA keinen Bruchteil zurückweist.
ENTITY_PARAM_STEP_ATTR: Final[str] = "step"
#: Attribut mit den erlaubten Optionen (``select``/``input_select``, B-9).
ENTITY_PARAM_OPTIONS_ATTR: Final[str] = "options"
#: **B-5 – Wire-Skalierung:** der semantische Wert (Prozent/Ganzzahl) wird
#: **erst am Call-Punkt** auf das HA-Format gebracht.  ``volume_level`` erwartet
#: laut Service-Feld **0.0–1.0**; live stehen ``1.0``/``0.53`` in den States.
#: Vorher ging ``40`` (Prozent) unskaliert an HA ⇒ erster Design-Befund B.1(ii).
ENTITY_PARAM_WIRE_SCALE: Final[Mapping[str, float]] = {"volume_level": 0.01}
#: ``supported_features``-Bit für **Helligkeit** (``light``) – live 21/21 ohne.
SUPPORT_BRIGHTNESS: Final[int] = 1 << 1
#: Attribut, in dem HA die ``supported_features`` ablegt.
SUPPORTED_FEATURES_ATTR: Final[str] = "supported_features"
#: **E114 (O-2, User-Abtsicherung 2026-09-30) – eigener Fehlertext** statt
#: „Ich habe dich nicht verstanden.": der Satz wurde verstanden, das **Gerät**
#: kann es nur nicht.  Stil wie :data:`NEGATION_GUARD_TEXT` – kurz, ohne
#: Sonderzeichen, ohne Zahl (TTS/Piper kann ihn unverändert sprechen).
LIGHT_NO_BRIGHTNESS_TEXT: Final[str] = "Diese Lampe kann ich nicht dimmen."
#: **B-6 – Aufzählung der erlaubten Dienste** für die Prompts (ersetzt „z. B.
#: turn_on|turn_off|toggle"): Domain-spezifisch ausgeschrieben statt geraten.
#: **Kein** „z. B." mehr – ein geratener Dienst fällt jetzt hart durch
#: :data:`DOMAIN_SERVICE_CRITERIA`.
SERVICE_HINT_HEADER: Final[str] = "Erlaubte Dienste (nur diese, sonst service null):"
#: **B-6 – Aufzählung der erlaubten ``service_data``-Schlüssel** je Paar.
PARAM_HINT_HEADER: Final[str] = (
    "Zulässige service_data-Schlüssel (alle anderen werden verworfen):"
)
#: **B-6 – Anti-Raten-Regel** für Parameterdienste ohne Zahl/Wert im Satz.
PARAM_HINT_NO_VALUE: Final[str] = (
    "Steht im Transkript keine Zahl bzw. kein Wert, sende \"service_data\": {} "
    "und rate keinen Wert."
)
#: **E92: Auslöse-Schwelle** für ``needs_param`` – aus
#: ``settings.router_needs_param_threshold`` (**konfigurierbar**, nicht hart).
#: Dieselbe Logik wie das Intent-Gate (``NoulResult.value`` = ``score >= 0.5``,
#: E58), weil ``needs_param`` denselben ``noul``-Fragetyp benutzt.
NEEDS_PARAM_THRESHOLD: Final[float] = settings.router_needs_param_threshold
#: Tiefster ``volume_level``-Bruchteil, der noch als Bruchteil gelesen wird
#: (HA-Konvention 0.0–1.0).  Darunter ⇒ Fehler statt Raten.
VOLUME_FRACTION_MAX: Final[float] = 1.0

# ── E98: Negations-Guard (Verneinungsschutz) ───────────────────────────────
#: **Live-Bug, wörtlich:** „Schalte Wohnzimmer nicht an." ⇒ das System rief
#: ``light.wohnzimmer/turn_off`` auf und **schaltete das Licht aus** – die
#: Verneinung wurde als positive Schalt-Anweisung ausgeführt.  Der Vergleich
#: „Schalte das Wohnzimmerlicht **nicht** aus." wurde dagegen blockiert
#: (``GATE_REJECTED``) ⇒ die Verneinungsbehandlung war **inkonsistent**
#: (Jev-Score, nicht Logik).  Eine Verneinung darf **nie** schalten.
#:
#: **Grundregel (E98):** *Im Zweifel nicht schalten.*  Kann die Anweisung nicht
#: eindeutig als **positive** Absicht gelesen werden, gibt es **keinen**
#: Service-Call – der Assistent fragt nach.  Der Guard sitzt an **zwei** Stellen:
#:
#: 1. :meth:`Router.route` – **vor** jedem LLM-Aufruf, aber nur wenn der Satz
#:    **befehlsförmig** ist (:data:`NEGATION_COMMAND_VERBS`/„lass … an/aus"),
#:    damit Statusfragen („Warum ist das Licht nicht aus?") heute wie bisher
#:    beantwortet werden.
#: 2. :meth:`Router.execute` – der **einzige** Punkt, an dem
#:    ``ha_client.call_service`` aufgerufen wird.  Das ist die eigentliche
#:    Zusage: **kein** HA-Call, egal welcher Routing-Pfad (A/D/off) die
#:    COMMAND-Entscheidung erzeugt hat.
#:
#: **Antwort-Ton:** ein **eigener** deutscher Satz statt „Ich habe dich nicht
#: verstanden." – weil der Satz verstanden **wurde**, nur eben **nicht** als
#: Schalt-Auftrag.  Bewusst kurz, ohne Sonderzeichen und ohne Zahl ⇒ TTS/Piper
#: kann ihn unverändert sprechen (kein neues Format).
NEGATION_GUARD_TEXT: Final[str] = (
    "Ich habe nichts geschaltet. Bitte sag mir klar, was ich tun soll."
)
#: Stabiler Kurz-Code (Diagnose/Dashboard/Tests), Muster wie ``GATE_REJECTED``.
NEGATION_GUARD_CODE: Final[str] = "NEGATION_GUARD"
#: Verneinungs-Marker als **exakte** Wörter (klein geschrieben).  Bewusst keine
#: Präfix-Regel für ``nie``: „**nie**rig" ist kein Verneinungs-Marker.
NEGATION_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "nicht",
        "nichts",
        "kein",
        "keine",
        "keinen",
        "keinem",
        "keiner",
        "keines",
        "keinerlei",
        "keineswegs",
        "nie",
        "niemals",
        "nimmer",
        "ohne",
        "weder",
    }
)
#: Zusätzliche **Präfixe** – unkritisch, weil es im Deutschen kein Wort gibt,
#: das mit „nicht"/„kein" beginnt und **keine** Verneinung ist („klein" beginnt
#: mit „kle").  Fängt zusammengeschriebene STR-/ASR-Formen („nichtein").
NEGATION_PREFIXES: Final[tuple[str, ...]] = ("nicht", "kein")
#: **Befehlsförmige** Formulierungen – nur für den Vorabcheck in
#: :meth:`Router.route`.  Ohne eines dieser Wörter bleibt der Satz (z. B. eine
#: Statusfrage) **unverändert** beim Routing.
NEGATION_COMMAND_VERBS: Final[tuple[str, ...]] = (
    "schalte",
    "schalt",
    "schalten",
    "einschalten",
    "ausschalten",
    "anschalten",
    "mach",
    "mache",
    "machst",
    "machen",
    "einmachen",
    "ausmachen",
    "anmach",
    "anmache",
    "dimme",
    "dimmen",
    "stelle",
    "stell",
    "stellen",
    "aktiviere",
    "aktivieren",
    "deaktiviere",
    "deaktivieren",
    "umschalten",
    "toggeln",
)
#: **Ausnahme gegen Fehlalarme:** „nicht **nur** … **sondern** …" ist eine
#: **positive** Mehrfach-Anweisung („schalte nicht nur das Licht ein, sondern
#: auch die Heizung").  Ein naiver Guard würde sie fälschlich blockieren.  Der
#: ganze „nicht nur … sondern"-Abschnitt wird deshalb **vor** der Marker-Suche
#: entfernt – nur diese eine Form, keine allgemeine „nicht … sondern"-Ausnahme
#: (dort ist das „nicht" die echte Verneinung ⇒ konservativ blockieren).
_NEGATION_EXEMPT: Final = re.compile(r"nicht\s+nur\b[^.!?]*?\bsondern\b")
#: Weiche Aufforderung „lass/lasse … an|aus|ein" – **ohne** Verneinungs-Marker,
#: aber im Zweifel genauso mehrdeutig, also geschützt.  Kein Satzzusatz und
#: keine Konjunktion zwischen „lass" und dem Zustandsverb: „lass **mal** das
#: Licht aus" ⇒ geschützt, „lass nicht nur das Licht an, sondern …" nicht.
_NEGATION_LEAVE: Final = re.compile(
    r"\b(?:lass|lasse|lasst|lässt)\b"
    r"(?![^.!?]*\b(?:sondern|und|oder|aber|sowie|auch)\b)"
    r"[^.!?]*?\b(?:aus|an|ein)\b"
)
#: Wort-Tokenisierung: alles, was kein Buchstabe ist, trennt Wörter.  Deckt
#: Satzzeichen, Ziffern und Bindestriche ab, ohne Sprachregeln zu erfinden.
_NEGATION_WORD: Final = re.compile(r"[a-zäöüß]+")

# ── DeepSeek-Prompts (JSON-Modus, temperature=0) ──────────────────────────
#: **B-6 – Prompt-Härtung (System-Prompt).**  Die Zusätze sind **Pflicht**, kein
#: Ornament: ohne sie nennt DeepSeek erfundene Dienste (⇒ ``RouterProtocolError``)
#: oder erfundene Parameter (⇒ verworfen, und der Auftrag läuft ohne den
#: gewünschten Wert).  Die konkrete Dienst-/Parameterliste steht im
#: Aufgaben-Prompt (:func:`_service_data_hint`) – hier nur die **Regel**.
_PARAM_RULE: Final[str] = (
    "Nutze ausschließlich die im Auftrag genannten Dienste und service_data-"
    "Schlüssel. Erfinde weder einen Dienst noch einen Parameterwert: steht im "
    "Transkript keine Zahl oder kein Wert, setze \"service_data\": {} und "
    "bestätige nur die ausgeführte Aktion."
)
#: **C-3 – Prompt-Härtung für Fragen.**  Ohne diese Regel nennt DeepSeek
#: Temperaturen, Anwesenheiten und Wetter aus dem Weltwissen – der Wert im
#: Prompt ist dann nur dekorativ.  Pflicht sind deshalb drei Dinge: sich **auf
#: die gelisteten Werte stützen**, den **Zeitstempel** nennen und ein
#: **fehlendes** Datum benennen statt zu raten.
_FACT_RULE: Final[str] = (
    "Fragen zu Messwerten, Wetter, Sonne oder Personen beantwortest du "
    "ausschließlich aus der Liste 'Aktuelle Werte aus Home Assistant': nenne "
    "den Wert und seinen Zeitstempel (Stand). Steht der gesuchte Wert nicht in "
    "dieser Liste, sage wörtlich, dass du ihn nicht weißt, und erfinde weder "
    "Zahl noch Einheit noch Zeitpunkt."
)
DEEPSEEK_COMMAND_SYSTEM_PROMPT: Final[str] = (
    "Du bist der Geräte-Router eines deutschen Sprachassistenten. Wähle die "
    "Entity ausschließlich aus der mitgegebenen Liste. Antworte nur mit JSON. "
    + _PARAM_RULE
)
DEEPSEEK_QUESTION_SYSTEM_PROMPT: Final[str] = (
    "Du bist ein hilfreicher deutscher Sprachassistent. Antworte kurz und "
    "präzise auf Deutsch. Antworte nur mit JSON. " + _FACT_RULE
)
DEEPSEEK_FULL_SYSTEM_PROMPT: Final[str] = (
    "Du bist der Router eines deutschen Sprachassistenten. Entscheide, ob der "
    "Nutzer ein Gerät steuern will oder eine Frage stellt, und wähle Entities "
    "ausschließlich aus der mitgegebenen Liste. Antworte nur mit JSON. "
    + _PARAM_RULE
    + " "
    + _FACT_RULE
)
#: E92: **ausschließlich** Zahlenextraktion.  Kein Entity-Routing, kein Dienst,
#: kein Antworttext – die Bestätigung bleibt Template.
DEEPSEEK_PARAM_SYSTEM_PROMPT: Final[str] = (
    "Du extrahierst genau einen Zahlenwert aus einem deutschen Sprachbefehl. "
    "Du wählst weder ein Gerät noch einen Dienst und du schreibst keinen "
    "Antworttext. Antworte nur mit JSON."
)


# ── Fehler ────────────────────────────────────────────────────────────────
class RouterError(Exception):
    """Basisklasse aller Router-Fehler.

    ``code`` ist ein stabiler Kurz-Code für Diagnose/Tests. Turn-Fehler
    (:class:`RouterTurnError`) tragen zusätzlich ``response_text`` (v4 §7.2).
    """

    code: str = "ROUTER_ERROR"


class RouterConfigError(RouterError):
    """Programmier-/Config-Fehler (ungültiger ``JEV_MODE``, fehlender Client).

    Wird von :meth:`Router.route_with_fallback` **nicht** geschluckt – ein
    Config-Fehler ist kein Nutzer-Turn.
    """

    code = "ROUTER_CONFIG"


class RouterTurnError(RouterError):
    """Erwarteter Laufzeitfehler eines Turns; trägt den v4-§7.2-Fallback."""

    code = "ROUTER_TURN"
    #: Wörtlicher Piper-Fallback (v4 §7.2).
    response_text: str = FALLBACK_NOT_UNDERSTOOD

    def __init__(self, detail: Optional[str] = None) -> None:
        super().__init__(self.response_text)
        self.detail = detail


class JevError(RouterTurnError):
    """Jev nicht erreichbar/Protokollverstoß (`WyomingPlan.txt:871`)."""

    code = "JEV_ERROR"
    response_text = FALLBACK_NOT_UNDERSTOOD


class DeepSeekError(RouterTurnError):
    """DeepSeek nicht erreichbar/Protokollverstoß (`WyomingPlan.txt:873`)."""

    code = "DEEPSEEK_ERROR"
    response_text = FALLBACK_DEEPSEEK


class HaError(RouterTurnError):
    """HA-/Entity-Cache-Fehler (`WyomingPlan.txt:872`)."""

    code = "HA_ERROR"
    response_text = FALLBACK_HA_UNAVAILABLE


class EmptyEntityCacheError(RouterTurnError):
    """Entity-Cache bleibt auch nach Refresh leer (`WyomingPlan.txt:875`)."""

    code = "EMPTY_ENTITY_CACHE"
    response_text = FALLBACK_NO_DEVICES


class AllowlistViolationError(RouterTurnError):
    """Aufgelöste Entity fehlt im HA-Cache ⇒ **kein** Service-Call (PLAN:459)."""

    code = "ENTITY_NOT_ALLOWED"
    response_text = FALLBACK_NOT_UNDERSTOOD


class ConfidenceGateError(RouterTurnError):
    """COMMAND unter dem Confidence-Gate 0.75 (E3) ⇒ **kein** Service-Call."""

    code = "GATE_REJECTED"
    response_text = FALLBACK_NOT_UNDERSTOOD


class EntityNotFoundError(RouterTurnError):
    """Variante D: Jev #2 ``target == "none"`` ⇒ **kein** Service-Call (E90).

    Text = :data:`FALLBACK_NO_DEVICES` („Keine Geräte gefunden.", v4 §7.2
    ``:875``) – der bestehende „kein Gerät"-Fallback.
    """

    code = "ENTITY_NOT_FOUND"
    response_text = FALLBACK_NO_DEVICES


class EntityParamMissingError(RouterTurnError):
    """E92: verlangter Zahlenwert nicht (sicher) extrahierbar ⇒ **kein** Call.

    Auslöser: ``needs_param`` hoch, aber DeepSeek liefert **keinen** Wert für
    den **einzigen** erlaubten Key (kein Feld / ``null`` / fremder Key), einen
    nicht-numerischen Wert, einen Wert außerhalb des Plausibilitäts- oder des
    Entity-Bereichs – oder der DeepSeek-Aufruf selbst scheitert.

    **Bewusst** wird der Turn **nicht** ersatzweise als parameterloser
    ``turn_on`` ausgeführt: „dimme auf 40 Prozent" ohne die 40 hieße
    *Vollhelligkeit* – die Umkehrung des Auftrags, und zwar **stillschweigend**.
    Stattdessen der bestehende v4-§7.2-Ton :data:`FALLBACK_NOT_UNDERSTOOD`
    („Ich habe dich nicht verstanden."), der zum Wiederholen auffordert.  Es wird
    **kein** neuer Satz erfunden.

    **B-3/E114 – ``reason``:** ``"feature"`` ist der eine Fall mit **eigenem**
    Antworttext (:data:`LIGHT_NO_BRIGHTNESS_TEXT`): die Entity kann die Fähigkeit
    nicht (Live-Beleg: 21/21 Lights ohne ``SUPPORT_BRIGHTNESS``).  Alle anderen
    Gründe behalten den v4-Ton.
    """

    code = "ENTITY_PARAM_MISSING"
    response_text = FALLBACK_NOT_UNDERSTOOD

    #: Bekannte Gründe – als Attribut am Fehlerobjekt (Diagnose/Tests/Dashboard).
    REASON_FEATURE = "feature"
    REASON_OUT_OF_RANGE = "out_of_range"
    REASON_UNKNOWN_OPTION = "unknown_option"
    REASON_MISSING = "missing"

    def __init__(
        self, detail: Optional[str] = None, *, reason: str = REASON_MISSING
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        if reason == self.REASON_FEATURE:
            # E114/O-2: eigener Satz statt „Ich habe dich nicht verstanden." –
            # der Satz war verständlich, das **Gerät** kann es nicht.
            self.response_text = LIGHT_NO_BRIGHTNESS_TEXT


class NegationGuardError(RouterTurnError):
    """E98: Verneinung im Transkript ⇒ **kein** Service-Call.

    Auslöser ist **nicht** der LLM-Score, sondern der **Text**: eine Verneinung
    („schalte X **nicht** an", „lass das Licht aus") darf nie als positive
    Schalt-Anweisung ausgeführt werden – der Live-Fall war das Gegenteil
    (``turn_off`` auf „Schalte Wohnzimmer nicht an.").

    Der Ton ist bewusst **ein eigener deutscher Satz**
    (:data:`NEGATION_GUARD_TEXT`) statt „Ich habe dich nicht verstanden.":
    der Satz wurde verstanden, nur nicht als Schalt-Auftrag.  Der Nutzer wird
    um eine klare Anweisung gebeten – es wird **nichts** geschaltet.
    """

    code = NEGATION_GUARD_CODE
    response_text = NEGATION_GUARD_TEXT


class RouterProtocolError(RouterTurnError):
    """Antwort/Schema von Jev/DeepSeek unbrauchbar (`WyomingPlan.txt:873`)."""

    code = "PROTOCOL_ERROR"
    response_text = FALLBACK_DEEPSEEK


# ── Ergebnis ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RouteDecision:
    """Ergebnis eines Routing-Turns (P4.T5/P5 bauen darauf auf).

    * ``intent`` ∈ :data:`INTENT_COMMAND` / :data:`INTENT_QUESTION` /
      :data:`INTENT_ERROR`.
    * Bei COMMAND: ``entity_id``, ``domain``, ``service``, ``service_data``
      (Payload **ohne** ``entity_id`` – das setzt der HA-Client selbst).
    * ``response_text``: Piper-Text (Antwort oder Fallback).
    * ``executed``/``call_result``: vom :meth:`Router.execute` gesetzt.
    """

    intent: str
    transcript: str = ""
    response_text: str = ""
    target_class: Optional[str] = None
    entity_id: Optional[str] = None
    domain: Optional[str] = None
    service: Optional[str] = None
    service_data: Mapping[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    source: str = ""
    executed: bool = False
    call_result: Any = None
    error_code: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        """True, wenn der Turn einen Service-Call auslösen darf."""
        return self.intent == INTENT_COMMAND

    @property
    def is_question(self) -> bool:
        """True, wenn DeepSeek eine Antwort geliefert hat."""
        return self.intent == INTENT_QUESTION

    @property
    def is_error(self) -> bool:
        """True, wenn ein definierter Fallback vorliegt."""
        return self.intent == INTENT_ERROR


# ── Helfer (rein) ─────────────────────────────────────────────────────────
def _friendly_name(value: Any) -> str:
    """Anzeigename aus einem Cache-Wert (``str`` oder State-``Mapping``).

    Reihenfolge (E115): Home Assistant legt ``friendly_name`` **innerhalb** von
    ``attributes`` ab (``{"state": …, "attributes": {"friendly_name": …}}``,
    ``app/ha_client.py:271``) – das ist der Ort, an dem **echte** Entities den
    Namen tragen.  Ein flaches ``friendly_name`` folgt als Rückwärts-
    kompatibilität (Anzeigename-``str``, Alt-Fixtures);
    ohne Namen bleibt ``""`` und der Aufrufer nimmt die ``entity_id``
    (Notnagel, keine Doppelzählung im Relevance-Score).

    Nicht-``str``, ``None`` und ``Mapping``-Typen führen **nie** zu einer
    Exception – ein kaputter Cache darf die Geräteauswahl nicht sprengen.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for source in (value.get("attributes"), value):
            if not isinstance(source, Mapping):
                continue
            name = source.get("friendly_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def _entity_lines(catalog: Mapping[str, Any]) -> str:
    """Katalog als mehrzeilige ``- <id>: <name>``-Liste (sortiert, stabil)."""
    lines: list[str] = []
    for entity_id in sorted(catalog):
        name = _friendly_name(catalog[entity_id])
        if name and name != entity_id:
            lines.append(f"- {entity_id}: {name}")
        else:
            lines.append(f"- {entity_id}")
    return "\n".join(lines)


def _entity_criteria(catalog: Mapping[str, Any]) -> dict[str, str]:
    """``target``-``criteria`` für Jev #2 (E90): ``{entity_id: friendly_name}``.

    Die Pflicht-Option ``none`` ergänzt :func:`app.llm_client.build_entity_questions`
    (dort **eine** Quelle).  Fehlt ein ``friendly_name``, dient die ``entity_id``
    als Anzeigename.
    """
    criteria: dict[str, str] = {}
    for entity_id in sorted(catalog):
        name = _friendly_name(catalog[entity_id])
        criteria[str(entity_id)] = name or str(entity_id)
    return criteria


# ── Katalog-Begrenzung (E113/A-5, `P12_DESIGN.md` A-5) ─────────────────────
#: **Harte** Obergrenze der Jev-``choice``-Frage: ``JEV_MAX_CHOICES - 1`` = 254.
#: Bei 254 Optionen bleibt die Pflicht-Option ``none`` (255) noch unter dem
#: 255er-Cap von ``app.llm_client.build_entity_questions``.
JEV_CATALOG_LIMIT: Final[int] = JEV_MAX_CHOICES - 1
#: Obergrenze der Entity-Liste **in Prompts** (class/C) – 120 Zeilen.
PROMPT_CATALOG_LIMIT: Final[int] = 120
#: Schlüssel in ``RouteDecision.raw`` mit der Auswahl-Statistik (A-5).
CATALOG_SELECTION_KEY: Final[str] = "catalog_selection"

#: Umlaut-Faltung für den Textvergleich (``ä``→``ae`` …) – nach PLAN-Regel
#: „Umlaute → ae/oe/ue/ss, Kleinbuchstaben".  Großbuchstaben sind **explizit**
#: mitgeführt: ``_fold`` lowered **nach** dem Translate, ein alleinstehendes
#: ``"Ü"`` bliebe sonst ungefaltet und der Vergleich „ÜBER-/ÜBER"-Naming
#: (``switch.ÜBER_…``) würde stillschweigend verfehlen.
_FOLD_MAP: Final[Mapping[int, str]] = str.maketrans(
    {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "ae", "Ö": "oe", "Ü": "ue", "ẞ": "ss",
    }
)
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

#: Füll-/Befehlswörter, die **kein** Geräte-Signal sind.  Ohne diese Liste
#: bekäme z. B. „schalte" jedes ``switch``-Entity Bonuspunkte.
_RELEVANCE_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "hey", "jarvis", "bitte", "mal", "einfach", "kurz", "danke", "dank",
        "schalte", "schalt", "schalten", "schaltest", "schaltete",
        "mache", "mach", "machen", "machst", "machst", "bitte",
        "ein", "aus", "an", "ab", "auf", "zu", "um", "mit", "ohne",
        "im", "in", "am", "an", "der", "die", "das", "den", "dem", "des",
        "den", "dem", "eine", "einen", "einem", "einer", "eines", "ist",
        "sind", "war", "sind", "sein", "hatte", "hat", "gib", "gibst",
        "gehe", "geh", "kann", "kannst", "soll", "sollen", "mag", "wird",
        "wollen", "will", "muss", "mir", "mich", "dich", "sich", "uns",
        "noch", "nur", "so", "dann", "jetzt", "hier", "da", "dort", "jetzt",
        "gerne", "wirklich", "trotzdem", "und", "oder", "aber", "wenn",
        "dass", "was", "wie", "wo", "wer", "welche", "welchen", "welcher",
        "hin", "her", "nochmal", "wieder", "sofort",
    }
)

#: Deutsche **Alias-Begriffe je Domain** – das Transkript sagt „Lampe"/"Musik",
#: die Entity heißt ``light.wohnzimmer``/``media_player.wohnzimmer``.  Ohne
#: diesen Bonus fände „schalte die Lampe im Wohnzimmer an" **keine** Entity.
_DOMAIN_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "light": frozenset(
        {"licht", "lichts", "lampe", "lampen", "leuchte", "beleuchtung",
         "led", "spots"}
    ),
    "switch": frozenset(
        {"schalter", "steckdose", "steckdosen", "stecker"}
    ),
    "cover": frozenset(
        {"rollo", "rolladen", "rolllaeden", "vorhang", "vorhaenge",
         "jalousie", "lamelle", "lamellen", "schatten", "boden", "tuer",
         "tor", "garagentor"}
    ),
    "climate": frozenset(
        {"heizung", "heiz", "klima", "klimatisierung", "thermostat",
         "temperatur", "waerme", "warm", "kalt"}
    ),
    "media_player": frozenset(
        {"musik", "radio", "fernseher", "tv", "player", "lautsprecher",
         "speaker", "ton", "audio", "playlist"}
    ),
    "scene": frozenset({"szene", "szenen", "programm", "stimmung"}),
    "script": frozenset({"skript", "routine", "ansage", "ablauf"}),
    "button": frozenset({"knopf", "knopfdruck", "taste", "tasten", "start"}),
    "number": frozenset({"zahl", "zahlen", "regler", "zahlwert", "wert"}),
    "select": frozenset({"auswahl", "wahl", "option", "optionen"}),
    "input_boolean": frozenset({"schalter", "auswahl", "option", "anlage"}),
    "input_select": frozenset({"auswahl", "wahl", "option", "modus"}),
    "input_number": frozenset({"zahl", "schwelle", "grenzwert", "wert"}),
    "fan": frozenset({"ventilator", "luefter", "geblaese", "luft"}),
    "update": frozenset({"update", "aktualisierung", "firmware", "version"}),
    "automation": frozenset({"automation", "automatisierung", "ablauf"}),
}


def _fold(value: Any) -> str:
    """Text für den Vergleich normalisieren (Kleinbuchstaben, Umlaute gefaltet)."""
    return str(value).translate(_FOLD_MAP).lower()


def _relevance_tokens(text: Any) -> frozenset[str]:
    """Wort-Token eines Textes ohne Füll-/Befehlswörter."""
    words = set(_WORD_RE.findall(_fold(text)))
    return frozenset(words - _RELEVANCE_STOPWORDS)


def _identity_hit(
    transcript_tokens: frozenset[str], entity_id: str, friendly_name: str
) -> bool:
    """True, wenn das Transkript die **Identität** des Geräts benennt.

    „Identität" = Worttreffer im Anzeigenamen **oder** in der ``entity_id`` –
    also das, was das Gerät *heißt*.  Der Domain-Alias zählt hier **nicht**
    bewusst nicht: „Lampe im Wohnzimmer" darf nicht sämtliche Leuchten der
    Instanz zu Treffern machen, sonst bliebe die Auswahl bei 375 Entities
    wertlos.  Der Alias wirkt stattdessen als **Rangbonus** in
    :func:`_relevance_score` (der Treffer mit dem Alias landet vorn, die
    anderen derselben Domain bleiben aber draußen).
    """
    if not transcript_tokens:
        return False
    name_tokens = frozenset(_WORD_RE.findall(_fold(friendly_name)))
    id_tokens = frozenset(_WORD_RE.findall(_fold(entity_id)))
    return bool(transcript_tokens & name_tokens or transcript_tokens & id_tokens)


def _relevance_score(
    transcript_tokens: frozenset[str], entity_id: str, friendly_name: str
) -> int:
    """Punktzahl eines Kandidaten (deterministisch, **ohne** Zusatz-LLM-Call).

    Gewichtung (A-5: „Token-Overlap … Gleichgewicht > Teiltreffer"):

    * **3** je Worttreffer **im Anzeigenamen** (das stärkste Signal – so nennen
      Menschen ihr Gerät),
    * **2** je Worttreffer in der ``entity_id`` (z. B. ``light.wohnzimmer``),
    * **3** je Worttreffer in den **Domain-Aliasen** – nur als *Rang*-Bonus:
      Transkript „Lampe"/„Musik" schlägt damit die „echten“ Namens-/ID-Treffer
      derselben Domain, ohne sie zu Treffern zu machen (s. :func:`_identity_hit`),
    * **1** je **Teiltreffer** (mind. 4 Zeichen) im zusammengesetzten Text aus
      Anzeigename und ``entity_id`` – fängt ``Wohnzimmerlicht`` für „Licht"
      bzw. ``wohnzimmer`` für „Zimmer".

    Gleichstand ⇒ neutral, die Sortierung entscheidet danach über die
    ``entity_id`` (in :func:`_select_relevant_entities`).
    """
    if not transcript_tokens:
        return 0
    folded_id = _fold(entity_id)
    folded_name = _fold(friendly_name)
    name_tokens = frozenset(_WORD_RE.findall(folded_name))
    id_tokens = frozenset(_WORD_RE.findall(folded_id))
    alias_tokens = _DOMAIN_ALIASES.get(entity_domain(entity_id), frozenset())
    haystack = f"{folded_name} {folded_id}"
    exact = len(transcript_tokens & name_tokens) * 3
    exact += len(transcript_tokens & id_tokens) * 2
    exact += len(transcript_tokens & alias_tokens) * 3
    partial = sum(
        1
        for token in transcript_tokens
        if len(token) >= 4 and token in haystack
    )
    return exact + partial


@dataclass(frozen=True)
class CatalogSelection:
    """Ergebnis der Katalog-Begrenzung – auch **Historie** (A-5).

    Wandert als ``RouteDecision.raw["catalog_selection"]`` mit, damit der
    Diagnose-Dash (T4) zeigt, wie viele Kandidaten es gab, wie viele durchkamen
    und ob die Begrenzung gegriffen hat.

    ``entities`` ist **alphabetisch** sortiert (stabile Prompt-Reihenfolge für
    :func:`_entity_lines`/:func:`_entity_criteria`); die *Rangfolge* zeigt sich
    deshalb daran, **welche** Entities die Grenze überleben.
    """

    entities: Mapping[str, Any]
    candidates: int
    selected: int
    matches: int
    limit: int
    truncated: bool
    mode: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-taugliche Zusammenfassung (ohne die Entities selbst)."""
        return {
            "candidates": self.candidates,
            "selected": self.selected,
            "matches": self.matches,
            "limit": self.limit,
            "truncated": self.truncated,
            "mode": self.mode,
        }


def _select_relevant_entities(
    transcript: str,
    catalog: Mapping[str, Any],
    *,
    limit: int,
    purpose: str = "catalog",
) -> CatalogSelection:
    """Katalog auf höchstens ``limit`` Entities begrenzen – **relevanzbasiert**.

    Ablauf (deterministisch, **ohne** Zusatz-LLM-Call):

    1. Passt der Katalog **ohnehin** unter das Limit, wird er **unverändert**
       durchgereicht (``mode="all"``).  Der Filter ist eine **Begrenzung, kein
       Vorschaufilter**: unterhalb des Limits gäbe das Weglassen nichts ein –
       nur das Risiko, ein Gerät zu verlieren, das das Modell sonst gesehen
       hätte.  Für kleine Test-/Demo-Kataloge bleibt das Verhalten damit
       exakt wie bisher (kein ungeplanter Bruch bestehender Zusicherungen).
    2. **Sonst** (Katalog > Limit – live ab 301 Entities, A-5): **Treffer**
       sind Entities, deren **Name** oder ``entity_id`` das Transkript
       *benennt* (:func:`_identity_hit`); der Domain-Alias („Lampe" ⇒ ``light``)
       ist nur ein **Rangbonus** (:func:`_relevance_score`), damit „Lampe im
       Wohnzimmer" nicht jede Leuchte der Instanz zum Treffer macht.  Rangfolge:
       **absteigend nach Punktzahl, dann aufsteigend nach ``entity_id``** ⇒
       identische Auswahl bei identischer Eingabe.
    3. **Kein einziger Treffer** ⇒ **voller Katalog** bis ``limit``
       (``mode="fallback"``) – kein „nichts gefunden", der Befehl scheitert dann
       wie bisher an ``AllowlistViolationError`` (**fail-closed**).
    4. Es wird **nie weniger als die Treffer** geliefert: der gelieferte Satz
       enthält alle Treffer bis zum Limit; kennt das Modell eine Entity nur aus
       dem Katalog, aber nicht aus der Auswahl, wird sie nicht geschaltet.

    :param limit: harte Obergrenze (:data:`JEV_CATALOG_LIMIT` bzw.
        :data:`PROMPT_CATALOG_LIMIT`).
    :param purpose: nur für die Protokollzeile („criteria“, „prompt“, …).
    """
    candidates = sorted(catalog)
    total = len(candidates)

    # (1) Unterhalb des Limits: unverändert durchreichen.
    if limit <= 0 or total <= limit:
        selection = CatalogSelection(
            entities=dict(catalog),
            candidates=total,
            selected=total,
            matches=total,
            limit=limit,
            truncated=False,
            mode="all",
        )
        _LOG.debug(
            "Katalog (%s): %d Kandidaten <= Limit %d – ungefiltert "
            "(%d durchgelassen, Modus=all)",
            purpose,
            total,
            limit,
            total,
        )
        return selection

    # (2) Ranking über den vollen Katalog.  Treffer = Namens-/ID-Worttreffer
    # (:func:`_identity_hit`), der Domain-Alias wirkt nur als Rangbonus.
    tokens = _relevance_tokens(transcript)
    scored: list[tuple[int, bool, str]] = []
    for entity_id in candidates:
        friendly = _friendly_name(catalog[entity_id])
        scored.append(
            (
                _relevance_score(tokens, entity_id, friendly),
                _identity_hit(tokens, entity_id, friendly),
                entity_id,
            )
        )
    matched = sorted(
        (row for row in scored if row[1]),
        key=lambda row: (-row[0], row[2]),
    )

    # (3) Kein Treffer ⇒ voller Katalog (bis zum Limit), deterministisch sortiert.
    if matched:
        ordered = [entity_id for _score, _hit, entity_id in matched]
        mode = "relevance"
    else:
        ordered = list(candidates)
        mode = "fallback"

    selected = ordered[:limit]
    selection = CatalogSelection(
        entities={entity_id: catalog[entity_id] for entity_id in sorted(selected)},
        candidates=total,
        selected=len(selected),
        matches=len(matched),
        limit=limit,
        truncated=len(selected) < total,
        mode=mode,
    )
    # (4) Beobachtbarkeit: greift die Begrenzung, steht es im Log (INFO).
    _LOG.info(
        "Katalog begrenzt (%s): %d Kandidaten -> %d durchgelassen "
        "(Limit %d, Texttreffer %d, Modus=%s, gekuerzt=%s)",
        purpose,
        total,
        selection.selected,
        limit,
        selection.matches,
        mode,
        selection.truncated,
    )
    return selection


# ── P12.T3 / Block C: Lese-Pfad für Sensor- und Zustandsfragen (C-1 … C-4) ─
#: **Harte** Obergrenze der Wertzeilen je Frage-Prompt (C-2).  12 Zeilen sind
#: ~180 Token Prefill (Latenz-Schätzung `P12_DESIGN.md` §Latenz) und reichen
#: für „Wohnzimmer + draußen + wer ist da" in **einer** Antwort.
READABLE_STATE_LIMIT: Final[int] = 12
#: **C-4 – ehrlicher Fallback.**  Passt kein Wert zum Satz, wird **nicht**
#: geraten: der Nutzer bekommt diesen Satz statt einer erfundenen Zahl.  Stil wie
#: :data:`NEGATION_GUARD_TEXT` / :data:`LIGHT_NO_BRIGHTNESS_TEXT` – zwei kurze
#: Sätze, keine Sonderzeichen, keine Zahl ⇒ TTS/Piper spricht ihn unverändert.
QUESTION_NO_DATA_TEXT: Final[str] = (
    "Das weiß ich nicht, ich habe dazu keinen aktuellen Wert aus Home Assistant."
)
#: Schlüssel in ``RouteDecision.raw`` – macht im Log sichtbar, ob eine Frage mit
#: echten Werten beantwortet wurde (``False`` = ehrlicher Fallback, C-4).
FACTS_AVAILABLE_KEY: Final[str] = "facts_available"
#: ``source`` eines Frage-Turns **ohne** Werte (kein LLM-Call, C-4).
FACTS_NONE_SOURCE: Final[str] = "facts-none"

#: Frische-Bonus (C-2) – frische Werte zuerst, uralte Werte (falls doch einmal
#: ein Aufrufer ungefilterte Daten reingibt) nach hinten.
_FRESH_ONE_HOUR: Final[float] = 3600.0
_FRESH_SIX_HOURS: Final[float] = 6 * 3600.0
_FRESH_STALE: Final[float] = -2.0

#: Zusätzliche Füllwörter **nur** für den Lese-Pfad.  A-5 teilt seine Stoppwort-
#: Liste mit dem Geräte-Pfad; hier käme „es" (in „ist es warm?") als Rauschen
#: dazu.  Bewusst additiv – die A-5-Liste bleibt unangetastet.
_READABLE_STOPWORDS: Final[frozenset[str]] = _RELEVANCE_STOPWORDS | {
    "es",
    "euer",
    "eure",
    "euren",
    "euerem",
}

#: **Messbegriffe je ``device_class``** (C-2).  Der Nutzer sagt „wie warm ist
#: es?", die Entity heißt ``sensor.temp_wohnzimmer`` mit
#: ``device_class: temperature`` – ohne diese Brücke fände der Filter nichts.
#: Alle Einträge sind **bereits gefaltet** (Umlaute zu ``ae/oe/ue``).
_READABLE_DEVICE_CLASS_WORDS: Final[Mapping[str, frozenset[str]]] = {
    "temperature": frozenset(
        {"temperatur", "temperaturen", "grad", "warm", "kalt", "kuehle", "hitze",
         "thermostat", "heizung", "klima", "aussen", "draussen", "innen"}
    ),
    "humidity": frozenset(
        {"feuchte", "feuchtigkeit", "luftfeuchte", "luftfeuchtigkeit",
         "humiditaet", "luft"}
    ),
    "illuminance": frozenset(
        {"helligkeit", "licht", "beleuchtung", "lux", "dunkel", "hell"}
    ),
    "power": frozenset({"strom", "leistung", "verbrauch", "watt"}),
    "energy": frozenset({"energie", "verbrauch", "kwh", "zaehler", "kosten"}),
    "battery": frozenset({"akku", "batterie", "battery", "ladung"}),
    "carbon_dioxide": frozenset({"co2", "kohlendioxid", "luftqualitaet"}),
    "carbon_monoxide": frozenset({"co", "kohlenmonoxid"}),
    "pm1": frozenset({"pm1", "feinstaub", "partikel", "staub"}),
    "pm10": frozenset({"pm10", "feinstaub", "partikel", "staub"}),
    "pm25": frozenset({"pm25", "feinstaub", "partikel", "staub"}),
    "aqi": frozenset({"luftqualitaet", "luftindex", "aqi"}),
    "uv_index": frozenset({"uv", "sonnenbrand"}),
    "atmospheric_pressure": frozenset({"luftdruck", "druck", "barometer"}),
    "atmospheric_pressure_altitude": frozenset({"hoehe", "altimeter"}),
    "pressure": frozenset({"druck"}),
    "gas": frozenset({"gas", "gasmenge"}),
    "water": frozenset({"wasser", "leck", "flut", "durchfluss", "fluss"}),
    "moisture": frozenset({"feuchte", "feuchtigkeit", "erdfeuchte", "nass"}),
    "precipitation": frozenset({"regen", "niederschlag"}),
    "precipitation_intensity": frozenset({"regen", "niederschlag"}),
    "wind_speed": frozenset({"wind", "windstaerke", "windgeschwindigkeit"}),
    "wind_direction": frozenset({"windrichtung"}),
    "wind_gust": frozenset({"wind", "boeen", "boen", "sturm"}),
    "voltage": frozenset({"spannung"}),
    "current": frozenset({"strom", "stromstaerke"}),
    "power_factor": frozenset({"leistungsfaktor"}),
    "volume": frozenset({"lautstaerke", "volume", "ton", "laut"}),
    "mould": frozenset({"schimmel"}),
    "signal_strength": frozenset({"signal", "empfang", "funk", "wlan"}),
    "visibility": frozenset({"sicht", "sichtweite"}),
    "sound_pressure": frozenset({"laerm", "geraeusch", "schall", "schalldruck"}),
    "volatile_organic_compounds": frozenset({"voc", "schadstoffe"}),
    "volatile_organic_compounds_parts": frozenset({"voc", "schadstoffe"}),
    "distance": frozenset({"entfernung", "abstand"}),
    "speed": frozenset({"geschwindigkeit"}),
    "weight": frozenset({"gewicht"}),
    "conductivity": frozenset({"leitfaehigkeit"}),
    "ph": frozenset({"ph", "saeure"}),
    "energy_storage": frozenset({"speicher", "batterie", "akku"}),
    # `binary_sensor`-Klassen (C-1-Whitelist) – Tür/Fenster/Melder/Präsenz.
    "window": frozenset({"fenster", "fensterstatus"}),
    "door": frozenset({"tuer", "tür", "türöffner", "eingang", "ausgang"}),
    "garage_door": frozenset({"garage", "garagentor", "tor"}),
    "opening": frozenset({"offen", "oeffnung", "öffnung"}),
    "lock": frozenset({"schloss", "verriegelt", "verschlossen"}),
    "smoke": frozenset({"rauch", "rauchmelder", "feuer"}),
    "safety": frozenset({"sicherheit"}),
    "problem": frozenset({"problem", "stoerung", "störung", "fehler"}),
    "motion": frozenset({"bewegung", "bewegungsmelder"}),
    "occupancy": frozenset({"belegung"}),
    "presence": frozenset(
        {"anwesenheit", "person", "personen", "bewohner", "besuch", "wer", "da"}
    ),
    "connectivity": frozenset({"verbindung", "online", "wifi", "wlan"}),
    "battery_charging": frozenset({"akku", "batterie", "ladung"}),
    "plug": frozenset({"steckdose", "stecker"}),
    "sound": frozenset({"geraeusch", "geräusch", "laerm", "lärm", "klingeln"}),
    "vibration": frozenset({"vibration", "erschuetterung", "erschütterung"}),
    "tamper": frozenset({"manipulation"}),
    "heat": frozenset({"hitze", "waerme", "wärme"}),
    "cold": frozenset({"kaelte", "kälte"}),
    "light": frozenset({"licht", "helligkeit"}),
}

#: Domain-Aliase der **Lese**-Domains (C-2).  Bewusst ohne ``sensor``: dort
#: tragen die Messbegriffe oben – ein Domain-Bonus für ``sensor`` würde **alle**
#: 118 Sensoren jedes Mal gleich behandeln und die Auswahl entweder fluten oder
#: nichts beitragen.  ``person``/``weather``/``sun`` tragen dagegen echte
#: Fragewörter, die **keine** Entity benennt („wer ist da?").
_READABLE_DOMAIN_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "weather": frozenset(
        {"wetter", "wetterbericht", "vorhersage", "draussen", "aussen", "regen",
         "temperatur", "grad", "wind", "luft"}
    ),
    "sun": frozenset(
        {"sonne", "sonnenaufgang", "sonnenuntergang", "tageslicht", "sonnenstand",
         "dunkel", "hell"}
    ),
    "person": frozenset(
        {"person", "personen", "bewohner", "zuhause", "anwesenheit", "anwesend",
         "wer", "da", "besuch", "besucher"}
    ),
    "binary_sensor": frozenset(
        {"fenster", "tuer", "tür", "offen", "geschlossen", "rauch", "wasser",
         "laerm", "lärm", "eingang", "melder"}
    ),
}

#: **Phrasen-Anker je Domain** (C-2).  Einzelne Füllwörter wie „da" oder „wer"
#: stehen in :data:`_READABLE_STOPWORDS` – zu schwach, um daran eine Entity zu
#: hängen („wer" könnte jeder Satz treffen).  Die **Phrase** „wer ist da" ist
#: dagegen eindeutig und wird hier über den gefalteten Rohtext geprüft
#: (``_fold`` ⇒ „draußen" ⇒ ``draussen``).  Bei Treffer bekommt **jede** Entity
#: der Domain denselben Bonus – die 12-Zeilen-Grenze sortiert dann nach
#: Aktualität.
_READABLE_PHRASE_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    "person": (
        "wer ist da",
        "wer ist zuhause",
        "wer ist daheim",
        "wer ist im haus",
        "ist jemand da",
        "ist jemand zuhause",
        "sind alle da",
        "sind alle zuhause",
        "ist jemand im haus",
        "zuhause",
        "im haus",
        "anwesenheit",
        "anwesend",
        "besuch",
    ),
    "weather": (
        "wie ist das wetter",
        "wie ist es draussen",
        "was ist das wetter",
        "wetterbericht",
        "wie warm ist es draussen",
        "wie kalt ist es draussen",
        "regnet es",
        "schneit es",
    ),
    "sun": (
        "sonnenaufgang",
        "sonnenuntergang",
        "wie hell ist es",
        "wie dunkel ist es",
        "tageslicht",
    ),
    "binary_sensor": (
        "ist das fenster",
        "sind die fenster",
        "fenster offen",
        "tuer zu",
        "ist die tuer",
        "ist die tür",
        "rauchmelder",
        "ist das licht",
    ),
}

#: ``Stand HH:MM`` aus einer Wertzeile (für die Kopfzeile des Prompt-Blocks).
_FACT_STAMP_RE: Final[re.Pattern[str]] = re.compile(r"Stand (\d{1,2}:\d{2})")


def _state_display_name(state: Any, entity_id: str) -> str:
    """Anzeigename eines **State**-Mappings für den C-Pfad.

    Home Assistant legt ``friendly_name`` **innerhalb** von ``attributes`` ab
    (``{"state": …, "attributes": {"friendly_name": …}}``).  Seit dem E115-Fix
    (P12.T3b) liest :func:`_friendly_name` denselben Ort zuerst – dieser
    Helfer bleibt für den C-Pfad (mit ``entity_id`` als Notnagel) und folgt
    derselben Reihenfolge ``attributes.friendly_name`` ⇒ ``friendly_name``
    ⇒ ``entity_id``.

    Ohne Namen wäre die Prompt-Zeile ``- sensor.temp_x: 22,4 °C`` – ohne
    Räumskontext, den der Nutzer so nie gesagt hat.
    """
    attributes = state.get("attributes") if isinstance(state, Mapping) else None
    for source in (attributes, state):
        if not isinstance(source, Mapping):
            continue
        name = source.get("friendly_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return entity_id


def _readable_tokens(text: Any) -> frozenset[str]:
    """Transkript-Token für den Lese-Pfad (gefaltet, ohne Füllwörter).

    Wie :func:`_relevance_tokens`, aber mit :data:`_READABLE_STOPWORDS` – die
    A-5-Liste bleibt damit unverändert (kein Risiko für die Geräte-Auswahl).
    """
    words = set(_WORD_RE.findall(_fold(text)))
    return frozenset(words - _READABLE_STOPWORDS)


def _readable_class_words(state: Any) -> frozenset[str]:
    """Messbegriffe des ``device_class`` eines States (leer ohne Attribut)."""
    attributes = state.get("attributes") if isinstance(state, Mapping) else None
    if not isinstance(attributes, Mapping):
        return frozenset()
    device_class = attributes.get("device_class")
    if not isinstance(device_class, str) or not device_class.strip():
        return frozenset()
    return _READABLE_DEVICE_CLASS_WORDS.get(device_class.strip().lower(), frozenset())


def _readable_state_score(
    tokens: frozenset[str], entity_id: str, state: Any, *, folded_text: str = ""
) -> int:
    """Punktzahl eines lesbaren Werts (deterministisch, **ohne** LLM-Call).

    Gewichtung (C-2, Analogie zu :func:`_relevance_score`):

    * **3** je Worttreffer im ``friendly_name`` (der Name, den Menschen sagen),
    * **2** je Worttreffer in der ``entity_id`` (``temp_wohnzimmer``),
    * **3** je Treffer in den **Messbegriffen** des ``device_class``
      („warm" ⇒ ``temperature``),
    * **2** je Treffer im **Domain-Alias** („wer ist da?" ⇒ ``person``),
    * **+3** bei einem **Phrasen-Anker** (:data:`_READABLE_PHRASE_ALIASES`,
      höchstens einmal – sonst würde „zuhause" in drei Varianten denselben
      Bonus stapeln).

    Im Unterschied zum Geräte-Pfad zählt ein Treffer **jeder** Quelle als
    Treffer (Hierarchie-Filter): es gibt keine Sicherheitswirkung, die ein
    Treffer haben könnte, und die harte Grenze von 12 Zeilen plus der leere
    Fall („weiß ich nicht") begrenzen die Auswahl.
    """
    if not tokens and not folded_text:
        return 0
    friendly = _state_display_name(state, entity_id)
    name_tokens = frozenset(_WORD_RE.findall(_fold(friendly)))
    id_tokens = frozenset(_WORD_RE.findall(_fold(entity_id)))
    domain = entity_domain(entity_id)
    score = len(tokens & name_tokens) * 3
    score += len(tokens & id_tokens) * 2
    score += len(tokens & _readable_class_words(state)) * 3
    score += len(tokens & _READABLE_DOMAIN_ALIASES.get(domain, frozenset())) * 2
    if folded_text and any(
        phrase in folded_text
        for phrase in _READABLE_PHRASE_ALIASES.get(domain, ())
    ):
        score += 3
    return score


def _freshness_bonus(age_seconds: Optional[float]) -> int:
    """Frische-Bonus (C-2): ≤1 h **+2**, ≤6 h **+1**, ≤24 h **0**, sonst **−2**.

    ``None`` (kein Zeitstempel) gilt als **frisch** – ein Wert ganz ohne Zeit
    bleibt über die Sortierung vorn und wird am Ende der Zeile sichtbar
    gekennzeichnet; er kommt ohnehin nur durch :func:`is_readable_entity`, wo
    ein fehlender Zeitstempel ausschließt.
    """
    if age_seconds is None:
        return 2
    if age_seconds <= _FRESH_ONE_HOUR:
        return 2
    if age_seconds <= _FRESH_SIX_HOURS:
        return 1
    if age_seconds > READABLE_MAX_AGE_SECONDS:
        return int(_FRESH_STALE)
    return 0


def _readable_line(entity_id: str, state: Any) -> str:
    """Eine Prompt-Zeile: ``- Name: Wert (entity_id, Stand HH:MM)``.

    Die ``entity_id`` steht **mit** drin: sie ist der überprüfbare Beleg, und
    ohne sie kann das Modell „Wohnzimmer 22,4 °C" nicht von einer geratenen Zahl
    unterscheiden.  Fehlt der Zeitstempel (nur bei Direktaufrufen möglich), wird
    das **sichtbar** als ``(entity_id, Stand unbekannt)`` geschrieben – nie
    stillschweigend weggelassen.
    """
    name = _state_display_name(state, entity_id)
    value = readable_value(state, entity_id=entity_id)
    stamp = readable_timestamp(state) or "unbekannt"
    return f"- {name}: {value} ({entity_id}, Stand {stamp})"


def _select_relevant_states(
    transcript: str,
    readable: Mapping[str, Any],
    *,
    limit: int = READABLE_STATE_LIMIT,
) -> list[str]:
    """Höchstens ``limit`` Wertzeilen zum Transkript – **relevanzbasiert** (C-2).

    Ablauf (deterministisch, **kein** Zusatz-LLM-Call, **kein** HTTP):

    1. Punktzahl je Entity über ``friendly_name``/``entity_id``/
       ``device_class``-Messbegriffe/Domain-Alias (Umlaute gefoldt, Füllwörter
       entfernt) **+** Frische-Bonus.
    2. Rangfolge: **absteigend Punktzahl**, dann **jüngstes** ``last_updated``,
       dann aufsteigend ``entity_id`` ⇒ identische Auswahl bei identischer
       Eingabe (kein Zufall, keine Dict-Reihenfolge).
    3. **Kein Treffer ⇒ ``[]``** – bewusst **kein** Fallback auf „die ersten 12
       Cache-Werte": das wäre geraten, und geratene Werte sind genau der Defekt,
       den C schließt.  ``[]`` führt im Router zu
       :data:`QUESTION_NO_DATA_TEXT` (C-4).
    4. ``limit <= 0`` ⇒ ``[]`` (kein „unbegrenzt", das wäre die Prompt-Bombe).

    Die **Frische** ist bereits im Cache hart gefiltert (``unknown``/
    ``unavailable``/``>24 h`` ⇒ raus, C-1); dieser Filter arbeitet trotzdem auf
    beliebigen Mappings und zieht alte Werte mit ``−2`` nur nach hinten.
    """
    if not isinstance(readable, Mapping) or not readable or limit <= 0:
        return []

    tokens = _readable_tokens(transcript)
    folded_text = _fold(transcript)
    rows: list[tuple[int, float, str]] = []
    for entity_id in sorted(str(key) for key in readable):
        state = readable[entity_id]
        score = _readable_state_score(
            tokens, entity_id, state, folded_text=folded_text
        )
        if score <= 0:
            continue
        age = readable_age_seconds(state)
        rows.append(
            (score + _freshness_bonus(age), age if age is not None else 0.0, entity_id)
        )

    if not rows:
        _LOG.info(
            "Frage ohne passenden HA-Wert: 0 Treffer aus %d lesbaren Werten – "
            "ehrliche Antwort statt Raten",
            len(readable),
        )
        return []

    rows.sort(key=lambda row: (-row[0], row[1], row[2]))
    selected = rows[:limit]
    lines = [
        _readable_line(entity_id, readable[entity_id]) for _s, _a, entity_id in selected
    ]
    _LOG.info(
        "HA-Werte fuer Frage begrenzt: %d Kandidaten -> %d Zeilen "
        "(Limit %d, gekuerzt=%s)",
        len(readable),
        len(lines),
        limit,
        len(lines) < len(rows),
    )
    return lines


def _readable_facts_block(lines: Sequence[str]) -> str:
    """Der Prompt-Block: ``Aktuelle Werte aus Home Assistant (Stand HH:MM):``.

    Der ``Stand`` in der **Kopfzeile** ist der **jüngste** Wert der Auswahl (die
    Zeilen tragen zusätzlich ihren eigenen) – damit steht am Blockanfang die
    Aussage „so aktuell ist das Ganze", ohne dass der Wert selbst geraten wird.
    Formulierung wörtlich aus `deploy/docs/P12_DESIGN.md:378`.
    """
    stamps = _FACT_STAMP_RE.findall("\n".join(lines))
    newest = max(stamps) if stamps else "unbekannt"
    return "Aktuelle Werte aus Home Assistant (Stand %s):\n%s" % (
        newest,
        "\n".join(lines),
    )


def _route_question_no_data(transcript: str) -> RouteDecision:
    """C-4 – Frage ohne passenden Wert: ehrlicher Text, **kein** LLM-Call.

    Bewusst **ohne** DeepSeek: ein Aufruf könnte die Zahl erfinden, die gerade
    vermieden werden soll.  Der Turn bleibt ein ganz normaler QUESTION-Turn
    (``source="facts-none"``), damit TTS/Pipeline unverändert arbeiten.
    """
    _LOG.info(
        "QUESTION ohne lesbare HA-Werte – %s (kein LLM-Call, kein Raten)",
        QUESTION_NO_DATA_TEXT,
    )
    return RouteDecision(
        intent=INTENT_QUESTION,
        transcript=transcript,
        response_text=QUESTION_NO_DATA_TEXT,
        source=FACTS_NONE_SOURCE,
        raw={FACTS_AVAILABLE_KEY: False},
    )


def _format_param_value(value: float) -> str:
    """Zahlenwert **deutsch** formatieren (Dezimalkomma) für das Template."""
    number = float(value)
    if number.is_integer():
        return str(int(round(number)))
    return f"{number:.1f}".replace(".", ",")


def _normalize_param_value(param_key: str, value: Any) -> float:
    """DeepSeek-Wert numerisch prüfen und **semantisch** normalisieren (B-5).

    * **numerisch** – ``int``/``float``, ausdrücklich **kein** ``bool`` und
      **kein** Ziffern-String (``"40"`` ⇒ Ablehnung: eine Extrahier-Leistung, die
      Text statt Zahl liefert, ist kein belastbarer Zahlenwert).
    * **Art** – :data:`ENTITY_PARAM_KIND` entscheidet über die Rundung:
      ``degrees``/``number`` behalten eine Nachkommastelle, ``percent``/
      ``fraction`` sind Ganzzahl in der **semantischen** Einheit (Prozent).
    * **Bereich** – :data:`ENTITY_PARAM_RANGES` (Plausibilität, immer);
      der **Entity**-Bereich kommt in :func:`_apply_typed_service_data` dazu.
    * **``volume_level``** – HA kennt zwei Konventionen (0.0–1.0 und 0–100).
      Entscheidung: **Ganzzahl-Literal ⇒ Prozent** (unverändert), **Literal mit
      Dezimalpunkt ≤ 1.0 ⇒ Bruchteil ⇒ × 100**.  Damit bleibt „auf 40" 40 und
      „auf 0,4" wird 40 – deterministisch und ohne Raten.  Die Regel hängt am
      JSON-Literal, nicht am Zahlenwert: ``1`` bleibt bewusst 1 %
      (Ganzzahl), ``1.0`` ist 100 % (Bruchteil).  Die **Wire**-Skalierung
      (÷ 100 auf 0.0–1.0) passiert später und **nur** am Call-Punkt (B-5).

    Ablehnung ⇒ :class:`EntityParamMissingError` ⇒ **kein** Service-Call.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EntityParamMissingError(
            f"DeepSeek lieferte für {param_key} keinen Zahlenwert "
            f"({type(value).__name__})"
        )
    # Bruchteil-Erkennung **vor** der `float()`-Umwandlung: sie hängt am
    # JSON-Literal (Punkt vorhanden?), nicht am Zahlenwert – ``1`` ⇒ 1 %,
    # ``1.0`` ⇒ 100 %.
    is_decimal_literal = isinstance(value, float)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN/inf
        raise EntityParamMissingError(f"{param_key} ist keine endliche Zahl")
    if (
        param_key == "volume_level"
        and is_decimal_literal
        and 0.0 < number <= VOLUME_FRACTION_MAX
    ):
        number = round(number * 100.0, 1)
    low, high = ENTITY_PARAM_RANGES[param_key]
    if not low <= number <= high:
        raise EntityParamMissingError(
            f"{param_key}={number:g} liegt außerhalb {low:g}–{high:g}",
            reason=EntityParamMissingError.REASON_OUT_OF_RANGE,
        )
    if ENTITY_PARAM_KIND[param_key] in ("degrees", "number"):
        return round(number, 1)
    return float(int(round(number)))


# ── B-3: ein gemeinsamer Validator für **alle** Varianten ──────────────────
#: Zahl im Transkript (B-4b) – ein Ziffern-Literal, deutsches Dezimalkomma
#: eingeschlossen.
_TRANSCRIPT_NUMBER: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,]\d+)?")
#: **Wortkontext** einer Level-Angabe.  Nötig, weil „schalte die 2 Lampen ein"
#: zwar eine Zahl enthält, aber **keinen** Helligkeitswert meint – ohne diesen
#: Kontext würde das E114-Gate einen völlig legitimen Befehl ablehnen.
_LEVEL_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"prozent|%|hellig|dimm|dunkel|stufe|level|grad|°|lautstär|lauter|leiser"
    r"|position|öffne|schließe|temperatur",
    re.IGNORECASE,
)


def _mentions_level_value(transcript: str) -> bool:
    """True, wenn der Satz **einen Zahlenwert als Level** nennt (B-4b/E114).

    Zahl **und** Wortkontext müssen beide vorliegen: „auf 40 Prozent"/„dimme auf
    40"/„21,5 Grad" ja, „schalte die 2 Lampen ein" nein.  Diese eine Funktion
    entscheidet an **beiden** Stellen, ob ein Zahlenwert überhaupt erwartet wird
    – der Extraktions-Gate (Variante D) und das Feature-Gate (E114).
    """
    if not isinstance(transcript, str):
        return False
    return bool(_TRANSCRIPT_NUMBER.search(transcript)) and bool(
        _LEVEL_CONTEXT.search(transcript)
    )


def _as_float(value: Any) -> Optional[float]:
    """``int``/``float`` → ``float``; alles andere (inkl. ``bool``) → ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _entity_attributes(entity_state: Any) -> Mapping[str, Any]:
    """``attributes`` eines Cache-Werts (State-``Mapping``) – sonst leer.

    Der Entity-Cache hält den **vollen** HA-State
    (``{"state":…, "attributes": {…}}``, ``app/ha_client.py:271``).  Ein
    Katalogeintrag kann aber auch ein bloßer Anzeigename (``str``) sein –
    dann sind **keine** Attribute bekannt, und die Antwort darauf ist
    konservativ: Fähigkeit unbekannt ⇒ **nicht** unterstützt.
    """
    if isinstance(entity_state, Mapping):
        attributes = entity_state.get("attributes")
        if isinstance(attributes, Mapping):
            return attributes
    return {}


def _entity_supports_feature(entity_state: Any, bit: int) -> bool:
    """``supported_features``-Bit der Entity – **fail-closed**.

    Fehlt das Attribut oder ist es kein ``int``, gilt die Fähigkeit als **nicht
    vorhanden**: lieber ehrlich ablehnen (E114) als einen Wert senden, den das
    Gerät stillschweigend ignoriert.
    """
    raw = _entity_attributes(entity_state).get(SUPPORTED_FEATURES_ATTR)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False
    return bool(raw & bit)


def _entity_param_bounds(
    param_key: str, entity_state: Any
) -> tuple[float, float]:
    """Gültiger Bereich je Key – **Entity-Bereich schlägt Plausibilität**.

    ``temperature`` nutzt ``min_temp``/``max_temp``, ``value`` (number/
    input_number) ``min``/``max`` (B-7/B-8).  Fehlen die Attribute, gilt
    :data:`ENTITY_PARAM_RANGES`.  Ein unbrauchbarer Entity-Bereich
    (``min > max``) wird **verworfen** statt still zu gelten.
    """
    fallback = ENTITY_PARAM_RANGES[param_key]
    attrs_spec = ENTITY_PARAM_RANGE_ATTRS.get(param_key)
    if attrs_spec is None:
        return fallback
    attributes = _entity_attributes(entity_state)
    low = _as_float(attributes.get(attrs_spec[0]))
    high = _as_float(attributes.get(attrs_spec[1]))
    if low is None or high is None or low > high:
        return fallback
    return (low, high)


def _snap_to_step(param_key: str, value: float, entity_state: Any) -> float:
    """Wert auf das ``step``-Raster der Entity runden (``number``/``input_number``).

    HA weist Werte außerhalb des Rasters in der UI zurück; das Runden passiert
    deshalb **vor** dem Call.  Ohne ``step``-Attribut bleibt der Wert unverändert.
    """
    step = _as_float(_entity_attributes(entity_state).get(ENTITY_PARAM_STEP_ATTR))
    if step is None or step <= 0.0:
        return value
    _, high = _entity_param_bounds(param_key, entity_state)
    low = _as_float(
        _entity_attributes(entity_state).get(ENTITY_PARAM_RANGE_ATTRS[param_key][0])
    )
    origin = low if low is not None else 0.0
    snapped = origin + round((value - origin) / step) * step
    snapped = min(max(snapped, origin if low is not None else snapped), high)
    # Rundfehler des Rasters (0.30000000000000004) nicht an HA weitergeben.
    return round(snapped, 3)


def _normalize_service_name(service: str, domain: str) -> str:
    """**B-1 (P12.T4-4) – die *eine* Stelle, an der die Namenform vereinheitlicht wird.

    **Nackt vs. qualifiziert:** Home Assistant erwartet beim Call den **nackten**
    Service-Namen (``turn_on``) plus ``entity_id``.  Genau deshalb sind
    :data:`DOMAIN_SERVICE_CRITERIA`, :data:`ENTITY_PARAM_ALLOWLIST` und
    :data:`ENTITY_RESPONSE_TEMPLATES` durchweg **nackt** geschlüsselt.  Ein LLM
    liefert aber die **qualifizierte** Form – live belegt bei DeepSeek
    („dimme die Lichtsteuerung auf 40 %" ⇒ ``"light.turn_on"`` ⇒
    ``PROTOCOL_ERROR``, die **gesamte** ``light``-Domain war damit blockiert).

    * **nackt** (``turn_on``) ⇒ unverändert (bestehendes Verhalten, Tests grün),
    * **qualifiziert + gleiche Domain** (``light.turn_on`` auf einer ``light``-
      Entity) ⇒ ``turn_on`` – als nackter Dienst ausgeführt,
    * **qualifiziert + andere Domain** (``light.turn_on`` auf einer ``switch``-
      Entity, ``homeassistant.turn_on`` o. Ä.) ⇒ :class:`RouterProtocolError`,
      **kein** Call (fail-closed, wie bisher),
    * ``light.a.b`` / ``light.`` ⇒ :class:`RouterProtocolError`.

    **Die Schranke bleibt B-1:** hier wird nur die *Form* vereinheitlicht, nicht
    die Erlaubnis – :data:`DOMAIN_SERVICE_CRITERIA` wird direkt danach geprüft.
    """
    name = service.strip()
    if "." not in name:
        return name
    prefix, _, bare = name.partition(".")
    bare = bare.strip()
    if not bare or "." in bare:
        raise RouterProtocolError(
            f"service {name!r} ist kein gültiger HA-Service-Name – erwartet "
            "'service' oder 'domain.service'"
        )
    if prefix.strip() != domain:
        raise RouterProtocolError(
            f"service {name!r} gehört zur Domain {prefix.strip()!r}, die Entity "
            f"ist {domain!r} – Domain-Mismatch (B-1), kein Call"
        )
    return bare


def _apply_typed_service_data(
    domain: str,
    service: str,
    raw: Any,
    entity_state: Any,
    *,
    transcript: str = "",
    param_required: bool = False,
) -> dict[str, Any]:
    """**B-3 – der eine Validator** für alle drei Aufrufstellen (Bereich A/D/off).

    Nimmt den rohen ``service_data``-Vorschlag (aus DeepSeek oder aus der
    Zahlen-Extraktion der Variante D) und liefert **bereinigt** den
    ``service_data``-Dict in **semantischer** Einheit (Prozent/Ganzzahl) – oder
    wirft :class:`EntityParamMissingError` ⇒ **kein** Service-Call.

    Schritte:

    1. **Typ** – kein Mapping ⇒ ``RouterProtocolError`` (Modelldefekt, kein
       Nutzerfehler; die Aufrufer prüfen das bereits, die Prüfung bleibt hier
       als Vertrag).
    2. **Allowlist** – nur der zum Paar ``(domain, service)`` erlaubte Key
       überlebt; **jeder** andere Key wird verworfen.  ``service`` ist hier
       **immer nackt** (die Aufrufstellen normalisieren vorher über
       :func:`_normalize_service_name`) – sonst fände der Allowlist-Schlüssel
       ``("light", "turn_on")`` kein Paar.  Paar ohne Key ⇒ ``{}``
       (Ausschalten braucht keinen Wert).  Das ist die E92-Zusage – und sie
       gilt ab hier in **allen** Varianten, nicht nur in ``entity``.
    3. **Feature-Gate (E114/O-2)** – ``light`` ohne
       ``SUPPORT_BRIGHTNESS`` kann ``brightness_pct`` nicht.  Steht der Wert im
       Vorschlag **oder** nennt der Satz einen Zahlenwert
       (:func:`_mentions_level_value`), wird **ehrlich abgelehnt**
       (``reason="feature"``) – **kein** stillschweigendes An/Aus.
    4. **Enum** – ``option`` muss in ``attributes.options`` stehen; ohne dieses
       Attribut ⇒ Ablehnung (nicht validierbar ⇒ kein Call).
    5. **Zahl** – numerisch, im Bereich der **Entity** (``min``/``max``/
       ``min_temp``/``max_temp``) und der Plausibilität, auf ``step`` gerundet.
    6. **Pflicht** – ``param_required`` (Variante D: Jev sagt „Zahl nötig")
       bzw. ein Paar mit Enum-Key ohne Wert ⇒ Ablehnung statt Raten.

    Die **Wire**-Umrechnung (÷ 100 bei ``fraction``) ist **nicht** Teil dieser
    Funktion – sie passiert genau einmal, am Call-Punkt (B-5,
    :func:`_wire_service_data`).
    """
    if not isinstance(raw, Mapping):
        raise RouterProtocolError("service_data ist kein Objekt")
    param_key = ENTITY_PARAM_ALLOWLIST.get((domain, service))
    if param_key is None:
        # Kein erlaubter Key für dieses Paar ⇒ **alles** verwerfen.  Ein
        # Paar ohne Wert ist normal (turn_off, press, open_cover …).
        return {}
    kind = ENTITY_PARAM_KIND[param_key]
    requested = _mentions_level_value(transcript)

    if kind == "enum":
        options = _entity_attributes(entity_state).get(ENTITY_PARAM_OPTIONS_ATTR)
        if not isinstance(options, (list, tuple)) or not options:
            raise EntityParamMissingError(
                f"{param_key}: keine Optionen in den Entity-Attributen – "
                "kein Call ohne prüfbare Auswahl",
                reason=EntityParamMissingError.REASON_UNKNOWN_OPTION,
            )
        value = raw.get(param_key)
        if not isinstance(value, str) or value not in {
            str(option) for option in options
        }:
            raise EntityParamMissingError(
                f"{param_key}={value!r} steht nicht in den Optionen der Entity",
                reason=EntityParamMissingError.REASON_UNKNOWN_OPTION,
            )
        return {param_key: value}

    if param_key == "brightness_pct" and not _entity_supports_feature(
        entity_state, SUPPORT_BRIGHTNESS
    ):
        # E114/O-2: 21 von 21 Live-Lights haben `supported_features = 4` ⇒
        # `brightness_pct` käme bei HA **nicht** an.  Der Satz wurde verstanden,
        # das Gerät kann es nicht ⇒ eigener Satz, **kein** Call.
        if param_key in raw or requested:
            raise EntityParamMissingError(
                f"light ohne {SUPPORTED_FEATURES_ATTR}-Bit {SUPPORT_BRIGHTNESS} "
                f"(Bitmaske {SUPPORT_BRIGHTNESS}) – {param_key} kann nicht "
                "gesetzt werden (E114/O-2)",
                reason=EntityParamMissingError.REASON_FEATURE,
            )
        return {}

    if param_key not in raw:
        if param_required or requested:
            # Ein Zahlenwert **wurde** genannt (bzw. Jev sagt, einer sei nötig),
            # DeepSeek lieferte ihn aber nicht ⇒ nicht raten, nicht halb
            # ausführen (E92-Grundregel).
            raise EntityParamMissingError(
                f"für {domain}.{service} nennt der Satz einen Wert, "
                f"{param_key} fehlt aber im Vorschlag",
                reason=EntityParamMissingError.REASON_MISSING,
            )
        return {}

    value = _normalize_param_value(param_key, raw[param_key])
    low, high = _entity_param_bounds(param_key, entity_state)
    if not low <= value <= high:
        raise EntityParamMissingError(
            f"{param_key}={value:g} liegt außerhalb {low:g}–{high:g} "
            f"(Bereich der Entity)",
            reason=EntityParamMissingError.REASON_OUT_OF_RANGE,
        )
    if kind == "number":
        value = _snap_to_step(param_key, value, entity_state)
    return {param_key: value}


def _wire_service_data(
    domain: str, service: str, service_data: Mapping[str, Any]
) -> dict[str, Any]:
    """**B-5 – semantisch → Wire** an **einem** Ort: dem Call-Punkt.

    Nur der zum Paar erlaubte Key wird skaliert, und nur wenn
    :data:`ENTITY_PARAM_WIRE_SCALE` ihn kennt (``volume_level`` ÷ 100 ⇒ 0.0–1.0
    laut Service-Feld).  Alles andere bleibt unverändert.  Bewusst **keine**
    Filterung hier: die Allowlist ist Sache des Validators (B-3) – diese
    Funktion rechnet nur um, damit die Anzeige „auf 40 Prozent" Prozent bleibt,
    während HA ``0.4`` bekommt.

    ``domain``/``service`` kommen hier aus ``RouteDecision`` und müssen
    **nackt** sein (:func:`_normalize_service_name` an der LLM-Grenze) – ein
    qualifiziertes ``light.turn_on`` hätte keinen Allowlist-Treffer und der
    Wire-Wert ``40`` ginge **unskaliert** an HA.
    """
    param_key = ENTITY_PARAM_ALLOWLIST.get((domain or "", service or ""))
    if param_key is None or param_key not in ENTITY_PARAM_WIRE_SCALE:
        return {str(key): value for key, value in service_data.items()}
    factor = ENTITY_PARAM_WIRE_SCALE[param_key]
    wire: dict[str, Any] = {}
    for key, value in service_data.items():
        if key != param_key:
            wire[str(key)] = value
            continue
        number = _as_float(value)
        wire[param_key] = value if number is None else round(number * factor, 3)
    return wire


def _service_data_hint(entities: Mapping[str, Any]) -> str:
    """**B-6 – Prompt-Härtung:** erlaubte Dienste + erlaubte ``service_data``.

    Nur die Domains, die im **gesendeten** Katalog überhaupt vorkommen, werden
    aufgeführt (der Prompt wächst nicht mit dem 16-Domain-Default).  Die
    Param-Zeilen nennen Schlüssel, Bereich und Einheit – damit DeepSeek den
    erlaubten Key benennt, statt einen zu raten, der dann verworfen würde.
    """
    domains = sorted({entity_domain(key) for key in entities})
    lines = [SERVICE_HINT_HEADER]
    for domain in domains:
        allowed = DOMAIN_SERVICE_CRITERIA.get(domain)
        if allowed:
            # Einrückung: die Hinweiszeilen sind eine **Unterliste** und
            # dürfen nicht wie Katalogzeilen (`- <entity_id>: …`) aussehen –
            # sonst ist der Entity-Block im Prompt nicht mehr abgrenzbar.
            lines.append(f"  - {domain}: {', '.join(sorted(allowed))}")
    params = sorted(
        pair
        for pair, _key in ENTITY_PARAM_ALLOWLIST.items()
        if pair[0] in set(domains)
    )
    if params:
        lines.append(PARAM_HINT_HEADER)
        for domain, service in params:
            key = ENTITY_PARAM_ALLOWLIST[(domain, service)]
            kind = ENTITY_PARAM_KIND[key]
            if kind == "enum":
                detail = "Textwert, nur eine vorhandene Option der Entity"
            elif kind == "degrees":
                detail = "Zahl in Grad, nur im Bereich der Entity"
            elif kind == "fraction":
                detail = "Zahl 0-100 (Prozent)"
            else:
                detail = "Zahl 0-100" if kind == "percent" else "Zahl im Bereich der Entity"
            lines.append(f"  - {domain}.{service}: {key} ({detail})")
        lines.append(PARAM_HINT_NO_VALUE)
    return "\n".join(lines)


# ── E98: Negations-Erkennung (rein, ohne I/O) ─────────────────────────────
def detect_negation(transcript: str) -> Optional[str]:
    """Verneinung im deutschen Transkript – Grund für den Guard, sonst ``None``.

    Reihenfolge (bewusst so, damit die Ausnahme vor dem Marker greift):

    1. **Ausnahme „nicht nur … sondern"** – der Abschnitt wird entfernt
       (:data:`_NEGATION_EXEMPT`).  „schalte **nicht nur** das Licht ein,
       **sondern** auch die Heizung" ist eine **positive** Mehrfach-Anweisung
       und darf **nicht** blockiert werden.
    2. **Weiche Aufforderung „lass … an/aus"** – geschützt (:data:`_NEGATION_LEAVE`).
    3. **Verneinungs-Marker** als exaktes Wort (:data:`NEGATION_TOKENS`) oder
       mit einem der unkritischen Präfixe (:data:`NEGATION_PREFIXES`).

    ``"Warum ist das Licht nicht aus?"`` enthält zwar „nicht" und „aus", ist
    aber **kein** Befehl – die Unterscheidung macht der Aufrufer
    (:func:`_detect_command_negation`), diese Funktion liest nur den Text.

    **Doppelte Verneinung** („nicht **nicht**") ist ein Edgefall ohne
    zuverlässige Lesart ⇒ sie enthält weiterhin „nicht" und wird damit
    **konservativ geblockt** (im Zweifel nicht schalten, E98).

    Rückgabe ist der Begründungs-String (für Log/Tests), nicht nur ein Bool.
    """
    if not isinstance(transcript, str):
        return None
    text = transcript.strip().lower()
    if not text:
        return None
    # (1) Ausnahme zuerst: „nicht nur … sondern" ist keine Verneinung.
    text = _NEGATION_EXEMPT.sub(" ", text)
    if _NEGATION_LEAVE.search(text):
        return "leave_pattern"
    for token in _NEGATION_WORD.findall(text):
        # (3) „niedrig" ist **kein** „nie"-Marker ⇒ nur exakte Wörter.
        if token in NEGATION_TOKENS or token.startswith(NEGATION_PREFIXES):
            return f"marker:{token}"
    return None


def _detect_command_negation(transcript: str) -> Optional[str]:
    """:func:`detect_negation`, aber nur für **befehlsförmige** Sätze.

    Der Vorabcheck in :meth:`Router.route` darf keine Frage verschlucken:
    „Ist das Licht nicht an?" ist eine **Frage** und wird heute vom Router
    beantwortet.  Erst ein Satz mit einem Befehlsverb (:data:`NEGATION_COMMAND_VERBS`)
    oder der weichen „lass … an/aus"-Form gilt als potenzieller Schaltbefehl.
    """
    reason = detect_negation(transcript)
    if reason is None:
        return None
    if reason == "leave_pattern":
        return reason  # „lass … an/aus" ist per Definition befehlsförmig
    lowered = transcript.strip().lower()
    for token in _NEGATION_WORD.findall(_NEGATION_EXEMPT.sub(" ", lowered)):
        if token in NEGATION_COMMAND_VERBS:
            return reason
    return None


def _error_decision(
    transcript: str,
    response_text: str,
    error_code: str,
    *,
    detail: Optional[str] = None,
) -> RouteDecision:
    """Definierter ERROR-Turn mit v4-§7.2-Text (kein Service-Call)."""
    raw: dict[str, Any] = {}
    if detail:
        raw["detail"] = detail
    return RouteDecision(
        intent=INTENT_ERROR,
        transcript=transcript,
        response_text=response_text,
        source="fallback",
        error_code=error_code,
        raw=raw,
    )


# ── Router ────────────────────────────────────────────────────────────────
class Router:
    """Variante-A-Router: Jev → ``intent``/``target_class``, DeepSeek → Auflösung.

    Die drei Clients sind **injizierbar**; ohne Injektion werden die
    Default-Clients aus P4.T2/P4.T0 erzeugt (keine I/O bei der Konstruktion).
    Der Router ruft selbst niemals das Netz auf – nur die injizierten Clients.
    """

    def __init__(
        self,
        *,
        jev_client: Any = None,
        deepseek_client: Any = None,
        ha_client: Any = None,
        jev_mode: Optional[str] = None,
        confidence_gate: Optional[float] = None,
        variant: Optional[str] = None,
        needs_param_threshold: Optional[float] = None,
    ) -> None:
        self.jev_client = (
            jev_client if jev_client is not None else self._build_jev_client()
        )
        self.deepseek_client = (
            deepseek_client
            if deepseek_client is not None
            else self._build_deepseek_client()
        )
        self.ha_client = ha_client
        #: Reihenfolge: expliziter Override → Client-Modus → Settings.
        self.jev_mode = self._resolve_mode(
            jev_mode
            if jev_mode is not None
            else (
                getattr(self.jev_client, "mode", None)
                if self.jev_client is not None
                else None
            )
        )
        #: E90 – ``class`` (A, Default) | ``entity`` (D); aus Settings, nicht hart.
        self.variant: str = self._resolve_variant(
            variant if variant is not None else DEFAULT_ROUTER_VARIANT
        )
        #: E3 – kommt aus `settings.router_confidence_gate`, **nicht** hart 0.75.
        self.confidence_gate: float = (
            float(confidence_gate)
            if confidence_gate is not None
            else ROUTER_CONFIDENCE_GATE
        )
        #: E92 – Auslöse-Schwelle für ``needs_param`` (Jev #2), aus
        #: `settings.router_needs_param_threshold`, **nicht** hart kodiert.
        self.needs_param_threshold: float = (
            float(needs_param_threshold)
            if needs_param_threshold is not None
            else NEEDS_PARAM_THRESHOLD
        )

    # ── Defaults (lazy, keine I/O) ─────────────────────────────────────
    @staticmethod
    def _build_jev_client() -> Any:
        from app.llm_client import JevClient

        return JevClient()

    @staticmethod
    def _build_deepseek_client() -> Any:
        from app.llm_client import DeepSeekClient

        return DeepSeekClient()

    @staticmethod
    def _resolve_mode(mode: Optional[str]) -> str:
        """``JEV_MODE`` validieren (E1/E58: ``intent``/``gate``/``off``)."""
        value = mode if isinstance(mode, str) and mode.strip() else DEFAULT_JEV_MODE
        normalized = value.strip().lower()
        if normalized not in JEV_MODES:
            raise RouterConfigError(
                f"JEV_MODE muss eines von {JEV_MODES} sein (E1), ist {mode!r}"
            )
        return normalized

    @staticmethod
    def _resolve_variant(variant: Optional[str]) -> str:
        """``ROUTER_VARIANT`` validieren (E90: ``class``/``entity``)."""
        value = (
            variant
            if isinstance(variant, str) and variant.strip()
            else DEFAULT_ROUTER_VARIANT
        )
        normalized = value.strip().lower()
        if normalized not in ROUTER_VARIANTS:
            raise RouterConfigError(
                f"ROUTER_VARIANT muss eines von {ROUTER_VARIANTS} sein (E90), "
                f"ist {variant!r}"
            )
        return normalized

    # ── Öffentliche API ────────────────────────────────────────────────
    async def route(
        self,
        transcript: str,
        *,
        entities: Optional[Mapping[str, Any]] = None,
    ) -> RouteDecision:
        """Einen Turn routen – **reine Entscheidung**, kein Service-Call.

        ``entities`` überschreibt den HA-Katalog (Test-/Pipeline-Injektion).
        Fehler ⇒ :class:`RouterTurnError` (:class:`RouterError`); der Caller
        nutzt :meth:`route_with_fallback` für den v4-§7.2-Text.
        """
        if not isinstance(transcript, str) or not transcript.strip():
            raise RouterProtocolError("transcript ist leer oder kein str")

        # E98: Vorabcheck **vor** jedem LLM-Aufruf.  Nur befehlsförmige Sätze
        # („schalte … nicht an", „lass das Licht aus") – Statusfragen und
        # Aussagesätze laufen **unverändert** weiter.  Der zweite, verbindliche
        # Guard sitzt in `execute()` vor `ha_client.call_service`.
        reason = _detect_command_negation(transcript)
        if reason is not None:
            raise NegationGuardError(
                f"Verneinung im Transkript ({reason}) – kein Service-Call (E98)"
            )

        catalog = await self._resolve_catalog(entities)
        if not catalog:
            raise EmptyEntityCacheError(
                "Entity-Cache leer (auch nach Refresh) – WyomingPlan.txt:875"
            )

        if self.jev_mode == "off":
            return await self._route_deepseek_only(transcript, catalog)

        if self.variant == "entity":
            # 255-Cap-Guard (E90): eine ``choice``-Frage verträgt höchstens 255
            # Optionen (256 ⇒ HTTP 400).  Statt zu chunken wird auf Variante A
            # (class) zurückgefallen – kein HTTP-400-Crash, kein stiller Fehler.
            if len(catalog) > JEV_MAX_CHOICES:
                _LOG.warning(
                    "ROUTER_VARIANT=entity, aber Katalog hat %d Entities > %d "
                    "(SystemOne-choice-Grenze) – Fallback auf Variante A (class)",
                    len(catalog),
                    JEV_MAX_CHOICES,
                )
            else:
                return await self._route_entity(transcript, catalog)

        result, selection = await self._classify_with_jev(transcript, catalog)
        score = result.intent.score

        if not result.intent.value:  # score < 0.5 ⇒ QUESTION
            return await self._route_question(transcript, catalog)

        if score < self.confidence_gate:  # E3: COMMAND ablehnen
            raise ConfidenceGateError(
                f"intent.noul={score:.3f} < Gate {self.confidence_gate:.2f} "
                "(E3) – kein Service-Call"
            )

        target_class = result.target.value if result.target is not None else None
        return await self._route_command(
            transcript,
            catalog,
            target_class=target_class,
            confidence=score,
            selection=selection,
        )

    async def route_with_fallback(
        self,
        transcript: str,
        *,
        entities: Optional[Mapping[str, Any]] = None,
    ) -> RouteDecision:
        """Wie :meth:`route`, fängt aber Turn-Fehler → ERROR-Turn (v4 §7.2).

        Ein :class:`RouterConfigError` (Programmierfehler) wird **nicht**
        geschluckt.
        """
        try:
            return await self.route(transcript, entities=entities)
        except RouterConfigError:
            raise
        except RouterTurnError as exc:
            _LOG.warning("Turn-Fallback %s: %s", exc.code, exc)
            return _error_decision(
                transcript,
                exc.response_text,
                exc.code,
                detail=exc.detail,
            )

    async def execute(self, decision: RouteDecision) -> RouteDecision:
        """COMMAND ausführen: ``ha_client.call_service(domain, service, id, **data)``.

        Setzt ``executed=True`` + ``call_result``. HA-Fehler werden zum
        ERROR-Turn „Home Assistant antwortet nicht." (v4 §7.2).

        **E98 – Negations-Guard (verbindlich):** dies ist der **einzige** Punkt
        im Manager, an dem ein Service-Call an Home Assistant abgesetzt wird.
        Steht eine Verneinung im Transkript, wird **hier** – vor
        ``call_service`` – abgebrochen und :data:`NEGATION_GUARD_TEXT`
        gesprochen.  Das gilt **unabhängig** davon, wie die COMMAND-Entscheidung
        zustande kam (Variante A/D/off, L2-Integration, Test-`), also auch dann,
        wenn der Vorabcheck in :meth:`route` nicht gegriffen hat.
        """
        if not decision.is_command:
            return decision
        reason = detect_negation(decision.transcript)
        if reason is not None:
            _LOG.warning(
                "Negations-Guard: kein Service-Call (%s) für %r – %s",
                reason,
                decision.transcript,
                NEGATION_GUARD_TEXT,
            )
            return _error_decision(
                decision.transcript,
                NEGATION_GUARD_TEXT,
                NEGATION_GUARD_CODE,
                detail=f"Verneinung erkannt: {reason}",
            )
        if self.ha_client is None:
            return _error_decision(
                decision.transcript, HaError.response_text, HaError.code,
                detail="kein ha_client injiziert",
            )
        try:
            # B-5 – **erst hier** wird der semantische Wert zum HA-Wert
            # (`volume_level` 40 % ⇒ 0.4).  Der eine Call-Punkt ist auch der
            # einzige Ort, an dem diese Umrechnung überhaupt passiert.
            wire_data = _wire_service_data(
                decision.domain or "", decision.service or "", decision.service_data
            )
            result = await self.ha_client.call_service(
                decision.domain,
                decision.service,
                decision.entity_id,
                **wire_data,
            )
        except HaClientError as exc:
            return _error_decision(
                decision.transcript, HaError.response_text, HaError.code,
                detail=str(exc),
            )
        return replace(decision, executed=True, call_result=result)

    async def handle(
        self,
        transcript: str,
        *,
        entities: Optional[Mapping[str, Any]] = None,
    ) -> RouteDecision:
        """Kompletter Turn: routen (mit Fallback) und COMMAND ausführen."""
        decision = await self.route_with_fallback(transcript, entities=entities)
        return await self.execute(decision)

    # ── Katalog ────────────────────────────────────────────────────────
    async def _resolve_catalog(
        self, entities: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Katalog bestimmen; bei leerem HA-Cache **sofort** Refresh (v4 §7.2)."""
        if entities is not None:
            if not isinstance(entities, Mapping):
                raise RouterConfigError("entities muss ein Mapping sein")
            return {str(key): value for key, value in entities.items()}

        catalog = await self._load_catalog()
        if not catalog:
            catalog = await self._refresh_catalog()
        return catalog

    async def _load_catalog(self) -> dict[str, Any]:
        if self.ha_client is None:
            return {}
        try:
            snapshot = await self.ha_client.cached_entities()
        except HaUnavailableError as exc:
            raise HaError(detail=str(exc)) from exc
        return dict(snapshot) if isinstance(snapshot, Mapping) else {}

    async def _load_readable(self) -> dict[str, Any]:
        """Lese-Cache lesen – **kein** Refresh, **kein** HTTP (P12.T3/C-1).

        Bewusst **ohne** ``_refresh_catalog()``-Semantik: dort ist ein leerer
        Cache ein Anlass für einen sofortigen Roundtrip.  Für C wäre das genau
        falsch herum – ein leerer **Lese**-Cache (HA noch nicht bereit, kein
        ``readable_snapshot`` in einem Test-Double) bedeutet „keine Werte",
        nicht „hole jetzt welche".  Sonst hinge die Antwort einer Frage von der
        Erreichbarkeit des HA-Servers ab und jeder Ausfall kostete zusätzliche
        Retries.  Deshalb fail-closed: Fehler ⇒ ``{}`` ⇒ C-4-Fallback.

        Ein Fehler wird **nicht** zu :class:`HaError` hochgestuft: die Frage ist
        dadurch nicht kaputt, sie ist nur ohne Werte beantwortbar.
        """
        client = self.ha_client
        if client is None:
            return {}
        getter = getattr(client, "readable_snapshot", None)
        if getter is None:
            # Fake/Stub ohne Lese-Pfad (z.B. COMMAND-Tests): leere Fakten ⇒
            # `_route_question` antwortet ehrlich statt zu raten.
            return {}
        try:
            snapshot = await getter()
        except (HaClientError, HaUnavailableError) as exc:
            _LOG.warning(
                "Lese-Cache nicht lesbar (%s) – Frage ohne HA-Werte, kein "
                "Refresh-Versuch (C-1)",
                exc,
            )
            return {}
        return dict(snapshot) if isinstance(snapshot, Mapping) else {}

    async def _refresh_catalog(self) -> dict[str, Any]:
        """`WyomingPlan.txt:875` – sofortiger Refresh, falls Cache leer."""
        if self.ha_client is None:
            return {}
        refresh = getattr(self.ha_client, "refresh", None)
        if refresh is None:
            return {}
        try:
            await refresh()
            snapshot = await self.ha_client.cached_entities()
        except HaUnavailableError as exc:
            raise HaError(detail=str(exc)) from exc
        return dict(snapshot) if isinstance(snapshot, Mapping) else {}

    # ── Jev + DeepSeek ─────────────────────────────────────────────────
    def _build_state(
        self, transcript: str, catalog: Mapping[str, Any], *, purpose: str
    ) -> tuple[str, CatalogSelection]:
        """``state`` = Entity-Liste + Transkript (v4 §5.7, PLAN §1.1).

        Die Entity-Liste ist auf :data:`PROMPT_CATALOG_LIMIT` Zeilen
        begrenzt (E113/A-5) – bei 375 Live-Entities bleibt der Jev-``state``
        damit **kleiner** als vor E113.  Die Auswahl wird zurückgegeben, damit
        der Aufrufer sie 1) als Historie (:data:`CATALOG_SELECTION_KEY`)
        ablegen und 2) im class-Pfad **derselben** Ausgabe für den
        DeepSeek-Prompt nutzen kann (nur eine Filterung, eine Log-Zeile).
        """
        selection = _select_relevant_entities(
            transcript, catalog, limit=PROMPT_CATALOG_LIMIT, purpose=purpose
        )
        return (
            f"Entitäten:\n{_entity_lines(selection.entities)}\n\n"
            f"Transkript: {transcript}",
            selection,
        )

    async def _classify_with_jev(
        self, transcript: str, catalog: Mapping[str, Any]
    ) -> tuple[SystemOneResult, CatalogSelection]:
        """Einen ``systemone``-Call (E1/A). Jev-Fehler ⇒ :class:`JevError`.

        Liefert zusätzlich die Katalog-Auswahl (E113/A-5), damit derselbe
        Entities-Satz in den DeepSeek-Prompt wandert.
        """
        if self.jev_client is None:
            raise JevError("kein jev_client injiziert")
        state, selection = self._build_state(transcript, catalog, purpose="class")
        try:
            result = await self.jev_client.classify(state)
        except JevDisabledError as exc:
            # Mode off wurde oben schon abgefangen; hier defensiv als Fehler.
            raise JevError(f"Jev disabled: {exc}") from exc
        except LlmClientError as exc:
            raise JevError(detail=f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, SystemOneResult):
            raise RouterProtocolError(f"Jev lieferte {type(result)!r} statt SystemOneResult")
        return result, selection

    # ── Variante D (E90): 2-stufiges Jev ───────────────────────────────
    async def _classify_intent_only(self, transcript: str) -> SystemOneResult:
        """**Jev #1**: ``state`` = Transkript **ohne** Entity-Liste, nur ``intent``."""
        if self.jev_client is None:
            raise JevError("kein jev_client injiziert")
        try:
            result = await self.jev_client.classify_intent(transcript)
        except JevDisabledError as exc:
            raise JevError(f"Jev disabled: {exc}") from exc
        except LlmClientError as exc:
            raise JevError(detail=f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, SystemOneResult):
            raise RouterProtocolError(
                f"Jev #1 lieferte {type(result)!r} statt SystemOneResult"
            )
        return result

    async def _classify_entity_choices(
        self,
        transcript: str,
        catalog: Mapping[str, Any],
        selection: CatalogSelection,
    ) -> EntityChoiceResult:
        """**Jev #2**: ``state`` = Entity-Liste + Transkript, ``target``+``service``.

        Die ``criteria`` kommen aus **derselben** Auswahl wie die ``state``-Liste
        (E113/A-5, Limit :data:`JEV_CATALOG_LIMIT`) – sonst könnte Jev eine
        Entity wählen, die es gar nicht gesehen hat.
        """
        if self.jev_client is None:
            raise JevError("kein jev_client injiziert")
        state, state_selection = self._build_state(
            transcript, selection.entities, purpose="state"
        )
        criteria = _entity_criteria(selection.entities)
        _LOG.debug(
            "Jev #2 criteria: %d Kandidaten -> %d Optionen "
            "(state-Liste: %d, Limit criteria=%d, Limit state=%d)",
            state_selection.candidates,
            len(criteria),
            state_selection.selected,
            selection.limit,
            state_selection.limit,
        )
        try:
            result = await self.jev_client.classify_entity(state, criteria)
        except JevDisabledError as exc:
            raise JevError(f"Jev disabled: {exc}") from exc
        except LlmClientError as exc:
            raise JevError(detail=f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, EntityChoiceResult):
            raise RouterProtocolError(
                f"Jev #2 lieferte {type(result)!r} statt EntityChoiceResult"
            )
        return result

    async def _route_entity(
        self, transcript: str, catalog: Mapping[str, Any]
    ) -> RouteDecision:
        """Variante D (E90/E92): COMMAND/TEXT → (Jev #2) → HA-Call, sonst DeepSeek.

        * ``intent < 0.5`` ⇒ TEXT ⇒ :meth:`_route_question` (DeepSeek) – **kein**
          Jev #2, **kein** Param-Aufruf (TEXT-Pfad unverändert).
        * ``target == "none"`` ⇒ **kein** HA-Call (:class:`EntityNotFoundError`).
        * ``target``-Konfidenz < ``router_confidence_gate`` ⇒ **kein** HA-Call
          (:class:`ConfidenceGateError`) – verhindert die Smoke-Fehlschaltung
          (falsche Entity mit Konfidenz 0.55–0.60).
        * Domain **autoritativ** aus der ``entity_id``; Dienst nur aus der
          ``service``-``choice`` (v1: ``turn_on``/``turn_off``/``toggle``).
        * **E92:** erst **nach** diesen drei Sicherungen darf DeepSeek befragt
          werden.  ``needs_param`` (dritte Jev-#2-Frage) hoch **und**
          ``(domain, service)`` in :data:`ENTITY_PARAM_ALLOWLIST` ⇒ genau **ein**
          DeepSeek-Aufruf, der **ausschließlich** den Zahlenwert liefert; das
          Ergebnis landet **nur** unter dem einen erlaubten Key in
          ``service_data``.  Alles andere im DeepSeek-JSON wird **verworfen**.
        """
        intent_result = await self._classify_intent_only(transcript)
        if not intent_result.intent.value:  # < 0.5 ⇒ TEXT/Frage
            return await self._route_question(transcript, catalog)

        # E113/A-5: **eine** Auswahl für Jev #2 – ``criteria`` (≤254) und
        # ``state``-Liste (≤120) leiten sich beide daraus ab.
        selection = _select_relevant_entities(
            transcript, catalog, limit=JEV_CATALOG_LIMIT, purpose="criteria"
        )
        entity_result = await self._classify_entity_choices(
            transcript, catalog, selection
        )
        target = entity_result.target
        service = entity_result.service

        if target.value == ENTITY_TARGET_NONE:
            raise EntityNotFoundError(
                "Jev #2 target='none' – kein passendes Gerät (E90)"
            )
        entity_id = target.value
        # E113/A-5 fail-closed: Jev hat die ``criteria`` aus **dieser** Auswahl
        # gesehen – eine Entity **außerhalb** davon (auch wenn sie im vollen
        # Cache liegt) ist ein Halluzinieren und wird abgewiesen.
        if entity_id not in selection.entities:
            raise AllowlistViolationError(
                f"target {entity_id!r} nicht im gesendeten Katalog "
                f"(PLAN:459/E17)"
            )
        if target.score < self.confidence_gate:
            raise ConfidenceGateError(
                f"target.confidence={target.score:.3f} < Gate "
                f"{self.confidence_gate:.2f} (E90) – kein Service-Call"
            )
        # B-1/P12.T4-4: auch Jev #2 liefert die **Form** frei – erst nackt
        # machen (:func:`_normalize_service_name`), dann die E90-Criteria prüfen.
        # Downstream (Allowlist, ``ENTITY_RESPONSE_TEMPLATES``) sind nackt
        # geschlüsselt und würden einen qualifizierten Namen nur verschlucken.
        # Ein Nicht-``str`` (kein Modellfehler, sondern ein Protokollbruch) geht
        # unverändert in die E90-Prüfung ⇒ dieselbe Ablehnung wie bisher.
        raw_service = service.value
        service_name = (
            _normalize_service_name(raw_service, entity_domain(entity_id))
            if isinstance(raw_service, str)
            else raw_service
        )
        if service_name not in JEV_SERVICE_CRITERIA:
            raise RouterProtocolError(
                f"Jev #2 service {service_name!r} unzulässig (E90)"
            )

        # Domain autoritativ aus der entity_id (nie von Jev übernehmen).
        domain = entity_domain(entity_id)
        entity_state = catalog.get(entity_id)
        name = _friendly_name(entity_state) or entity_id

        service_data, param_key, param_value = await self._resolve_service_data(
            transcript,
            entity_id=entity_id,
            name=name,
            domain=domain,
            service=service_name,
            needs_param=entity_result.needs_param,
            entity_state=entity_state,
        )

        if param_key is None:
            # v1-Verhalten: kein service_data, kein DeepSeek-Param-Aufruf.
            response_text = ENTITY_RESPONSE_TEMPLATES[service_name].format(name=name)
        else:
            # `{value}` = der **validierte semantische** Wert (Prozent/Grad),
            # nicht der Wire-Wert – B-5 skaliert erst am Call-Punkt.
            response_text = ENTITY_PARAM_RESPONSE_TEMPLATES[param_key].format(
                name=name, value=_format_param_value(param_value or 0.0)
            )
        return RouteDecision(
            intent=INTENT_COMMAND,
            transcript=transcript,
            response_text=response_text,
            entity_id=entity_id,
            domain=domain,
            service=service_name,
            service_data=service_data,
            confidence=target.score,
            source="jev+jev",
            raw={
                "target": dict(target.raw),
                "service": dict(service.raw),
                "needs_param": dict(entity_result.needs_param.raw)
                if entity_result.needs_param is not None
                else {},
                CATALOG_SELECTION_KEY: selection.as_dict(),
            },
        )

    async def _resolve_service_data(
        self,
        transcript: str,
        *,
        entity_id: str,
        name: str,
        domain: str,
        service: str,
        needs_param: Optional[NoulResult],
        entity_state: Any,
    ) -> tuple[dict[str, Any], Optional[str], Optional[float]]:
        """B-3/B-4: entscheiden, ob und mit welchem Wert ein ``service_data`` entsteht.

        Ablauf (**kein** zusätzlicher Jev-Call):

        1. ``needs_param`` fehlt (Jev hat nicht geantwortet) oder liegt unter
           :data:`NEEDS_PARAM_THRESHOLD` ⇒ **kein** DeepSeek-Aufruf, kein
           ``service_data`` (v1-Verhalten).
        2. ``(domain, service)`` steht nicht in
           :data:`ENTITY_PARAM_ALLOWLIST` ⇒ **kein** DeepSeek-Aufruf (es gäbe
           nichts Erlaubtes zu übernehmen), kein ``service_data``.
        3. **B-4 – Extraktions-Gate:** nennt das Transkript **überhaupt einen
           Zahlenwert** (:func:`_mentions_level_value`), und ist der erlaubte
           Key **kein** Enum-Feld, wird **genau ein** DeepSeek-Aufruf gemacht.
           Sonst **kein** Aufruf – der häufigste Turn (nur an/aus) bleibt bei
           einem DeepSeek-Call, und ein Enum-Wert wird nie „extrahiert".
           Sagt Jev „Zahl nötig", im Satz steht aber keine ⇒ **Ablehnung**
           statt Raten (kein stilles ``turn_on``).
        4. Der rohe DeepSeek-Wert geht durch **denselben** Validator wie die
           class-/C-Pfade (:func:`_apply_typed_service_data`) – Feature-Gate
           (E114), Enum, Entity-Bereich und Allowlist gelten damit überall.

        ``service`` und ``domain`` stammen hier **bereits** aus den gesicherten
        Größen (``JEV_SERVICE_CRITERIA`` bzw. autoritativ aus ``entity_id``).
        """
        if needs_param is None or needs_param.score < self.needs_param_threshold:
            return {}, None, None
        param_key = ENTITY_PARAM_ALLOWLIST.get((domain, service))
        if param_key is None:
            # Kein erlaubter Param-Key für dieses Paar → kein DeepSeek-Aufruf.
            return {}, None, None
        kind = ENTITY_PARAM_KIND[param_key]
        if kind == "enum":
            # Enum-Werte sind **Text** – sie stehen in `attributes.options` und
            # werden nie aus dem Transkript „extrahiert".
            raise EntityParamMissingError(
                f"{param_key} ist ein Enum-Feld und kommt nicht aus der "
                "Zahlen-Extraktion (B-4c)"
            )
        if not _mentions_level_value(transcript):
            # B-4b: Jev sagt „Zahl nötig", aber der Satz nennt keine ⇒ nicht
            # raten und nicht ersatzweise ohne Wert schalten.
            raise EntityParamMissingError(
                f"needs_param hoch, aber {param_key} wird im Transkript nicht "
                "genannt (B-4b) – kein DeepSeek-Call, kein Service-Call"
            )
        value = await self._extract_param_value(
            transcript,
            entity_id=entity_id,
            name=name,
            domain=domain,
            service=service,
            param_key=param_key,
        )
        service_data = _apply_typed_service_data(
            domain,
            service,
            {param_key: value},
            entity_state,
            transcript=transcript,
            param_required=True,
        )
        # `service_data` enthält genau einen Key; für die **Anzeige** zählt der
        # **validierte semantische** Wert (25,5 % ⇒ „26 Prozent"), nicht der
        # rohe DeepSeek-Wert – sonst widerspricht die Bestätigung dem Call.
        return service_data, param_key, service_data[param_key]

    async def _extract_param_value(
        self,
        transcript: str,
        *,
        entity_id: str,
        name: str,
        domain: str,
        service: str,
        param_key: str,
    ) -> Any:
        """E92: DeepSeek fragt **ausschließlich** den Zahlenwert.

        Der Auftrag enthält Entity (**bereits** aufgelöst), ``entity_id``,
        Dienst, erlaubten Key und das **Transkript**.  DeepSeek wählt **weder**
        Entity noch Dienst und erzeugt **keinen** Antworttext (das bleibt
        Template).  Es wird **ausschließlich** ``payload[param_key]`` gelesen –
        jeder weitere Schlüssel der Antwort wird verworfen.

        **B-3:** die Normalisierung/Bereichs-/Feature-Prüfung passiert **nicht**
        mehr hier, sondern im gemeinsamen Validator – sonst gäbe es wieder zwei
        Logiken.  Der rohe Wert wird deshalb unverändert zurückgegeben.
        """
        prompt = (
            f"Gerät: {name} ({entity_id})\n"
            f"Home-Assistant-Dienst: {domain}.{service}\n"
            f'Zulässiger Parameter: "{param_key}"\n'
            f"Transkript: {transcript}\n\n"
            f'Extrahiere ausschließlich den Zahlenwert für "{param_key}" aus dem '
            "Transkript. Wähle kein Gerät, keinen Dienst und schreibe keinen "
            "Antworttext. Antworte als JSON mit genau einem Feld:\n"
            f'{{"{param_key}": <Zahl>}}\n'
            "Steht im Transkript kein passender Zahlenwert, ist die Zahl nicht "
            "einrangierbar oder liegt sie außerhalb des Bereichs, antworte mit "
            f'{{"{param_key}": null}}.'
        )
        try:
            payload = await self._ask_deepseek(
                prompt, system_prompt=DEEPSEEK_PARAM_SYSTEM_PROMPT
            )
        except (DeepSeekError, RouterProtocolError) as exc:
            # Auch ein ausgefallener DeepSeek-Aufruf ⇒ kein Service-Call und
            # derselbe Wiederholen-Ton (kein „keine Antwort gefunden" – es war
            # keine Frage, sondern ein Befehl mit Zahl).
            raise EntityParamMissingError(
                f"DeepSeek-Paramabfrage für {param_key} fehlgeschlagen: {exc}"
            ) from exc

        # Sicherheitskern: **nur** der eine erlaubte Key wird gelesen.
        value = payload.get(param_key)
        _LOG.debug(
            "DeepSeek-Param %s.%s: %s=%r (erlaubt: %s; alle anderen Felder "
            "verworfen; Validierung im gemeinsamen Validator)",
            domain,
            service,
            param_key,
            value,
            param_key,
        )
        return value


    async def _ask_deepseek(self, prompt: str, *, system_prompt: str) -> Mapping[str, Any]:
        """``complete_json`` (JSON-Modus, temperature=0) + Schema-Check."""
        if self.deepseek_client is None:
            raise DeepSeekError("kein deepseek_client injiziert")
        try:
            payload = await self.deepseek_client.complete_json(
                prompt, system_prompt=system_prompt
            )
        except LlmClientError as exc:
            raise DeepSeekError(detail=f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise RouterProtocolError(
                f"DeepSeek-JSON ist kein Objekt, sondern {type(payload)!r}"
            )
        return payload

    async def _route_command(
        self,
        transcript: str,
        catalog: Mapping[str, Any],
        *,
        target_class: Optional[str],
        confidence: Optional[float],
        source: str = "jev+deepseek",
        selection: Optional[CatalogSelection] = None,
    ) -> RouteDecision:
        # E113/A-5: Entity-Liste im DeepSeek-Prompt auf PROMPT_CATALOG_LIMIT
        # begrenzen (live 375 Entities ⇒ Prompt **kleiner** als vor E113).
        # Im class-Pfad kommt ``selection`` aus :meth:`_classify_with_jev` –
        # dieselbe Auswahl, die schon im Jev-``state`` stand (eine Log-Zeile).
        if selection is None:
            selection = _select_relevant_entities(
                transcript, catalog, limit=PROMPT_CATALOG_LIMIT, purpose="prompt"
            )
        prompt = (
            f"Transkript: {transcript}\n"
            f"Erkannte Geräteklasse: {target_class or 'unbekannt'}\n\n"
            f"Verfügbare Entities:\n{_entity_lines(selection.entities)}\n\n"
            f"{_service_data_hint(selection.entities)}\n\n"
            "Bestimme die passende Entity und den Home-Assistant-Service. "
            "Antworte als JSON mit genau diesen Feldern:\n"
            '{"entity_id": "<id aus der Liste oder null>", '
            '"domain": "<domain oder null>", '
            '"service": "<einer der erlaubten Dienste oder null>", '
            '"service_data": {<nur die erlaubten Parameter>}, '
            '"response_text": "<kurze deutsche Bestätigung>"}\n'
            "Wähle entity_id ausschließlich aus der Liste; passt keine, setze null."
        )
        payload = await self._ask_deepseek(
            prompt, system_prompt=DEEPSEEK_COMMAND_SYSTEM_PROMPT
        )

        entity_id = payload.get("entity_id")
        # Fail-closed gegen die **gesendete** Liste, nicht gegen den vollen
        # Cache: DeepSeek kann keine Entity wählen, die es nicht gesehen hat.
        if not isinstance(entity_id, str) or entity_id not in selection.entities:
            raise AllowlistViolationError(
                f"entity_id {entity_id!r} nicht im gesendeten Katalog "
                f"(PLAN:459/E17)"
            )

        service = payload.get("service")
        if not isinstance(service, str) or not service.strip():
            raise RouterProtocolError("DeepSeek lieferte keinen gültigen service")

        # Domain ist autoritativ der Präfix der entity_id.
        domain = entity_domain(entity_id)
        reported_domain = payload.get("domain")
        if (
            isinstance(reported_domain, str)
            and reported_domain.strip()
            and reported_domain.strip() != domain
        ):
            raise RouterProtocolError(
                f"domain {reported_domain!r} passt nicht zu {entity_id!r}"
            )

        # B-1: der Dienst muss in der Tabelle der **autoritativen** Domain
        # stehen.  Ohne diese Schranke wäre jeder DeepSeek-Dienst schaltbar –
        # das war der Kerndefekt „im live aktiven Pfad gar keine Allowlist".
        # B-1/P12.T4-4: erst die **Form** vereinheitlichen (nackt/qualifiziert),
        # dann die **Erlaubnis** prüfen – DeepSeek liefert live `light.turn_on`.
        raw_service = service.strip()
        service = _normalize_service_name(raw_service, domain)
        if service not in DOMAIN_SERVICE_CRITERIA.get(domain, frozenset()):
            raise RouterProtocolError(
                f"service {raw_service!r} ist für Domain {domain!r} nicht erlaubt "
                "(B-1/DOMAIN_SERVICE_CRITERIA)"
            )

        data = payload.get("service_data") or {}
        # B-3: **ein** Validator für alle Varianten – Allowlist, Feature-Gate
        # (E114), Enum und Entity-Bereich.  Vorher lief hier jeder Key
        # ungefiltert zu `ha_client.call_service` durch.
        service_data = _apply_typed_service_data(
            domain,
            service,
            data,
            selection.entities.get(entity_id),
            transcript=transcript,
        )

        response_text = payload.get("response_text")
        if not isinstance(response_text, str):
            response_text = ""

        return RouteDecision(
            intent=INTENT_COMMAND,
            transcript=transcript,
            response_text=response_text,
            target_class=target_class,
            entity_id=entity_id,
            domain=domain,
            service=service.strip(),
            service_data=service_data,
            confidence=confidence,
            source=source,
            raw={**dict(payload), CATALOG_SELECTION_KEY: selection.as_dict()},
        )

    async def _route_question(
        self, transcript: str, catalog: Mapping[str, Any]
    ) -> RouteDecision:
        """Frage mit **echten HA-Werten** beantworten (P12.T3/C-3, C-4).

        Der Pfad ist unverändert „genau ein DeepSeek-Call" – nur der Prompt hat
        zusätzlich den Block aus :func:`_readable_facts_block`:

        * **Werte vorhanden** ⇒ Prompt enthält ≤12 Zeilen mit Wert,
          ``entity_id`` und ``Stand``; ``raw["facts_available"] = True`` macht
          im Log sichtbar, dass die Antwort auf einem Wert beruht.
        * **keine Werte** ⇒ **kein** DeepSeek-Call, sondern
          :func:`_route_question_no_data` (C-4).  Ein Aufruf ohne Werte im
          Prompt würde die Zahl aus dem Weltwissen holen – also genau die
          Halluzination, die C verhindern soll.

        ``catalog`` wird hier **nicht** gebraucht (Sensorwerte kommen aus dem
        Lese-Cache, C-1) – die Signatur bleibt, damit alle Aufrufer
        unverändert bleiben.
        """
        lines = _select_relevant_states(transcript, await self._load_readable())
        if not lines:
            return _route_question_no_data(transcript)

        prompt = (
            f"Transkript: {transcript}\n\n"
            f"{_readable_facts_block(lines)}\n\n"
            'Antworte als JSON: {"response_text": "<deutsche Antwort>"}'
        )
        payload = await self._ask_deepseek(
            prompt, system_prompt=DEEPSEEK_QUESTION_SYSTEM_PROMPT
        )
        response_text = payload.get("response_text")
        if not isinstance(response_text, str) or not response_text.strip():
            raise RouterProtocolError("DeepSeek lieferte keinen response_text")
        return RouteDecision(
            intent=INTENT_QUESTION,
            transcript=transcript,
            response_text=response_text.strip(),
            source="jev+deepseek",
            raw={**dict(payload), FACTS_AVAILABLE_KEY: True},
        )

    async def _route_deepseek_only(
        self, transcript: str, catalog: Mapping[str, Any]
    ) -> RouteDecision:
        """``JEV_MODE=off`` (E1/C): DeepSeek entscheidet Intent **und** Auflösung.

        Kein Jev-Aufruf. Ohne Jev-Score ist das E3-Gate nicht anwendbar
        (dokumentierte Folge von Variante C).

        P12.T3/C: Auch hier ist das **ein** DeepSeek-Call für Intent **und**
        Antwort.  Deshalb liegen die HA-Werte schon im Intent-Prompt: das Modell
        kann eine Frage beantworten, ohne einen zweiten Aufruf.  Für den Fall
        „QUESTION, aber nichts Passendes" ist der Intent-Call trotzdem nötig
        (COMMAND könnte es ja sein) – dann wird die Modellantwort **verworfen**
        und :func:`_route_question_no_data` geliefert, damit auch hier keine
        Zahl erfunden wird.  Der COMMAND-Zweig bleibt unverändert (kein Wert im
        ``service_data``, kein zweiter Prompt).
        """
        # E113/A-5: auch im C-Pfad ist die Entity-Liste begrenzt.
        selection = _select_relevant_entities(
            transcript, catalog, limit=PROMPT_CATALOG_LIMIT, purpose="prompt"
        )
        # C-1/C-3: Werte **einmal** lesen – für den Prompt und die Frage-Entscheidung.
        fact_lines = _select_relevant_states(transcript, await self._load_readable())
        facts_block = f"{_readable_facts_block(fact_lines)}\n\n" if fact_lines else ""
        prompt = (
            f"Transkript: {transcript}\n\n"
            f"{facts_block}"
            f"Verfügbare Entities:\n{_entity_lines(selection.entities)}\n\n"
            f"{_service_data_hint(selection.entities)}\n\n"
            "Entscheide, ob der Nutzer ein Gerät steuern will (COMMAND) oder "
            "eine Frage stellt (QUESTION). Antworte als JSON:\n"
            '{"intent": "COMMAND"|"QUESTION", "target_class": "<klasse oder null>", '
            '"entity_id": "<id aus der Liste oder null>", "domain": "<domain oder null>", '
            '"service": "<einer der erlaubten Dienste oder null>", '
            '"service_data": {<nur die erlaubten Parameter>}, '
            '"response_text": "<deutsche Antwort/Bestätigung>"}'
        )
        payload = await self._ask_deepseek(
            prompt, system_prompt=DEEPSEEK_FULL_SYSTEM_PROMPT
        )
        intent = payload.get("intent")
        if not isinstance(intent, str):
            raise RouterProtocolError("DeepSeek lieferte keinen intent")
        intent = intent.strip().upper()

        if intent == INTENT_QUESTION:
            if not fact_lines:
                # C-4 auch im off-Pfad: die Modellantwort wird **nicht**
                # übernommen, sonst halluziniert sie einen HA-Wert.
                return _route_question_no_data(transcript)
            response_text = payload.get("response_text")
            if not isinstance(response_text, str) or not response_text.strip():
                raise RouterProtocolError("DeepSeek lieferte keinen response_text")
            return RouteDecision(
                intent=INTENT_QUESTION,
                transcript=transcript,
                response_text=response_text.strip(),
                source="deepseek",
                raw={
                    **dict(payload),
                    CATALOG_SELECTION_KEY: selection.as_dict(),
                    FACTS_AVAILABLE_KEY: True,
                },
            )

        if intent != INTENT_COMMAND:
            raise RouterProtocolError(f"unbekannter DeepSeek-intent {intent!r}")

        # COMMAND: dieselbe Validierung wie im Jev-Pfad (gemeinsame Logik).
        entity_id = payload.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in selection.entities:
            raise AllowlistViolationError(
                f"entity_id {entity_id!r} nicht im gesendeten Katalog "
                f"(PLAN:459/E17)"
            )
        service = payload.get("service")
        if not isinstance(service, str) or not service.strip():
            raise RouterProtocolError("DeepSeek lieferte keinen gültigen service")
        domain = entity_domain(entity_id)
        reported_domain = payload.get("domain")
        if (
            isinstance(reported_domain, str)
            and reported_domain.strip()
            and reported_domain.strip() != domain
        ):
            raise RouterProtocolError(
                f"domain {reported_domain!r} passt nicht zu {entity_id!r}"
            )
        # B-1: gleiche Dienst-Schranke wie im Jev-Pfad – erst die Form
        # vereinheitlichen (B-1/P12.T4-4), dann die Erlaubnis prüfen.
        raw_service = service.strip()
        service = _normalize_service_name(raw_service, domain)
        if service not in DOMAIN_SERVICE_CRITERIA.get(
            domain, frozenset()
        ):
            raise RouterProtocolError(
                f"service {raw_service!r} ist für Domain {domain!r} nicht "
                "erlaubt (B-1/DOMAIN_SERVICE_CRITERIA)"
            )
        data = payload.get("service_data") or {}
        # B-3: derselbe Validator wie in Variante A und D – `JEV_MODE=off` darf
        # nicht das schwächste Glied der Kette sein.
        service_data = _apply_typed_service_data(
            domain,
            service,
            data,
            selection.entities.get(entity_id),
            transcript=transcript,
        )
        response_text = payload.get("response_text")
        target_class = payload.get("target_class")
        return RouteDecision(
            intent=INTENT_COMMAND,
            transcript=transcript,
            response_text=response_text if isinstance(response_text, str) else "",
            target_class=target_class if isinstance(target_class, str) else None,
            entity_id=entity_id,
            domain=domain,
            service=service.strip(),
            service_data=service_data,
            source="deepseek",
            raw={**dict(payload), CATALOG_SELECTION_KEY: selection.as_dict()},
        )
