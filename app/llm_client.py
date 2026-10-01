"""LLM-Gateway-Clients (P4.T2, `PLAN.md:457`) – reine Client-Logik.

Auftrag (wörtlich `PLAN.md:457`): **Jev-Client** (httpx ``POST
{LLM_BASE_URL}/systemone``, Request mit ``state`` + ``questions`` (``noul``/
``choice``), Timeout, ``JEV_MODE``-Schalter ``intent``/``gate``/``off``) **und**
**DeepSeek-Client** (OpenAI-kompatibles ``POST {LLM_BASE_URL}/chat/completions``,
``temperature=0``, JSON-Modus).

**E1/Variante A (PLAN §1.3):** Jev ist ein **Intent-Klassifizierer**, kein
Textgenerator.  Ein ``systemone``-Call beantwortet pro Frage einen **Wert +
Score** – kein Freitext, kein JSON-Schema-Output:

* ``intent`` (``type=noul``) ⇒ ``{"type":"noul","noul":0.97}`` – die
  Wahrscheinlichkeit „will der Nutzer ein Gerät steuern?" (⇒ ``NoulResult``,
  ``value`` = ``score >= 0.5``).
* ``target`` (``type=choice``) ⇒ ``{"type":"choice","choice":"light",
  "confidence":1,"probabilities":{…}}`` (⇒ ``ChoiceResult``).

Die **verifizierte Live-Antwort** (P0.T2, STATE §3) ist:
``{"model":"jev-1.13","answers":{"intent":{"type":"noul","noul":0.97},
"target":{"type":"choice","choice":"light","confidence":1,"probabilities":{…}}},
"usage":{…}}``.  Der Parser liest genau diese Form; die Request-Fragen
(``noul``/``choice``, ``criteria``) sind wörtlich PLAN §1.1.

**``JEV_MODE``-Semantik (E1):**

* ``intent`` (A, Default) – **zwei** Fragen: ``intent`` + ``target``.
* ``gate`` (B) – **nur** ``intent`` (COMMAND-vs-QUESTION); das Entity-/Service-
  Mapping macht dann DeepSeek.
* ``off`` (C) – Jev wird **nicht** aufgerufen: ``JevClient.classify`` wirft
  ``JevDisabledError`` **ohne** jeden HTTP-Aufruf.

**DeepSeek (E1/A):** Entity-Mapping + Antworten über ``POST
{LLM_BASE_URL}/chat/completions`` (OpenAI-kompatibel, ``temperature=0``,
optional ``response_format={"type":"json_object"}``).  Antwort = ``choices[0].
message.content`` (``ChatResult``).

**Entscheidung E57 – kein ``openai``-Paket (Option B).**  ``PLAN.md:457`` nennt
``openai.AsyncOpenAI``, aber **weder ``requirements.txt`` noch PLAN §4 führen
``openai``** – und der Vertrag ist ein einzelner, OpenAI-kompatibler
REST-Endpunkt (POST + Bearer + JSON-Body), ohne Streaming/Tools.
``openai`` würde ~5 Fremd-Abhängigkeiten und einen Image-Neubau in P6 kosten,
ohne funktionalen Mehrwert.  Deshalb hier **httpx** (bereits Runtime-Dependency
für HA/LLM, PLAN §4/P1.T5) statt der SDK.  Siehe ``STATE.md`` §4/E57.

**E91 (Modellidentität lokal beweisbar):** ``result.model`` (die vom Server
**gemeldete** ID) wird in **allen drei** Jev-Aufrufwegen in die bestehende
``DEBUG``-Zeile geschrieben (``model=jev-1.13``).  Weicht die gemeldete ID von
der angefragten ab – oder fehlt sie – gibt es **eine** ``WARNING`` je
Wert-Paar (Flooding-Schutz pro Client).  Damit ist die Identität **lokal**
belegbar; der frühere Nachweis über einen Gateway-Aufruf mit serverseitigen
Headern war **unzulässiger clientseitiger „Beweis"** (kein Betriebsweg).

**E92 (``service_data`` v2, Variante D):** Jev #2 bekommt im **selben** Call
eine **dritte** Frage ``needs_param`` (``noul``) – *muss für diesen Befehl ein
Zahlenwert angegeben werden?*.  Damit bleibt die Trennung sauber: **Jev**
entscheidet *was/welches Gerät/welcher Dienst* und **ob** eine Zahl nötig ist;
**DeepSeek** liefert nur den Zahlenwert.  Jev trägt weiterhin **keinen**
Zahlenwert (ein ``noul`` kann keinen transportieren) und erzeugt weiterhin
**keinen** Text.  Die Auswertung der Antwort sitzt im Router
(``app/router.py``), nicht hier.

**Reine Client-Logik:** Importiert **nichts** aus ``app.router``/``app.pipeline``
– nur stdlib, ``httpx``, ``app.config`` und ``app.logger``.  Secrets (API-Key)
werden **nie** geloggt oder in Exceptions ausgegeben.

**Nicht** hier: Tests (``tests/test_llm_client.py``, P4.T3) und der Router
(``app/router.py``, P4.T4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional, Sequence

import httpx

from app.config import JEV_MODES, settings
from app.logger import get_logger

__all__ = [
    "LlmClientError",
    "LlmConfigError",
    "LlmUnavailableError",
    "LlmProtocolError",
    "JevDisabledError",
    "NoulResult",
    "ChoiceResult",
    "SystemOneResult",
    "EntityChoiceResult",
    "ChatResult",
    "JevClient",
    "DeepSeekClient",
    "build_systemone_questions",
    "build_systemone_request",
    "build_intent_only_questions",
    "build_entity_questions",
    "MODEL_ID_UNKNOWN",
    "model_id_for_log",
    "warn_on_model_mismatch",
    "parse_systemone",
    "parse_systemone_intent",
    "parse_systemone_entity",
    "parse_chat_completion",
    "LLM_BASE_URL",
    "JEV_MODEL",
    "JEV_TIMEOUT",
    "JEV_MODE",
    "JEV_SYSTEMONE_PATH",
    "JEV_SYSTEMONE_URL",
    "JEV_INTENT_INSTRUCTIONS",
    "JEV_TARGET_INSTRUCTIONS",
    "JEV_TARGET_CRITERIA",
    "JEV_ENTITY_TARGET_INSTRUCTIONS",
    "JEV_ENTITY_SERVICE_INSTRUCTIONS",
    "JEV_ENTITY_NEEDS_PARAM_INSTRUCTIONS",
    "JEV_TARGET_NONE_KEY",
    "JEV_TARGET_NONE_LABEL",
    "JEV_SERVICE_CRITERIA",
    "JEV_MAX_CHOICES",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TIMEOUT",
    "DEEPSEEK_SYSTEM_PROMPT",
    "DEEPSEEK_CHAT_PATH",
    "DEEPSEEK_CHAT_URL",
    "DEFAULT_TEMPERATURE",
    "JSON_RESPONSE_FORMAT",
]

_LOG: Final = get_logger("llm_client")

# ── Verbindungs-/Modell-Parameter (alle aus `app.config`, PLAN §4) ──────────
#: Basis-URL des Gateways; Jev **und** DeepSeek hängen ihren Pfad an (PLAN:457).
LLM_BASE_URL: Final[str] = settings.llm_base_url
#: Modell-ID des Jev-Klassifizierers.
JEV_MODEL: Final[str] = settings.jev_model
#: HTTP-Timeout je Jev-Aufruf (PLAN §4: 8,0 s).
JEV_TIMEOUT: Final[float] = settings.jev_timeout
#: Default-``JEV_MODE`` (E1: ``intent``).
JEV_MODE: Final[str] = settings.jev_mode
#: Pfad des SystemOne-Endpunkts, relativ zu :data:`LLM_BASE_URL`.
JEV_SYSTEMONE_PATH: Final[str] = "/systemone"
#: Vollständige Jev-URL (PLAN:457/§1.1: ``{LLM_BASE_URL}/systemone``).
JEV_SYSTEMONE_URL: Final[str] = LLM_BASE_URL.rstrip("/") + JEV_SYSTEMONE_PATH

#: Fragen-Vorgaben – **wörtlich PLAN §1.1**, nicht erfunden.
JEV_INTENT_INSTRUCTIONS: Final[str] = "Will der Nutzer ein Gerät steuern?"
JEV_TARGET_INSTRUCTIONS: Final[str] = "Welche Geräteklasse?"
JEV_TARGET_CRITERIA: Final[Mapping[str, str]] = {
    "light": "Licht",
    "switch": "Schalter",
    "climate": "Heizung/Klima",
    "media_player": "Musik",
    "cover": "Rollo/Jalousie",
    "scene": "Szene",
    "script": "Skript",
    "other": "Sonstiges",
}

# ── Variante D (E90): 2-stufiges Jev ──────────────────────────────────────
#: **Jev #1** fragt **nur** ``intent`` (COMMAND vs. TEXT) – State = Transkript.
#: **Jev #2** fragt ``target`` **und** ``service`` als ``choice``.
JEV_ENTITY_TARGET_INSTRUCTIONS: Final[str] = "Welches Zielgerät?"
JEV_ENTITY_SERVICE_INSTRUCTIONS: Final[str] = "Welcher Befehl?"
#: **E92:** dritte Frage im **selben** Jev-#2-Call – ``noul``, also eine reine
#: Ja/Nein-Frage **ohne** Zahlenwert.  Jev entscheidet damit *ob* ein Zahlenwert
#: gebraucht wird; **welchen** Wert es ist, bleibt beim DeepSeek-Aufruf
#: (Zahl extrahieren).  Dadurch entsteht **kein** zusätzlicher Round-Trip – die
#: v1-Lücke (Entity korrekt, „40 %" verloren) wird geschlossen, ohne Jev einen
#: Zahlenwert aufzubürden, den ein ``noul`` strukturell nicht transportiert.
JEV_ENTITY_NEEDS_PARAM_INSTRUCTIONS: Final[str] = (
    "Muss für diesen Befehl ein Zahlenwert angegeben werden "
    "(z. B. Helligkeit, Temperatur, Lautstärke)?"
)
#: Pflicht-Option „kein passendes Gerät".  **Ohne** sie liefert Jev bei
#: fehlender Zielentität trotzdem eine **falsche** Entity (Konfidenz 0.55–0.60,
#: Machbarkeits-Smoke E90) – nur ``intent`` ist gegated, **nicht** ``target``.
JEV_TARGET_NONE_KEY: Final[str] = "none"
JEV_TARGET_NONE_LABEL: Final[str] = "kein passendes Gerät"
#: Variante-D-v1-Dienste (**kein** ``service_data``, E90-Grenze).
JEV_SERVICE_CRITERIA: Final[Mapping[str, str]] = {
    "turn_on": "einschalten",
    "turn_off": "ausschalten",
    "toggle": "umschalten",
}
#: Harte Optionsgrenze der SystemOne-``choice``-Frage: 256 ⇒ HTTP 400
#: („Too many choices. Must have at most 255 choices.", E90).
JEV_MAX_CHOICES: Final[int] = 255

#: DeepSeek-Basis-URL: PLAN:457 verlangt ``base_url=LLM_BASE_URL``; der Pfad
#: ``/chat/completions`` wird angehängt.  Ergebnis ist identisch mit
#: ``settings.deepseek_base_url`` (PLAN §4, bereits vollständig).
DEEPSEEK_MODEL: Final[str] = settings.deepseek_model
#: HTTP-Timeout je DeepSeek-Aufruf (PLAN §4: 12,0 s).
DEEPSEEK_TIMEOUT: Final[float] = settings.deepseek_timeout
#: System-Prompt aus den Settings (PLAN §4).
DEEPSEEK_SYSTEM_PROMPT: Final[str] = settings.deepseek_system_prompt
#: Pfad des Chat-Endpunkts, relativ zu :data:`LLM_BASE_URL`.
DEEPSEEK_CHAT_PATH: Final[str] = "/chat/completions"
#: Vollständige DeepSeek-URL (== ``settings.deepseek_base_url``).
DEEPSEEK_CHAT_URL: Final[str] = LLM_BASE_URL.rstrip("/") + DEEPSEEK_CHAT_PATH

#: ``temperature=0`` (PLAN:457) – deterministisches Routing.
DEFAULT_TEMPERATURE: Final[float] = 0.0
#: JSON-Modus (PLAN §1.2/E1/A): erzwungene JSON-Antwort.
JSON_RESPONSE_FORMAT: Final[Mapping[str, str]] = {"type": "json_object"}


# ── Fehler ────────────────────────────────────────────────────────────────
class LlmClientError(Exception):
    """Basisklasse aller LLM-Client-Fehler (auch Programmierfehler)."""


class LlmConfigError(LlmClientError):
    """Ungültige Konfiguration/Argumente (fehlender Key, falscher Modus …)."""


class LlmUnavailableError(LlmClientError):
    """LLM-Gateway nicht erreichbar oder hat mit Fehler geantwortet.

    Umfasst Timeout, Transport-Fehler, HTTP ≥ 400 und „nicht gestartet".
    Der technische Grund steht in ``detail`` (und als ``__cause__``) – **nie**
    der API-Key.
    """

    def __init__(self, detail: Optional[str] = None) -> None:
        super().__init__(detail or "LLM-Gateway nicht erreichbar.")
        self.detail = detail


class LlmProtocolError(LlmClientError):
    """Antwort verletzt den verifizierten Vertrag (JSON/Schema)."""


class JevDisabledError(LlmClientError):
    """``JEV_MODE=off`` (E1/C) – Jev wurde absichtlich **nicht** aufgerufen."""


# ── Ergebnis-Datentypen ───────────────────────────────────────────────────
@dataclass(frozen=True)
class NoulResult:
    """Antwort einer ``noul``-Frage: Wert + Score (E1/PLAN §1.1).

    ``score`` ist die vom Modell gelieferte Wahrscheinlichkeit; ``value``
    interpretiert sie als Boolesche Entscheidung (``score >= 0.5``).  Der
    Router (P4.T4) kann stattdessen ``score`` gegen ``router_confidence_gate``
    (E3) prüfen.
    """

    score: float
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def value(self) -> bool:
        """``True``, wenn die Frage als positiv beantwortet gilt (≥ 0.5)."""
        return self.score >= 0.5

    def at_least(self, threshold: float) -> bool:
        """``True``, wenn ``score`` die Schwelle erreicht/überschreitet."""
        return self.score >= threshold


@dataclass(frozen=True)
class ChoiceResult:
    """Antwort einer ``choice``-Frage: Gewählter Wert + Score (Konfidenz)."""

    value: str
    score: float
    probabilities: Mapping[str, float] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemOneResult:
    """Ergebnis eines ``systemone``-Calls (E1/A).

    ``intent`` ist immer vorhanden; ``target`` nur bei ``JEV_MODE=intent``
    (bei ``gate`` fragt der Client die ``choice``-Frage gar nicht).
    """

    intent: NoulResult
    target: Optional[ChoiceResult] = None
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityChoiceResult:
    """Ergebnis des **zweiten** Jev-Calls in Variante D (E90/E92).

    Zwei ``choice``-Antworten und eine ``noul``-Antwort aus **einem**
    ``systemone``-Call: das Zielgerät (``target``, Wert = ``entity_id`` oder
    :data:`JEV_TARGET_NONE_KEY`), der Dienst (``service``) und – seit E92 – die
    reine **Ja/Nein**-Frage, ob dieser Befehl überhaupt einen Zahlenwert
    braucht (``needs_param``).  Jev erzeugt **keinen** Text und **keinen**
    Zahlenwert; die Bestätigung baut der Router aus einem Template, die Zahl
    extrahiert der DeepSeek-Aufruf des Routers.

    ``needs_param is None`` ⇒ Jev hat die Frage nicht beantwortet.  Der Router
    behandelt das **konservativ** wie „nein" (kein ``service_data``, v1-Verhalten)
    – eine fehlende Antwort darf keinen Turn unerwartet in einen Param-Pfad
    schicken.
    """

    target: ChoiceResult
    service: ChoiceResult
    needs_param: Optional[NoulResult] = None
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResult:
    """Ergebnis eines DeepSeek-Chat-Calls (OpenAI-kompatibel)."""

    content: str
    model: str = ""
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


# ── Request-Bau + Parser (reine Funktionen, ohne Netz) ────────────────────
def _normalize_mode(mode: object) -> str:
    """``JEV_MODE`` normalisieren/validieren (E1: intent/gate/off)."""
    if not isinstance(mode, str):
        raise LlmConfigError(f"JEV_MODE muss ein str sein, ist {mode!r}")
    normalized = mode.strip().lower()
    if normalized not in JEV_MODES:
        raise LlmConfigError(
            f"JEV_MODE muss eines von {JEV_MODES} sein (E1), ist {mode!r}"
        )
    return normalized


def build_systemone_questions(mode: str = JEV_MODE) -> dict[str, Any]:
    """``questions``-Block für ``/systemone`` aus dem ``JEV_MODE`` bauen.

    * ``intent`` ⇒ ``intent`` (noul) **und** ``target`` (choice).
    * ``gate`` ⇒ **nur** ``intent`` (noul).
    * ``off``  ⇒ ``{}`` (Jev wird nicht aufgerufen; ``classify`` wirft vorher).
    """
    mode = _normalize_mode(mode)
    if mode == "off":
        return {}
    questions: dict[str, Any] = {
        "intent": {"type": "noul", "instructions": JEV_INTENT_INSTRUCTIONS}
    }
    if mode == "intent":
        questions["target"] = {
            "type": "choice",
            "instructions": JEV_TARGET_INSTRUCTIONS,
            "criteria": dict(JEV_TARGET_CRITERIA),
        }
    return questions


def build_intent_only_questions() -> dict[str, Any]:
    """**Jev #1** in Variante D (E90): **nur** ``intent`` (``noul``).

    Der ``state`` besteht dabei aus dem **Transkript allein** – ohne
    Entity-Liste (die braucht erst Jev #2).
    """
    return {"intent": {"type": "noul", "instructions": JEV_INTENT_INSTRUCTIONS}}


def build_entity_questions(criteria: Mapping[str, str]) -> dict[str, Any]:
    """**Jev #2** in Variante D (E90/E92): ``target`` + ``service`` + ``needs_param``.

    ``criteria`` muss die Zieloptionen enthalten; die Pflicht-Option
    :data:`JEV_TARGET_NONE_KEY` wird hier ergänzt, wenn sie fehlt (ohne sie
    schaltet Jev bei fehlender Zielentität falsch – E90).

    **E92:** die dritte Frage ``needs_param`` (``noul``) läuft im **selben**
    Call – **kein** zusätzlicher Round-Trip.  Sie sagt nur, **ob** ein
    Zahlenwert gebraucht wird (Helligkeit/Temperatur/Lautstärke); der Zahlenwert
    selbst wird nur dann und nur von DeepSeek extrahiert.
    """
    if not isinstance(criteria, Mapping) or not criteria:
        raise LlmConfigError("criteria muss ein nicht-leeres Mapping sein")
    target_criteria = {str(key): str(val) for key, val in criteria.items()}
    target_criteria.setdefault(JEV_TARGET_NONE_KEY, JEV_TARGET_NONE_LABEL)
    return {
        "target": {
            "type": "choice",
            "instructions": JEV_ENTITY_TARGET_INSTRUCTIONS,
            "criteria": target_criteria,
        },
        "service": {
            "type": "choice",
            "instructions": JEV_ENTITY_SERVICE_INSTRUCTIONS,
            "criteria": dict(JEV_SERVICE_CRITERIA),
        },
        "needs_param": {
            "type": "noul",
            "instructions": JEV_ENTITY_NEEDS_PARAM_INSTRUCTIONS,
        },
    }


def build_systemone_request(
    state: str,
    *,
    mode: str = JEV_MODE,
    questions: Optional[Mapping[str, Any]] = None,
    model: str = JEV_MODEL,
) -> dict[str, Any]:
    """Request-Body für ``POST /systemone`` bauen (PLAN §1.1).

    ``state`` = Transkript + Entity-Liste; ``questions`` überschreibt die aus
    ``mode`` abgeleiteten Standardfragen.
    """
    if not isinstance(state, str) or not state.strip():
        raise LlmConfigError("state muss ein nicht-leerer str sein")
    if questions is None:
        questions = build_systemone_questions(mode)
    if not isinstance(questions, Mapping):
        raise LlmConfigError("questions muss ein Mapping sein")
    return {"model": model, "state": state, "questions": dict(questions)}


def _require_number(value: object, field_name: str) -> float:
    """Zahl erzwingen (``bool`` ist keine gültige Zahl im Sinne des Vertrags)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LlmProtocolError(f"{field_name} muss eine Zahl sein, ist {value!r}")
    return float(value)


def parse_systemone(
    payload: Mapping[str, Any], *, mode: str = JEV_MODE
) -> SystemOneResult:
    """Antwort von ``/systemone`` parsen (Wert + Score, E1).

    Vertrag (P0.T2 verifiziert): ``answers.intent.noul`` (Zahl) und bei
    ``JEV_MODE=intent`` ``answers.target.choice`` + ``.confidence``.
    """
    mode = _normalize_mode(mode)
    if not isinstance(payload, Mapping):
        raise LlmProtocolError(f"Antwort ist kein Objekt, sondern {type(payload)!r}")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise LlmProtocolError("Antwort ohne 'answers'-Objekt")

    intent_raw = answers.get("intent")
    if not isinstance(intent_raw, Mapping):
        raise LlmProtocolError("Antwort ohne 'answers.intent'")
    intent = NoulResult(
        score=_require_number(intent_raw.get("noul"), "answers.intent.noul"),
        raw=dict(intent_raw),
    )

    target: Optional[ChoiceResult] = None
    if mode == "intent":
        target_raw = answers.get("target")
        if not isinstance(target_raw, Mapping):
            raise LlmProtocolError("Antwort ohne 'answers.target' (JEV_MODE=intent)")
        choice = target_raw.get("choice")
        if not isinstance(choice, str) or not choice:
            raise LlmProtocolError("answers.target.choice fehlt/leer")
        probabilities: dict[str, float] = {}
        raw_probs = target_raw.get("probabilities")
        if isinstance(raw_probs, Mapping):
            probabilities = {
                str(key): float(val)
                for key, val in raw_probs.items()
                if isinstance(val, (int, float)) and not isinstance(val, bool)
            }
        target = ChoiceResult(
            value=choice,
            score=_require_number(
                target_raw.get("confidence"), "answers.target.confidence"
            ),
            probabilities=probabilities,
            raw=dict(target_raw),
        )

    usage = payload.get("usage")
    return SystemOneResult(
        intent=intent,
        target=target,
        model=str(payload.get("model", "")),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


def _answers_of(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """``answers``-Objekt prüfen und zurückgeben (gemeinsame Parser-Basis)."""
    if not isinstance(payload, Mapping):
        raise LlmProtocolError(f"Antwort ist kein Objekt, sondern {type(payload)!r}")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise LlmProtocolError("Antwort ohne 'answers'-Objekt")
    return answers


def _parse_noul_answer(answers: Mapping[str, Any], key: str = "intent") -> NoulResult:
    """``answers.<key>.noul`` als :class:`NoulResult` lesen."""
    raw = answers.get(key)
    if not isinstance(raw, Mapping):
        raise LlmProtocolError(f"Antwort ohne 'answers.{key}'")
    return NoulResult(
        score=_require_number(raw.get("noul"), f"answers.{key}.noul"),
        raw=dict(raw),
    )


def _parse_choice_answer(answers: Mapping[str, Any], key: str) -> ChoiceResult:
    """``answers.<key>.choice``/``.confidence`` als :class:`ChoiceResult` lesen."""
    raw = answers.get(key)
    if not isinstance(raw, Mapping):
        raise LlmProtocolError(f"Antwort ohne 'answers.{key}'")
    choice = raw.get("choice")
    if not isinstance(choice, str) or not choice:
        raise LlmProtocolError(f"answers.{key}.choice fehlt/leer")
    probabilities: dict[str, float] = {}
    raw_probs = raw.get("probabilities")
    if isinstance(raw_probs, Mapping):
        probabilities = {
            str(prob_key): float(val)
            for prob_key, val in raw_probs.items()
            if isinstance(val, (int, float)) and not isinstance(val, bool)
        }
    return ChoiceResult(
        value=choice,
        score=_require_number(raw.get("confidence"), f"answers.{key}.confidence"),
        probabilities=probabilities,
        raw=dict(raw),
    )


def parse_systemone_intent(payload: Mapping[str, Any]) -> SystemOneResult:
    """**Jev #1** in Variante D (E90) parsen: nur ``intent``, **kein** ``target``.

    Anders als :func:`parse_systemone` (Variante A) wird hier **keine**
    ``target``-Antwort erwartet (die Frage wird nicht gestellt).
    """
    answers = _answers_of(payload)
    usage = payload.get("usage")
    return SystemOneResult(
        intent=_parse_noul_answer(answers),
        target=None,
        model=str(payload.get("model", "")),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


def parse_systemone_entity(payload: Mapping[str, Any]) -> EntityChoiceResult:
    """**Jev #2** in Variante D (E90/E92) parsen.

    ``target`` und ``service`` sind **Pflicht** (wie in E90).  ``needs_param``
    (E92) ist **optional**: beantwortet Jev die dritte ``noul``-Frage nicht,
    bleibt das Feld ``None`` und der Router bleibt im v1-Verhalten (kein
    ``service_data``).  Fehlende Antwort ⇒ **kein** Absturz und **kein**
    automatischer Param-Pfad.
    """
    answers = _answers_of(payload)
    usage = payload.get("usage")
    needs_param: Optional[NoulResult] = None
    if isinstance(answers.get("needs_param"), Mapping):
        needs_param = _parse_noul_answer(answers, "needs_param")
    return EntityChoiceResult(
        target=_parse_choice_answer(answers, "target"),
        service=_parse_choice_answer(answers, "service"),
        needs_param=needs_param,
        model=str(payload.get("model", "")),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


def parse_chat_completion(payload: Mapping[str, Any]) -> ChatResult:
    """OpenAI-kompatible Chat-Antwort parsen (``choices[0].message.content``)."""
    if not isinstance(payload, Mapping):
        raise LlmProtocolError(f"Antwort ist kein Objekt, sondern {type(payload)!r}")
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
        raise LlmProtocolError("Antwort ohne 'choices'-Liste")
    if not choices:
        raise LlmProtocolError("'choices' ist leer")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise LlmProtocolError("choices[0] ist kein Objekt")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LlmProtocolError("choices[0].message fehlt")
    content = message.get("content")
    if not isinstance(content, str):
        raise LlmProtocolError("choices[0].message.content fehlt")
    reasoning = message.get("reasoning_content")
    usage = payload.get("usage")
    finish = first.get("finish_reason")
    return ChatResult(
        content=content,
        model=str(payload.get("model", "")),
        finish_reason=finish if isinstance(finish, str) else None,
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
        usage=dict(usage) if isinstance(usage, Mapping) else {},
        raw=dict(payload),
    )


# ── Modellidentität (E91) ─────────────────────────────────────────────────
#: Platzhalter in der Debug-Zeile, wenn der Server **keine** Modell-ID meldet.
MODEL_ID_UNKNOWN: Final[str] = "?"


def model_id_for_log(reported: object) -> str:
    """Gemeldete Modell-ID für die Debug-Zeile – ``?`` bei fehlender ID.

    „Fehlend" heißt: ``None``, JSON-``null`` (die Parser liefern dann den
    String ``"None"``), leer oder reine Whitespace.  Der **Rückgabewort**
    behält jede Whitespace und jede Schreibweise: ``" jev-1.13"`` bleibt
    ``" jev-1.13"`` und ist damit ein **Befund**, kein Schönheitsfehler
    (E91: exakter Vergleich, kein stilles Case-Folding).
    """
    if reported is None:
        return MODEL_ID_UNKNOWN
    text = str(reported)
    if not text.strip() or text.strip() == "None":
        return MODEL_ID_UNKNOWN
    return text


def warn_on_model_mismatch(
    requested: str, reported: str, *, seen: set[tuple[str, str]]
) -> bool:
    """E91: Modell-Abweichung **einmal pro Wert-Paar** als ``WARNING`` loggen.

    Bedingung: ``reported != requested`` – **exakt**.  Damit ist auch eine
    fehlende ID (``?``) eine Abweichung, denn angefragt wurde eine konkrete
    Modell-ID.  Der Vergleich ist absichtlich strikt: ``jev-1.13`` ≠
    ``JEV-1.13`` ≠ ``"jev-1.13 "``.

    **Flooding-Schutz:** ``seen`` hält die bereits gemeldeten
    ``(angefragt, gemeldet)``-Paare; jeder Client trägt seine eigene Menge
    ⇒ höchstens **eine** Warnung je Wert-Paar.  Im Manager baut der Router
    genau **eine** ``JevClient``-Instanz (`app/router.py:_build_jev_client`),
    also faktisch höchstens 1× je Abweichungswert pro Prozess.

    Die Message nennt nur die beiden Modell-IDs – **kein** API-Key, **keine**
    URL (der Pfad kann im Key stecken).  Rückgabe: ``True``, wenn gewarnt
    wurde, sonst ``False``.
    """
    if reported == requested:
        return False
    key = (requested, reported)
    if key in seen:
        return False
    seen.add(key)
    _LOG.warning(
        "Jev-Modell weicht von der Anfrage ab: angefragt=%s, gemeldet=%s "
        "(E91; einmal je Wert)",
        requested,
        reported,
    )
    return True


# ── Gemeinsame HTTP-Lifecycle-Basis ───────────────────────────────────────
class _LlmHttpClient:
    """Basis: ``httpx.AsyncClient``-Lifecycle, Bearer-Auth, Fehlermapping.

    ``start()``/``stop()`` sind idempotent; ein per ``client=`` injizierter
    ``httpx.AsyncClient`` (MockTransport-Tests) wird übernommen und bekommt in
    ``start()`` denselben Bearer-Header.  Der Key wird **nie** geloggt.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str],
        timeout: float,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        #: Der Key lebt nur im Speicher/Header – **kein** Log, **keine** Exception.
        self._api_key: str = api_key if api_key is not None else settings.llm_api_key
        self.timeout: float = float(timeout)
        self._provided_client: Optional[httpx.AsyncClient] = client
        self._client: Optional[httpx.AsyncClient] = client
        self._started: bool = False

    @property
    def started(self) -> bool:
        """True zwischen ``start()`` und ``stop()``."""
        return self._started

    @property
    def client(self) -> Optional[httpx.AsyncClient]:
        """Der aktive ``httpx.AsyncClient`` (oder ``None``)."""
        return self._client

    async def start(self) -> None:
        """Client aufbauen (falls nötig) und Bearer-Auth setzen. Idempotent."""
        if self._started:
            return
        if self._client is None or self._client.is_closed:
            if self._provided_client is not None and not self._provided_client.is_closed:
                self._client = self._provided_client
            else:
                self._client = self._build_client()
        # Bearer-Auth auch für injizierte Clients garantieren (idempotent).
        self._client.headers["Authorization"] = f"Bearer {self._api_key}"
        self._started = True

    async def stop(self) -> None:
        """Client schließen. Idempotent und mehrfach aufrufbar."""
        client = self._client
        self._client = None
        self._started = False
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - Close ist Best-effort.
                _LOG.warning("LLM-AsyncClient konnte nicht geschlossen werden: %r", exc)

    async def __aenter__(self) -> _LlmHttpClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _post_json(self, url: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """``POST`` + JSON-Body; mappt alle Fehler auf ``LlmUnavailableError``."""
        if not self._api_key:
            raise LlmConfigError(
                "LLM_API_KEY ist nicht gesetzt (E20/E41) – Wert gehört in .env"
            )
        client = self._client
        if client is None:
            raise LlmUnavailableError("Client nicht gestartet")
        try:
            response = await client.post(url, json=dict(body))
        except httpx.TimeoutException as exc:
            raise LlmUnavailableError(
                f"Timeout nach {self.timeout:g}s auf {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LlmUnavailableError(f"HTTP {response.status_code} auf {url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmProtocolError("Antwort ist kein gültiges JSON") from exc
        if not isinstance(payload, Mapping):
            raise LlmProtocolError(f"Antwort ist kein Objekt, sondern {type(payload)!r}")
        return payload

    def _build_client(self) -> httpx.AsyncClient:
        """``httpx.AsyncClient`` mit Bearer-Auth und Timeout bauen."""
        return httpx.AsyncClient(timeout=self.timeout)


# ── Jev (SystemOne) ───────────────────────────────────────────────────────
class JevClient(_LlmHttpClient):
    """Jev-Intent-Klassifizierer (E1/A, ``POST /systemone``).

    ``JEV_MODE`` steuert die Fragen (s. Modul-Docstring); bei ``off`` wirft
    ``classify()`` ``JevDisabledError`` **ohne** HTTP-Aufruf.
    """

    def __init__(
        self,
        *,
        base_url: str = LLM_BASE_URL,
        api_key: Optional[str] = None,
        model: str = JEV_MODEL,
        timeout: float = JEV_TIMEOUT,
        mode: str = JEV_MODE,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(api_key=api_key, timeout=timeout, client=client)
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.mode: str = _normalize_mode(mode)
        self.systemone_url: str = self.base_url + JEV_SYSTEMONE_PATH
        #: Bereits gewarnte ``(angefragt, gemeldet)``-Paare (E91) – Flooding-Schutz.
        self._warned_models: set[tuple[str, str]] = set()

    @property
    def enabled(self) -> bool:
        """True, wenn ``JEV_MODE`` nicht ``off`` ist."""
        return self.mode != "off"

    async def start(self) -> None:
        await super().start()
        if self._started:
            _LOG.info(
                "Jev-Client bereit (mode=%s, model=%s, url=%s)",
                self.mode,
                self.model,
                self.systemone_url,
            )

    def build_questions(self) -> dict[str, Any]:
        """Fragen für den aktuellen ``JEV_MODE`` (s. ``build_systemone_questions``)."""
        return build_systemone_questions(self.mode)

    async def classify(
        self,
        state: str,
        *,
        questions: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
    ) -> SystemOneResult:
        """``POST /systemone`` – Intent (+ Zielklasse) klassifizieren.

        Bei ``JEV_MODE=off`` wird **kein** HTTP-Aufruf gemacht; es fliegt
        ``JevDisabledError``.

        **E91 (Modellidentität lokal beweisbar):** die Debug-Zeile führt die
        **serverseitig gemeldete** Modell-ID (``model=``) mit; weicht sie von
        der angefragten ab (oder fehlt sie), gibt es **eine** ``WARNING`` je
        Wert – mehr Info, **kein** anderes Verhalten.
        """
        if self.mode == "off":
            raise JevDisabledError(
                "JEV_MODE=off (E1/C): Jev wird nicht aufgerufen."
            )
        requested_model = model or self.model
        body = build_systemone_request(
            state,
            mode=self.mode,
            questions=questions,
            model=requested_model,
        )
        payload = await self._post_json(self.systemone_url, body)
        result = parse_systemone(payload, mode=self.mode)
        reported_model = model_id_for_log(result.model)
        _LOG.debug(
            "Jev: intent=%.3f target=%s model=%s",
            result.intent.score,
            result.target.value if result.target else None,
            reported_model,
        )
        warn_on_model_mismatch(
            requested_model, reported_model, seen=self._warned_models
        )
        return result

    async def classify_intent(self, state: str) -> SystemOneResult:
        """**Jev #1** in Variante D (E90): nur ``intent`` (COMMAND vs. TEXT).

        ``state`` ist das **Transkript allein** (ohne Entity-Liste).  Bei
        ``JEV_MODE=off`` kein HTTP-Aufruf ⇒ ``JevDisabledError``.

        **E91:** wie :meth:`classify` – gemeldete Modell-ID in der Debug-Zeile,
        Abweichung/Fehlen ⇒ **eine** ``WARNING`` je Wert.
        """
        if self.mode == "off":
            raise JevDisabledError(
                "JEV_MODE=off (E1/C): Jev wird nicht aufgerufen."
            )
        body = build_systemone_request(
            state,
            mode=self.mode,
            questions=build_intent_only_questions(),
            model=self.model,
        )
        payload = await self._post_json(self.systemone_url, body)
        result = parse_systemone_intent(payload)
        reported_model = model_id_for_log(result.model)
        _LOG.debug(
            "Jev #1: intent=%.3f model=%s", result.intent.score, reported_model
        )
        warn_on_model_mismatch(self.model, reported_model, seen=self._warned_models)
        return result

    async def classify_entity(
        self, state: str, criteria: Mapping[str, str]
    ) -> EntityChoiceResult:
        """**Jev #2** in Variante D (E90): ``target`` + ``service`` (choice).

        ``state`` = Entity-Liste + Transkript; ``criteria`` = Zieloptionen
        (Entity-IDs → Anzeigenamen); die Pflicht-Option ``none`` ergänzt
        :func:`build_entity_questions`.  Bei ``JEV_MODE=off`` kein HTTP-Aufruf.

        **E92:** der Call stellt zusätzlich die ``noul``-Frage ``needs_param``
        (dritte Frage, **gleicher** Call) – nur die Information, **ob** ein
        Zahlenwert gebraucht wird.  Sie steht in derselben Debug-Zeile wie
        ``target``/``service``/``model=``; **keine** zusätzliche Logzeile.

        **E91:** wie :meth:`classify` – gemeldete Modell-ID in der Debug-Zeile,
        Abweichung/Fehlen ⇒ **eine** ``WARNING`` je Wert.
        """
        if self.mode == "off":
            raise JevDisabledError(
                "JEV_MODE=off (E1/C): Jev wird nicht aufgerufen."
            )
        body = build_systemone_request(
            state,
            mode=self.mode,
            questions=build_entity_questions(criteria),
            model=self.model,
        )
        payload = await self._post_json(self.systemone_url, body)
        result = parse_systemone_entity(payload)
        reported_model = model_id_for_log(result.model)
        # E92: `needs_param` in dieselbe (bestehende) Debug-Zeile – **keine**
        # zusätzliche Logzeile; `model=` (E91) bleibt unverändert enthalten.
        _LOG.debug(
            "Jev #2: target=%s (%.3f) service=%s (%.3f) needs_param=%s (%.3f) "
            "model=%s",
            result.target.value,
            result.target.score,
            result.service.value,
            result.service.score,
            result.needs_param.value if result.needs_param is not None else None,
            result.needs_param.score if result.needs_param is not None else float("nan"),
            reported_model,
        )
        warn_on_model_mismatch(self.model, reported_model, seen=self._warned_models)
        return result


# ── DeepSeek (Chat Completions) ───────────────────────────────────────────
class DeepSeekClient(_LlmHttpClient):
    """DeepSeek-Chat-Client (OpenAI-kompatibel, E1/A).

    Verwendet ``httpx`` statt des ``openai``-Pakets (E57, s. Modul-Docstring).
    ``complete()`` setzt ``temperature=0`` (PLAN:457); ``json_mode=True``
    erzwingt ``response_format={"type":"json_object"}``.
    """

    def __init__(
        self,
        *,
        base_url: str = LLM_BASE_URL,
        api_key: Optional[str] = None,
        model: str = DEEPSEEK_MODEL,
        timeout: float = DEEPSEEK_TIMEOUT,
        system_prompt: str = DEEPSEEK_SYSTEM_PROMPT,
        chat_path: str = DEEPSEEK_CHAT_PATH,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(api_key=api_key, timeout=timeout, client=client)
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.system_prompt: str = system_prompt
        self.chat_path: str = chat_path
        self.chat_url: str = self.base_url + "/" + chat_path.lstrip("/")

    def build_messages(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """``[system, user]``-Nachrichtenliste bauen (System aus Settings)."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise LlmConfigError("prompt muss ein nicht-leerer str sein")
        system = system_prompt if system_prompt is not None else self.system_prompt
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        json_mode: bool = False,
        response_format: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        **extra: Any,
    ) -> ChatResult:
        """``POST /chat/completions`` ausführen und ``ChatResult`` liefern.

        ``json_mode=True`` (oder ein explizites ``response_format``) erzwingt
        die JSON-Antwort (PLAN:457/E1/A).  ``extra`` wird in den Body gemergt.
        """
        if not messages:
            raise LlmConfigError("messages darf nicht leer sein")
        body: dict[str, Any] = {
            "model": model or self.model,
            "temperature": temperature,
            "messages": [dict(message) for message in messages],
        }
        if response_format is None and json_mode:
            response_format = JSON_RESPONSE_FORMAT
        if response_format is not None:
            body["response_format"] = dict(response_format)
        body.update(extra)
        payload = await self._post_json(self.chat_url, body)
        return parse_chat_completion(payload)

    async def complete_prompt(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        temperature: float = DEFAULT_TEMPERATURE,
        json_mode: bool = False,
        response_format: Optional[Mapping[str, Any]] = None,
        **extra: Any,
    ) -> ChatResult:
        """Bequemer Ein-Prompt-Aufruf über :meth:`build_messages`."""
        return await self.complete(
            self.build_messages(prompt, system_prompt=system_prompt),
            temperature=temperature,
            json_mode=json_mode,
            response_format=response_format,
            **extra,
        )

    async def complete_json(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        **extra: Any,
    ) -> Any:
        """Ein-Prompt-Aufruf im JSON-Modus; parst ``content`` als JSON.

        Bei ungültigem JSON ⇒ ``LlmProtocolError`` (der Router validiert das
        geparste Objekt weiter).
        """
        result = await self.complete_prompt(
            prompt,
            system_prompt=system_prompt,
            json_mode=True,
            **extra,
        )
        try:
            return json.loads(result.content)
        except ValueError as exc:
            raise LlmProtocolError("content ist kein gültiges JSON") from exc
