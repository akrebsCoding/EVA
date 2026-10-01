"""Router-Tests für **Variante D** (E90, P7.T4) — Layer **L0/`unit`**.

Prüfling ist die additive Variante D in `app/router.py`; die Clients sind
**injizierte Fakes** (kein `httpx`, kein Socket).  Die Variante A (Default
`variant="class"`) bleibt unberührt und wird in `tests/test_router.py` geprüft.

Abgedeckt (Auftrag P7.T4):
* **COMMAND** — Jev #1 hoch ⇒ Jev #2 liefert Entity+Service ⇒ HA-Service-Call.
* **`none`** — kein HA-Call, Antwort „Keine Geräte gefunden.".
* **target-Konfidenz < Gate** — kein HA-Call (`GATE_REJECTED`).
* **QUESTION** — Jev #1 niedrig ⇒ DeepSeek, **kein** Jev #2.
* **255-Cap-Fallback** — Katalog > 255 ⇒ WARN + Variante A; 255 ⇒ D.
* **Template-Text** — „Okay, <Name> eingeschaltet/ausgeschaltet/umgeschaltet.".
* Allowlist, ungültiger Dienst, Default `variant="class"`, ungültige Variante.

**Prüfwerte sind Literale.**  Deterministisch: kein `sleep`, kein Netz.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import pytest

from app.llm_client import (
    ChoiceResult,
    EntityChoiceResult,
    LlmUnavailableError,
    NoulResult,
    SystemOneResult,
)
from app.router import (
    AllowlistViolationError,
    COMMAND_SCORE_THRESHOLD,
    ConfidenceGateError,
    DEEPSEEK_PARAM_SYSTEM_PROMPT,
    ENTITY_PARAM_ALLOWLIST,
    ENTITY_PARAM_KIND,
    ENTITY_PARAM_RANGES,
    ENTITY_PARAM_RESPONSE_TEMPLATES,
    ENTITY_PARAM_WIRE_SCALE,
    EntityNotFoundError,
    EntityParamMissingError,
    LIGHT_NO_BRIGHTNESS_TEXT,
    NEEDS_PARAM_THRESHOLD,
    Router,
    RouterConfigError,
    RouterProtocolError,
    _format_param_value,
)

pytestmark = pytest.mark.unit

NOT_UNDERSTOOD = "Ich habe dich nicht verstanden."
NO_DEVICES = "Keine Geräte gefunden."
DEEPSEEK_FAIL = "Ich konnte keine Antwort finden."
COMMAND = "COMMAND"
QUESTION = "QUESTION"
ERROR = "ERROR"
GATE = 0.75

CATALOG: dict[str, Any] = {
    "light.wohnzimmer": "Wohnzimmerlicht",
    "switch.steckdose": {"friendly_name": "Steckdose Flur"},
}

QUESTION_PAYLOAD = {"response_text": "Es ist 14:30 Uhr."}
#: Frischer Lese-Wert (P12.T3/C) im `ReadableCache`-Format; `friendly_name`
#: liegt – wie in Home Assistant – **innerhalb** von `attributes`.
def readable_state(entity_id: str, friendly_name: str, value: str, **attributes: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "friendly_name": friendly_name,
            **attributes,
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


#: „Wie warm ist es im Wohnzimmer?" ⇒ passender Temperatur-Sensor.
READABLE_TEMP = {
    "sensor.temp_wohnzimmer": readable_state(
        "sensor.temp_wohnzimmer", "Temperatur Wohnzimmer", "22.4",
        device_class="temperature", unit_of_measurement="\u00b0C",
    )
}
#: „ab wie viel Grad kocht Wasser?" ⇒ passender Wasser-Sensor.
READABLE_WASSER = {
    "sensor.wassertemperatur": readable_state(
        "sensor.wassertemperatur", "Wassertemperatur", "96.4",
        device_class="temperature", unit_of_measurement="\u00b0C",
    )
}
COMMAND_PAYLOAD = {
    "entity_id": "light.wohnzimmer",
    "service": "turn_on",
    "service_data": {},
    "response_text": "A-Text",
}


# ── Fakes ────────────────────────────────────────────────────────────────
class FakeEntityJev:
    """Variante-D-Fake: `classify_intent` (#1) + `classify_entity` (#2)."""

    def __init__(
        self,
        *,
        intent_score: float = 0.95,
        target: str = "light.wohnzimmer",
        target_conf: float = 1.0,
        service: str = "turn_on",
        mode: str = "intent",
        needs_param: Optional[float] = None,
    ) -> None:
        self.mode = mode
        self.intent_score = intent_score
        self.target = target
        self.target_conf = target_conf
        self.service = service
        #: E92: Score der dritten Jev-#2-Frage; ``None`` ⇒ Jev hat nicht
        #: geantwortet (konservativ: v1-Verhalten).
        self.needs_param = needs_param
        self.intent_calls: list[str] = []
        self.entity_calls: list[tuple[str, Mapping[str, str]]] = []
        self.classify_calls: list[str] = []

    async def classify_intent(self, state: str) -> SystemOneResult:
        self.intent_calls.append(state)
        return SystemOneResult(intent=NoulResult(score=self.intent_score), target=None)

    async def classify_entity(
        self, state: str, criteria: Mapping[str, str]
    ) -> EntityChoiceResult:
        self.entity_calls.append((state, dict(criteria)))
        return EntityChoiceResult(
            target=ChoiceResult(value=self.target, score=self.target_conf),
            service=ChoiceResult(value=self.service, score=1.0),
            needs_param=(
                None if self.needs_param is None else NoulResult(score=self.needs_param)
            ),
        )

    async def classify(self, state: str, **kwargs: Any) -> SystemOneResult:
        # Nur für den 255-Cap-Rückfall auf Variante A.
        self.classify_calls.append(state)
        return SystemOneResult(
            intent=NoulResult(score=0.95),
            target=ChoiceResult(value="light", score=1.0),
        )


class FakeDeepSeek:
    """DeepSeek-Fake.

    ``payloads`` (E92) beantwortet die Aufrufe **der Reihe nach** – nötig, weil
    im Param-Pfad ein eigener, fokussierter Aufruf mit eigener Antwort folgt.
    ``error`` lässt jeden Aufruf scheitern (E92-Fehlerpfad).
    """

    def __init__(self, payload: Any, *, payloads: Optional[list[Any]] = None,
                 error: Optional[Exception] = None) -> None:
        self.payload = payload
        self.payloads = list(payloads) if payloads is not None else None
        self.error = error
        self.calls: list[tuple[str, Optional[str]]] = []

    async def complete_json(
        self, prompt: str, *, system_prompt: Optional[str] = None, **extra: Any
    ) -> Any:
        self.calls.append((prompt, system_prompt))
        if self.error is not None:
            raise self.error
        if self.payloads:
            return self.payloads.pop(0)
        return self.payload


class FakeHa:
    """HA-Fake – inkl. **Lese**-Cache (P12.T3/C).

    `readable` bildet den `ReadableCache` ab: ohne ihn antwortet eine Frage mit
    dem ehrlichen C-4-Fallback statt mit einer Modellantwort.
    """

    def __init__(
        self,
        entities: Optional[Mapping[str, Any]] = None,
        readable: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.entities = dict(entities or {})
        self.readable = dict(readable or {})
        self.service_calls: list[tuple[str, str, str, dict[str, Any]]] = []

    async def cached_entities(self) -> dict[str, Any]:
        return dict(self.entities)

    async def readable_snapshot(self) -> dict[str, Any]:
        return dict(self.readable)

    async def refresh(self) -> int:
        return len(self.entities)

    async def call_service(
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> Any:
        self.service_calls.append((domain, service, entity_id, dict(data)))
        return {"status": "ok"}


def make_router(
    *,
    jev: Any,
    deepseek: Any = None,
    ha: Any = None,
    variant: str = "entity",
    confidence_gate: Optional[float] = None,
    needs_param_threshold: Optional[float] = None,
) -> Router:
    return Router(
        jev_client=jev,
        deepseek_client=deepseek if deepseek is not None else FakeDeepSeek({}),
        ha_client=ha,
        jev_mode="intent",
        variant=variant,
        confidence_gate=confidence_gate,
        needs_param_threshold=needs_param_threshold,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── COMMAND ──────────────────────────────────────────────────────────────
def test_entity_command_executes_service_call() -> None:
    jev = FakeEntityJev(intent_score=0.95)
    deep = FakeDeepSeek({})
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Schalte das Licht im Wohnzimmer ein", entities=CATALOG)

    decision = run(scenario())

    # Jev #1: state = nur Transkript (keine Entity-Liste).
    assert jev.intent_calls == ["Schalte das Licht im Wohnzimmer ein"]
    assert "Entitäten" not in jev.intent_calls[0]

    # Jev #2: Entity-Liste + Transkript; criteria = {id: name} (kein `none` hier,
    # das ergänzt erst `build_entity_questions` im echten Client).
    assert len(jev.entity_calls) == 1
    state, criteria = jev.entity_calls[0]
    assert "Entitäten:" in state and "light.wohnzimmer" in state
    assert "Transkript: Schalte das Licht im Wohnzimmer ein" in state
    assert criteria == {
        "light.wohnzimmer": "Wohnzimmerlicht",
        "switch.steckdose": "Steckdose Flur",
    }

    # Kein DeepSeek in Variante D.
    assert deep.calls == []

    assert decision.intent == COMMAND
    assert decision.entity_id == "light.wohnzimmer"
    assert decision.domain == "light"
    assert decision.service == "turn_on"
    assert dict(decision.service_data) == {}  # v1: kein service_data
    assert decision.source == "jev+jev"
    assert decision.confidence == 1.0
    assert decision.response_text == "Okay, Wohnzimmerlicht eingeschaltet."
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]


@pytest.mark.parametrize(
    "service,expected_text",
    [
        ("turn_on", "Okay, Wohnzimmerlicht eingeschaltet."),
        ("turn_off", "Okay, Wohnzimmerlicht ausgeschaltet."),
        ("toggle", "Okay, Wohnzimmerlicht umgeschaltet."),
    ],
)
def test_entity_command_template_text(service: str, expected_text: str) -> None:
    jev = FakeEntityJev(intent_score=0.9, service=service)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht schalten", entities=CATALOG)

    decision = run(scenario())
    assert decision.response_text == expected_text
    assert ha.service_calls == [("light", service, "light.wohnzimmer", {})]


def test_entity_command_friendly_name_from_mapping() -> None:
    jev = FakeEntityJev(target="switch.steckdose", service="toggle")
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Steckdose umschalten", entities=CATALOG)

    decision = run(scenario())
    assert decision.response_text == "Okay, Steckdose Flur umgeschaltet."
    assert decision.domain == "switch"
    assert ha.service_calls == [("switch", "toggle", "switch.steckdose", {})]


# ── none ⇒ kein HA-Call ──────────────────────────────────────────────────
def test_entity_none_makes_no_service_call() -> None:
    jev = FakeEntityJev(target="none", target_conf=0.9)
    deep = FakeDeepSeek({})
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Mach irgendwas", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "ENTITY_NOT_FOUND"
    assert decision.response_text == NO_DEVICES
    assert ha.service_calls == []
    assert deep.calls == []
    assert len(jev.entity_calls) == 1  # Jev #2 lief, aber kein Call


# ── target-Konfidenz < Gate ⇒ kein HA-Call ───────────────────────────────
def test_entity_low_target_confidence_makes_no_service_call() -> None:
    jev = FakeEntityJev(target="light.wohnzimmer", target_conf=0.55)
    deep = FakeDeepSeek({})
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "GATE_REJECTED"
    assert decision.response_text == NOT_UNDERSTOOD
    assert ha.service_calls == []
    assert deep.calls == []
    assert len(jev.entity_calls) == 1


def test_entity_target_confidence_at_gate_is_allowed() -> None:
    jev = FakeEntityJev(target="light.wohnzimmer", target_conf=GATE)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == COMMAND
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]


# ── QUESTION ⇒ DeepSeek, kein Jev #2 ─────────────────────────────────────
def test_entity_question_uses_deepseek_not_jev2() -> None:
    jev = FakeEntityJev(intent_score=0.14)
    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    # P12.T3/C: mit passendem Lese-Wert ⇒ genau **ein** DeepSeek-Call für die
    # Frage (ohne Wert gäbe es den C-4-Fallback, siehe `tests/test_router_state.py`).
    ha = FakeHa(readable=READABLE_TEMP)

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.route(
            "Wie warm ist es im Wohnzimmer?", entities=CATALOG
        )

    decision = run(scenario())
    assert decision.intent == QUESTION
    assert decision.response_text == "Es ist 14:30 Uhr."
    assert jev.entity_calls == []  # KEIN Jev #2
    assert len(deep.calls) == 1
    assert ha.service_calls == []


# ── 255-Cap-Guard ────────────────────────────────────────────────────────
def _big_catalog(n: int) -> dict[str, str]:
    return {f"light.gerät{i:03d}": f"Gerät {i}" for i in range(n)}


def test_entity_variant_falls_back_to_class_above_255(caplog: Any) -> None:
    jev = FakeEntityJev()
    deep = FakeDeepSeek(
        {
            "entity_id": "light.gerät000",
            "service": "turn_on",
            "service_data": {},
            "response_text": "A",
        }
    )
    ha = FakeHa()
    catalog = _big_catalog(256)

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        with caplog.at_level(logging.WARNING, logger="manager.router"):
            return await router.handle("Licht an", entities=catalog)

    decision = run(scenario())
    # Rückfall auf Variante A: `classify` (intent+target) + DeepSeek.
    assert len(jev.classify_calls) == 1
    assert jev.intent_calls == [] and jev.entity_calls == []
    assert decision.intent == COMMAND
    assert decision.executed is True
    assert "255" in caplog.text and "Fallback" in caplog.text


def test_entity_variant_uses_entity_path_at_255() -> None:
    jev = FakeEntityJev(target="light.gerät000")
    ha = FakeHa()
    catalog = _big_catalog(255)

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.route("Licht an", entities=catalog)

    decision = run(scenario())
    assert len(jev.entity_calls) == 1
    assert jev.classify_calls == []
    assert decision.intent == COMMAND
    assert decision.entity_id == "light.gerät000"


# ── Allowlist / Dienst / Variante ────────────────────────────────────────
def test_entity_target_not_in_catalog_rejected() -> None:
    jev = FakeEntityJev(target="light.gibtsnicht", target_conf=1.0)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "ENTITY_NOT_ALLOWED"
    assert decision.response_text == NOT_UNDERSTOOD
    assert ha.service_calls == []


def test_entity_invalid_service_rejected() -> None:
    jev = FakeEntityJev(service="dim")
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht dimmen", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL
    assert ha.service_calls == []


def test_default_variant_is_class() -> None:
    router = Router(
        jev_client=FakeEntityJev(), deepseek_client=FakeDeepSeek({}), ha_client=FakeHa()
    )
    assert router.variant == "class"


def test_invalid_variant_raises_config_error() -> None:
    with pytest.raises(RouterConfigError):
        make_router(jev=FakeEntityJev(), variant="gibtsnicht")


# ═══════════════════════════════════════════════════════════════════════════
# E92 — `service_data` v2: Jev #2 sagt per `needs_param`, OB eine Zahl nötig
# ist; DeepSeek liefert dann **nur** den Zahlenwert (Allowlist je
# `(domain, service)`, ein Key, Validierung, kein stiller Ersatz-Call).
# ═══════════════════════════════════════════════════════════════════════════
PARAM_CATALOG: dict[str, Any] = {
    # E114: **mit** SUPPORT_BRIGHTNESS (Bit 1 ⇒ 6 = 2|4) – nur so darf
    # `brightness_pct` überhaupt gesendet werden.
    "light.wohnzimmer": {
        "friendly_name": "Wohnzimmerlicht",
        "attributes": {"supported_features": 6},
    },
    # `supported_features = 4` = **nur** EFFECT, **kein** BRIGHTNESS – das ist
    # der Live-Zustand aller 21 Lights (P12.T1) ⇒ Feature-Gate-Pfad.
    "light.flur": {
        "friendly_name": "Flurlicht",
        "attributes": {"supported_features": 4},
    },
    "climate.wohnzimmer": {
        "friendly_name": "Heizung Wohnzimmer",
        "attributes": {"min_temp": 18, "max_temp": 22},
    },
    "media_player.wohnzimmer": {
        "friendly_name": "Lautsprecher Wohnzimmer",
        "attributes": {},
    },
}
NEEDS_PARAM_QUESTION = (
    "Muss für diesen Befehl ein Zahlenwert angegeben werden "
    "(z. B. Helligkeit, Temperatur, Lautstärke)?"
)
DIM_TRANSCRIPT = "dimme das Wohnzimmerlicht auf 40 Prozent"


def param_router(
    *,
    jev: Any,
    deepseek: Any,
    ha: Any,
    transcript: str = DIM_TRANSCRIPT,
    **kwargs: Any,
) -> Any:
    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deepseek, ha=ha, **kwargs)
        return await router.handle(transcript, entities=PARAM_CATALOG)

    return run(scenario())


# ── Trigger: needs_param niedrig ⇒ exakt v1-Verhalten ──────────────────────
def test_needs_param_low_makes_no_deepseek_and_no_service_data() -> None:
    jev = FakeEntityJev(needs_param=0.2)
    deep = FakeDeepSeek({})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert deep.calls == []  # **kein** DeepSeek-Param-Aufruf
    assert decision.intent == COMMAND
    assert dict(decision.service_data) == {}
    assert decision.response_text == "Okay, Wohnzimmerlicht eingeschaltet."
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]


def test_needs_param_missing_answer_stays_v1() -> None:
    """Jev beantwortet die dritte Frage nicht ⇒ konservativ **kein** Param-Pfad."""
    jev = FakeEntityJev(needs_param=None)
    deep = FakeDeepSeek({})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert deep.calls == []
    assert dict(decision.service_data) == {}
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]


def test_needs_param_turn_off_has_no_allowlisted_key_so_no_deepseek() -> None:
    """``light.turn_off`` steht nicht in der Tabelle ⇒ kein DeepSeek, kein Key."""
    jev = FakeEntityJev(service="turn_off", needs_param=0.99)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, transcript="Licht aus")

    assert deep.calls == []
    assert dict(decision.service_data) == {}
    assert decision.response_text == "Okay, Wohnzimmerlicht ausgeschaltet."
    assert ha.service_calls == [("light", "turn_off", "light.wohnzimmer", {})]


# ── Trigger: needs_param hoch + gültiger Wert ⇒ genau ein DeepSeek-Call ────
def test_needs_param_high_extracts_value_into_service_data() -> None:
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert len(deep.calls) == 1  # **genau ein** DeepSeek-Aufruf
    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert decision.intent == COMMAND and decision.executed is True
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


def test_needs_param_high_prompt_asks_only_for_the_number() -> None:
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek({"brightness_pct": 40})

    param_router(jev=jev, deepseek=deep, ha=FakeHa())

    prompt, system_prompt = deep.calls[0]
    # Bereits aufgelöstes Gerät + Dienst + **einziger** erlaubter Key + Text.
    assert "light.wohnzimmer" in prompt
    assert "Wohnzimmerlicht" in prompt
    assert "light.turn_on" in prompt
    assert '"brightness_pct"' in prompt
    assert DIM_TRANSCRIPT in prompt
    # Keine Entitäts-/Dienstwahl, kein Antworttext.
    assert '"entity_id"' not in prompt
    assert '"service"' not in prompt
    assert '"response_text"' not in prompt
    assert system_prompt == DEEPSEEK_PARAM_SYSTEM_PROMPT


def test_needs_param_high_foreign_fields_are_never_passed_through() -> None:
    """Nur der eine erlaubte Key landet im Payload – Fremdfelder werden verworfen."""
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek(
        {
            "brightness_pct": 40,
            "transition": 3,
            "entity_id": "light.keller",
            "brightness": 255,
            "color_name": "rot",
        }
    )
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


# ── Ablehnung ⇒ **kein** Service-Call, kein stiller turn_on ───────────────
@pytest.mark.parametrize(
    "payload,label",
    [
        ({"brightness_pct": None}, "kein Wert im Text"),
        ({}, "Feld fehlt"),
        ({"brightness": 40}, "fremder Key"),
        ({"brightness_pct": 150}, "150 Prozent"),
        ({"brightness_pct": -5}, "negativ"),
        ({"brightness_pct": "40"}, "Text statt Zahl"),
        ({"brightness_pct": "hell"}, "Wort statt Zahl"),
        ({"brightness_pct": True}, "bool"),
    ],
)
def test_needs_param_rejected_value_makes_no_service_call(
    payload: Any, label: str
) -> None:
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek(payload)
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert ha.service_calls == [], f"{label}: es darf kein Service-Call laufen"
    assert decision.intent == ERROR
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert decision.response_text == NOT_UNDERSTOOD  # v4-Ton, fordert Wiederholen
    assert decision.executed is False


def test_needs_param_deepseek_error_makes_no_service_call() -> None:
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek(None, error=LlmUnavailableError("Gateway tot"))
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert len(deep.calls) == 1
    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert decision.response_text == NOT_UNDERSTOOD


def test_needs_param_deepseek_non_object_makes_no_service_call() -> None:
    jev = FakeEntityJev(needs_param=0.93)
    deep = FakeDeepSeek(["brightness_pct", 40])
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_PARAM_MISSING"


# ── Je (domain, service) **nur** der erlaubte Key ─────────────────────────
def test_climate_turn_on_carries_no_temperature_in_variant_d() -> None:
    """**B-2:** ``climate.turn_on`` **ignoriert** ``temperature`` (live
    ``GET /api/services``: nur ``climate.set_temperature`` nimmt den Wert).

    Im Jev-Pfad wählt Jev den Dienst; ``turn_on`` ist nicht in der
    Allowlist ⇒ es wird **kein** DeepSeek-Param-Aufruf gemacht und **kein**
    wirkungsloses Feld mitgeschickt.  Die Temperatur läuft über den
    class-Pfad (``set_temperature``) – siehe
    ``tests/test_router.py::test_class_path_uses_climate_set_temperature``.
    """
    jev = FakeEntityJev(
        target="climate.wohnzimmer", service="turn_on", needs_param=0.9
    )
    deep = FakeDeepSeek({"temperature": 21.5, "brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(
        jev=jev,
        deepseek=deep,
        ha=ha,
        transcript="mach es im Wohnzimmer auf 21,5 Grad warm",
    )

    assert deep.calls == []  # kein Extraktions-Aufruf ohne erlaubten Key
    assert dict(decision.service_data) == {}
    assert decision.domain == "climate"
    assert decision.response_text == "Okay, Heizung Wohnzimmer eingeschaltet."
    assert ha.service_calls == [
        ("climate", "turn_on", "climate.wohnzimmer", {})
    ]


def test_climate_turn_off_carries_no_temperature_in_variant_d() -> None:
    jev = FakeEntityJev(
        target="climate.wohnzimmer", service="turn_off", needs_param=0.9
    )
    deep = FakeDeepSeek({"temperature": 21.5})
    ha = FakeHa()

    decision = param_router(
        jev=jev, deepseek=deep, ha=ha, transcript="mach es auf 21,5 Grad aus"
    )

    assert deep.calls == []
    assert dict(decision.service_data) == {}
    assert ha.service_calls == [
        ("climate", "turn_off", "climate.wohnzimmer", {})
    ]


def test_media_player_turn_on_carries_no_volume_in_variant_d() -> None:
    """**B-2:** ``media_player.turn_on``/``toggle`` **starten nur** – den
    Lautstärkewert nimmt ausschließlich ``media_player.volume_set``."""
    for service in ("turn_on", "toggle"):
        jev = FakeEntityJev(
            target="media_player.wohnzimmer", service=service, needs_param=0.9
        )
        deep = FakeDeepSeek({"volume_level": 40, "brightness_pct": 40})
        ha = FakeHa()

        decision = param_router(
            jev=jev,
            deepseek=deep,
            ha=ha,
            transcript="dreh den lautsprecher im Wohnzimmer auf 40",
        )

        assert deep.calls == []
        assert dict(decision.service_data) == {}
        assert ha.service_calls == [
            ("media_player", service, "media_player.wohnzimmer", {})
        ]


def test_light_toggle_uses_brightness_pct() -> None:
    jev = FakeEntityJev(service="toggle", needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 25})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, transcript="Licht auf 25 %")

    assert dict(decision.service_data) == {"brightness_pct": 25}
    assert decision.response_text == "Okay, Wohnzimmerlicht auf 25 Prozent."
    assert ha.service_calls == [
        ("light", "toggle", "light.wohnzimmer", {"brightness_pct": 25})
    ]


# ── E114: Light ohne SUPPORT_BRIGHTNESS (B-3) ─────────────────────────────
def test_light_without_brightness_support_is_rejected_honestly() -> None:
    """``supported_features = 4`` (Live-Zustand 21/21): **kein** Bit 1 ⇒
    ``brightness_pct`` kann nicht wirken.

    Statt den Wert formal durchzureichen und „Mach das Licht auf 40 Prozent"
    als *Einschalten* auszuführen: **ehrliche Ablehnung**, **kein** Call.
    """
    jev = FakeEntityJev(target="light.flur", service="turn_on", needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(
        jev=jev,
        deepseek=deep,
        ha=ha,
        transcript="mach das Flurlicht auf 40 Prozent",
    )

    assert ha.service_calls == []  # **kein** An/Aus als Ersatzhandlung
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert decision.response_text == LIGHT_NO_BRIGHTNESS_TEXT
    assert decision.executed is False


def test_light_with_brightness_support_sends_brightness_pct() -> None:
    jev = FakeEntityJev(target="light.wohnzimmer", service="turn_on", needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(
        jev=jev, deepseek=deep, ha=ha, transcript="mach das Licht auf 40 Prozent"
    )

    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


def test_light_without_brightness_support_plain_on_still_works() -> None:
    """Ohne Zahlenforderung bleibt das Gate **wirkungslos** – reines
    An/Aus funktioniert auch ohne ``SUPPORT_BRIGHTNESS`` (sonst wäre jedes
    Light im Haus tot)."""
    jev = FakeEntityJev(target="light.flur", service="turn_on", needs_param=0.0)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, transcript="mach das Licht an")

    assert dict(decision.service_data) == {}
    assert decision.response_text == "Okay, Flurlicht eingeschaltet."
    assert ha.service_calls == [("light", "turn_on", "light.flur", {})]


def test_needs_param_without_number_in_sentence_is_rejected_not_guessed() -> None:
    """Jev sagt „Zahl nötig", im Satz steht keine ⇒ **Ablehnung** statt Raten
    (E92-Grundregel, B-4-Gate) – auch bei einem Light mit Brightness-Support."""
    jev = FakeEntityJev(target="light.wohnzimmer", service="turn_on", needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, transcript="dimme das Licht")

    assert deep.calls == []  # kein Extraktions-Aufruf ohne Zahl im Satz
    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert dict(decision.service_data) == {}


def test_allowlist_contains_only_real_ha_service_pairs() -> None:
    """**B-2:** Die Tabelle ist die **einzige** Quelle erlaubter Param-Keys
    (E92) – und sie nennt **echte** HA-Service-Namen."""
    assert dict(ENTITY_PARAM_ALLOWLIST) == {
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
    # Genau **sieben** verschiedene Keys – kein `fan_set_percentage`,
    # kein `open_cover`, kein `set_hvac_mode`.
    assert set(ENTITY_PARAM_ALLOWLIST.values()) == {
        "brightness_pct",
        "temperature",
        "position",
        "volume_level",
        "percentage",
        "value",
        "option",
    }
    assert ("light", "turn_off") not in ENTITY_PARAM_ALLOWLIST
    assert ("cover", "turn_on") not in ENTITY_PARAM_ALLOWLIST
    # Die korrigierten Paare aus T2 sind **nicht** mehr enthalten …
    assert ("climate", "turn_on") not in ENTITY_PARAM_ALLOWLIST
    assert ("media_player", "turn_on") not in ENTITY_PARAM_ALLOWLIST
    assert ("media_player", "toggle") not in ENTITY_PARAM_ALLOWLIST
    # … und jeder erlaubte Key hat Art, Bereich und Antwort-Template.
    assert set(ENTITY_PARAM_ALLOWLIST.values()) == set(ENTITY_PARAM_KIND)
    assert set(ENTITY_PARAM_ALLOWLIST.values()) == set(
        ENTITY_PARAM_RESPONSE_TEMPLATES
    )
    numeric = {k for k, kind in ENTITY_PARAM_KIND.items() if kind != "enum"}
    assert numeric <= set(ENTITY_PARAM_RANGES)
    # Wire-Skalierung gibt es nur für numerische Keys und ist **Teilmenge**
    # davon – nie für einen Enum-Wert.
    assert set(ENTITY_PARAM_WIRE_SCALE) <= numeric
    assert set(ENTITY_PARAM_WIRE_SCALE) == {"volume_level"}


def test_param_response_templates_are_wortlaut() -> None:
    assert dict(ENTITY_PARAM_RESPONSE_TEMPLATES) == {
        "brightness_pct": "Okay, {name} auf {value} Prozent.",
        "temperature": "Okay, {name} auf {value} Grad.",
        "volume_level": "Okay, {name} auf {value} Prozent.",
        "position": "Okay, {name} auf {value} Prozent.",
        "percentage": "Okay, {name} auf {value} Prozent.",
        "value": "Okay, {name} auf {value} gestellt.",
        "option": "Okay, {name} auf {value} gestellt.",
    }


def test_param_value_is_german_formatted_in_template() -> None:
    jev = FakeEntityJev(target="light.wohnzimmer", service="turn_on", needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 25})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, transcript="auf 25 Prozent")

    assert dict(decision.service_data) == {"brightness_pct": 25.0}
    assert decision.response_text == "Okay, Wohnzimmerlicht auf 25 Prozent."


def test_percent_kind_is_whole_number_but_degrees_keep_one_decimal() -> None:
    """``percent`` ist in HA eine Ganzzahl ⇒ 25,5 % ⇒ 26 %;
    ``degrees``/``number`` behalten eine Nachkommastelle (21,5 Grad)."""
    jev = FakeEntityJev(target="light.wohnzimmer", service="turn_on", needs_param=0.9)
    ha = FakeHa()

    decision = param_router(
        jev=jev,
        deepseek=FakeDeepSeek({"brightness_pct": 25.5}),
        ha=ha,
        transcript="auf 25,5 Prozent",
    )

    assert dict(decision.service_data) == {"brightness_pct": 26.0}
    assert decision.response_text == "Okay, Wohnzimmerlicht auf 26 Prozent."


def test_format_param_value_is_german() -> None:
    assert _format_param_value(21.0) == "21"
    assert _format_param_value(21.5) == "21,5"
    assert _format_param_value(40) == "40"


# ── Niemand ohne Jev-Auflösung ans Gate (kein DeepSeek, kein Call) ────────
def test_needs_param_none_target_makes_no_deepseek_and_no_service_call() -> None:
    jev = FakeEntityJev(target="none", target_conf=0.9, needs_param=0.99)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert deep.calls == []  # **kein** Param-Aufruf ohne Jev-Auflösung
    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_NOT_FOUND"
    assert decision.response_text == NO_DEVICES


def test_needs_param_low_target_conf_makes_no_deepseek_and_no_service_call() -> None:
    jev = FakeEntityJev(target_conf=0.6, needs_param=0.99)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert deep.calls == []
    assert ha.service_calls == []
    assert decision.error_code == "GATE_REJECTED"


def test_needs_param_target_outside_catalog_makes_no_deepseek_and_no_call() -> None:
    jev = FakeEntityJev(target="light.gibtsnicht", needs_param=0.99)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert deep.calls == []
    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_NOT_ALLOWED"


# ── TEXT-Pfad unverändert: kein Param-Aufruf ─────────────────────────────
def test_needs_param_question_path_never_asks_for_a_parameter() -> None:
    jev = FakeEntityJev(intent_score=0.14, needs_param=0.99)
    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    # Passender Lese-Wert ⇒ der Frage-Pfad ruft DeepSeek **einmal** (C-3);
    # die `needs_param`-Frage von Jev #2 darf trotzdem nicht gestellt werden.
    ha = FakeHa(readable=READABLE_WASSER)

    decision = param_router(
        jev=jev, deepseek=deep, ha=ha, transcript="ab wie viel Grad kocht Wasser?"
    )

    assert jev.entity_calls == []  # kein Jev #2 ⇒ keine needs_param-Frage
    assert len(deep.calls) == 1  # nur der **Frage**-Pfad
    assert decision.intent == QUESTION
    assert decision.response_text == "Es ist 14:30 Uhr."
    assert ha.service_calls == []


# ── Schwelle: konfigurierbar, nicht hart kodiert ─────────────────────────
def test_needs_param_threshold_default_is_half_like_intent_gate() -> None:
    assert NEEDS_PARAM_THRESHOLD == 0.5
    assert NEEDS_PARAM_THRESHOLD == COMMAND_SCORE_THRESHOLD
    router = make_router(jev=FakeEntityJev(), deepseek=FakeDeepSeek({}))
    assert router.needs_param_threshold == 0.5


def test_needs_param_threshold_setting_is_bounded() -> None:
    """`ROUTER_NEEDS_PARAM_THRESHOLD` ist ein NoulScore: nur 0.0–1.0."""
    from app.config import Settings

    assert Settings.model_fields["router_needs_param_threshold"].default == 0.5
    assert Settings(router_needs_param_threshold=0.0).router_needs_param_threshold == 0.0
    assert Settings(router_needs_param_threshold=1.0).router_needs_param_threshold == 1.0
    for invalid in (-0.1, 1.5):
        with pytest.raises(Exception):
            Settings(router_needs_param_threshold=invalid)


def test_needs_param_threshold_is_configurable() -> None:
    jev = FakeEntityJev(needs_param=0.4)
    deep_low = FakeDeepSeek({"brightness_pct": 40})
    ha_low = FakeHa()

    below = param_router(
        jev=jev, deepseek=deep_low, ha=ha_low, needs_param_threshold=0.5
    )
    # Unter der Schwelle: **kein** Param-Aufruf, aber der Befehl läuft wie in v1
    # (turn_on ohne service_data) – kein Fehler, kein „nicht verstanden".
    assert deep_low.calls == []
    assert ha_low.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]
    assert dict(below.service_data) == {}
    assert below.response_text == "Okay, Wohnzimmerlicht eingeschaltet."

    jev2 = FakeEntityJev(needs_param=0.4)
    deep_high = FakeDeepSeek({"brightness_pct": 40})
    ha_high = FakeHa()

    above = param_router(
        jev=jev2, deepseek=deep_high, ha=ha_high, needs_param_threshold=0.3
    )
    assert len(deep_high.calls) == 1
    assert dict(above.service_data) == {"brightness_pct": 40}


def test_needs_param_threshold_boundary_is_inclusive() -> None:
    jev = FakeEntityJev(needs_param=0.5)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha, needs_param_threshold=0.5)

    assert len(deep.calls) == 1
    assert dict(decision.service_data) == {"brightness_pct": 40}


# ── Variante A (Default) und 255-Cap bleiben unverändert ──────────────────
def test_variant_class_ignores_needs_param_completely() -> None:
    """A kennt keine `needs_param`-Frage ⇒ sein Pfad bleibt E90/E1 unberührt."""
    jev = FakeEntityJev(needs_param=0.99)
    deep = FakeDeepSeek(dict(COMMAND_PAYLOAD))
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha, variant="class")
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())

    assert jev.classify_calls and not jev.entity_calls and not jev.intent_calls
    assert len(deep.calls) == 1  # nur der A-Befehls-Pfad
    assert dict(decision.service_data) == {}  # A-Payload war `service_data: {}`
    assert decision.source == "jev+deepseek"
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]


def test_cap_fallback_to_class_ignores_needs_param(caplog: Any) -> None:
    """> 255 Entities ⇒ Rückfall auf A; der Param-Pfad wird **nicht** betreten."""
    jev = FakeEntityJev(needs_param=0.99)
    deep = FakeDeepSeek(
        {
            "entity_id": "light.gerät000",
            "service": "turn_on",
            "service_data": {},
            "response_text": "A",
        }
    )
    ha = FakeHa()
    catalog = _big_catalog(256)

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        with caplog.at_level(logging.WARNING, logger="manager.router"):
            return await router.handle("Licht an", entities=catalog)

    decision = run(scenario())

    assert jev.entity_calls == []  # kein Jev #2 ⇒ keine needs_param-Frage
    assert len(jev.classify_calls) == 1
    assert len(deep.calls) == 1
    assert decision.executed is True
    assert "255" in caplog.text


# ── P12.T4-4 / B-1: nackt **oder** qualifiziert (`light.turn_on`) ─────────
# Die Live-Abnahme T4-3a lieferte den qualifizierten Namen; die Kriterien/
# Templates sind nackt geschlüsselt.  Diese Tests beweisen, dass die Form an der
# LLM-Grenze vereinheitlicht wird und die Schranke dabei **nicht** aufweicht.
@pytest.mark.parametrize(
    "service,expected",
    [
        ("turn_on", "turn_on"),
        ("light.turn_on", "turn_on"),
    ],
)
def test_entity_variant_accepts_both_service_name_forms(
    service: str, expected: str
) -> None:
    """Beide Formen ⇒ derselbe Call **und** dasselbe Antwort-Template
    (``ENTITY_RESPONSE_TEMPLATES`` ist nackt geschlüsselt)."""
    jev = FakeEntityJev(service=service)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())

    assert decision.intent == COMMAND
    assert decision.service == expected
    assert decision.response_text == "Okay, Wohnzimmerlicht eingeschaltet."
    assert ha.service_calls == [("light", expected, "light.wohnzimmer", {})]


@pytest.mark.parametrize(
    "service,expected",
    [
        ("toggle", "toggle"),
        ("switch.toggle", "toggle"),
    ],
)
def test_entity_variant_template_also_works_for_another_domain(
    service: str, expected: str
) -> None:
    """Der Fix ist **nicht** ``light``-spezifisch: `switch.toggle` findet das
    nackte Template genauso."""
    jev = FakeEntityJev(target="switch.steckdose", service=service)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Steckdose umschalten", entities=CATALOG)

    decision = run(scenario())

    assert decision.response_text == "Okay, Steckdose Flur umgeschaltet."
    assert ha.service_calls == [("switch", expected, "switch.steckdose", {})]


@pytest.mark.parametrize("service", ["light.turn_on", "turn_on"])
def test_entity_variant_qualified_name_keeps_brightness_param(service: str) -> None:
    """Auch der Param-Pfad: ``ENTITY_PARAM_ALLOWLIST`` ist nach
    ``("light", "turn_on")`` nackt geschlüsselt – ein qualifizierter Name
    dürfte den Wert nicht stillschweigend verwerfen."""
    jev = FakeEntityJev(service=service, needs_param=0.9)
    deep = FakeDeepSeek({"brightness_pct": 40})
    ha = FakeHa()

    decision = param_router(jev=jev, deepseek=deep, ha=ha)

    assert decision.service == "turn_on"
    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert decision.response_text == "Okay, Wohnzimmerlicht auf 40 Prozent."
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


@pytest.mark.parametrize(
    "service",
    [
        "switch.turn_on",  # Domain-Mismatch: Ziel ist ein `light`
        "homeassistant.turn_on",  # unerlaubte Domain
        "light.explode",  # unbekannter Dienst
        "light.a.b",  # kaputte Form
    ],
)
def test_entity_variant_rejects_foreign_or_unknown_qualified_name(
    service: str,
) -> None:
    """Fail-closed bleibt: Domain-Mismatch / unbekannt ⇒ ``PROTOCOL_ERROR``,
    **kein** Call."""
    jev = FakeEntityJev(service=service)
    ha = FakeHa()

    async def scenario() -> Any:
        router = make_router(jev=jev, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())

    assert decision.intent == ERROR
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL
    assert ha.service_calls == []


def test_cap_fallback_to_class_normalizes_qualified_name(caplog: Any) -> None:
    """> 255 Entities ⇒ Rückfall auf ``class`` – auch dort wird der
    qualifizierte Name normalisiert (sonst wäre genau der Live-Fall wieder
    blockiert)."""
    jev = FakeEntityJev(needs_param=0.99)
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "domain": "light",
            "service": "light.turn_on",
            "service_data": {},
            "response_text": "A",
        }
    )
    ha = FakeHa()
    catalog = _big_catalog(256)
    catalog["light.wohnzimmer"] = "Wohnzimmerlicht"

    async def scenario() -> Any:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        with caplog.at_level(logging.WARNING, logger="manager.router"):
            return await router.handle("Wohnzimmerlicht an", entities=catalog)

    decision = run(scenario())

    assert jev.entity_calls == []  # Rückfall ⇒ kein Jev #2
    assert decision.executed is True
    assert decision.service == "turn_on"
    assert ha.service_calls == [("light", "turn_on", "light.wohnzimmer", {})]
    assert "255" in caplog.text
