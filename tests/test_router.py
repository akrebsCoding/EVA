"""Router-Tests (P4.T5, `PLAN.md` §7 → P4.T5, Layer **L0/`unit`**).

Prüfling ist `app/router.py` (P4.T4, Variante A/E1).  Getestet wird gegen den
**verbindlichen Vertrag** aus `STATE.md` §3 („Router (P4.T4)") und **E59**, nicht
gegen eine Wunschfassung.  Der Prüfling bleibt **unangetastet** (kein Patch, kein
Bug-Workaround).

Abdeckung der sechs Pflichtfälle aus `PLAN.md:460`:

1. **COMMAND** — Jev liefert `intent`+`target_class`, DeepSeek löst Entity +
   Service + `service_data` + `response_text` auf ⇒ Service-Call mit korrektem
   Payload, `executed=True` →
   `test_command_resolves_and_executes_service_call`,
   `test_handle_executes_command_end_to_end`, `test_command_uses_ha_cache_catalog`
2. **QUESTION** — **kein** Service-Call, `response_text` gesetzt →
   `test_question_returns_answer_without_service_call`
3. **Fehler** — LLM-/HA-Client-Fehler ⇒ definierter `RouterError`/Fallback, kein
   Absturz → `test_jev_error_*`, `test_deepseek_error_*`, `test_ha_error_*`,
   `test_config_error_is_not_swallowed`
4. **Allowlist-Verletzung** — Entity **nicht** im Katalog ⇒ **kein** Service-Call,
   definierter Fehler → `test_allowlist_violation_*`
5. **Gate-Unterschreitung** — Score `< 0.75` ⇒ **kein** Service-Call, Fallback →
   `test_gate_underflow_*`, `test_gate_boundaries`
6. **Leerer Entity-Cache** — definierter Fallback/Fehler →
   `test_empty_entity_cache_*`
7. **Negations-Guard (P9.T2a, E98)** — eine Verneinung darf **nie** schalten
   („Schalte Wohnzimmer nicht an." ⇒ live `turn_off`) → `test_negation_*`,
   `test_live_bug_wohnzimmer_nicht_an_never_calls_turn_off`,
   `test_detect_negation_*`, `test_positive_commands_are_not_blocked`

**Kein echtes Netz.**  Jev, DeepSeek und HA sind **injizierte Fakes** (die
Clients sind nach E59/E55 injizierbar); es wird weder `httpx` noch ein Socket
benutzt.  Die autouse-Netzsperre aus `tests/conftest.py` (E36) ist aktiv und
wird im Test `test_router_runs_while_network_block_is_active` positiv belegt.

**Deterministisch:** kein `sleep`, keine Zeit-/Zufallsabhängigkeit; alle Fakes
antworten synchron und reproduzierbar.

**Prüfwerte sind Literale** (E56/E59) — insbesondere die vier v4-§7.2-Fallback-
Texte werden **nicht** aus `app.router` importiert, sondern lokal als Literal
definiert.  Genau diese Tautologie hatte in P4.T4 eine Mutation wirkungslos
gemacht (`STATE.md` §5/P4.T4).
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone
from string import ascii_lowercase as letters
from typing import Any, Mapping, Optional

import pytest

from app.ha_client import HA_BLOCKED_DOMAINS, HA_ENTITY_DOMAINS, HaUnavailableError
from app.llm_client import (
    ChoiceResult,
    LlmProtocolError,
    LlmUnavailableError,
    NoulResult,
    SystemOneResult,
)
from app.router import (
    AllowlistViolationError,
    ConfidenceGateError,
    DeepSeekError,
    DOMAIN_SERVICE_CRITERIA,
    EmptyEntityCacheError,
    ENTITY_PARAM_ALLOWLIST,
    ENTITY_PARAM_KIND,
    ENTITY_PARAM_RANGES,
    ENTITY_PARAM_RESPONSE_TEMPLATES,
    ENTITY_PARAM_WIRE_SCALE,
    JevError,
    LIGHT_NO_BRIGHTNESS_TEXT,
    NegationGuardError,
    RouteDecision,
    Router,
    RouterConfigError,
    RouterError,
    RouterProtocolError,
    RouterTurnError,
    _apply_typed_service_data,
    _detect_command_negation,
    _normalize_service_name,
    _wire_service_data,
    detect_negation,
)
from tests.conftest import NetworkAccessBlocked

pytestmark = pytest.mark.unit

# ── Literale (E59: unabhängig vom Prüfling) ──────────────────────────────
#: Die vier Fallback-Texte **wörtlich v4 §7.2** (`WyomingPlan.txt:871-875`),
#: bewusst als lokale Literale — nicht aus `app.router` importiert.
NOT_UNDERSTOOD = "Ich habe dich nicht verstanden."
HA_UNAVAILABLE = "Home Assistant antwortet nicht."
DEEPSEEK_FAIL = "Ich konnte keine Antwort finden."
NO_DEVICES = "Keine Geräte gefunden."

#: Intent-Werte wörtlich `PLAN.md:459` / STATE §3.
COMMAND = "COMMAND"
QUESTION = "QUESTION"
ERROR = "ERROR"

#: Datei-Grenze des Confidence-Gates (E3).
GATE = 0.75


# ── Fakes (injiziert, kein Netz) ─────────────────────────────────────────
class FakeJev:
    """Ersatz für ``JevClient`` – liefert einen festen Score/Target oder wirft."""

    def __init__(
        self,
        *,
        score: float = 0.95,
        target: Optional[str] = "light",
        error: Optional[BaseException] = None,
        mode: str = "intent",
    ) -> None:
        self.mode = mode
        self.score = score
        self.target = target
        self.error = error
        self.calls: list[str] = []

    async def classify(self, state: str, **kwargs: Any) -> SystemOneResult:
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        return SystemOneResult(
            intent=NoulResult(score=self.score),
            target=(
                ChoiceResult(value=self.target, score=1.0)
                if self.target is not None
                else None
            ),
        )


class ExplodingJev:
    """Darf im ``JEV_MODE=off``-Pfad **nie** aufgerufen werden."""

    mode = "off"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, state: str, **kwargs: Any) -> SystemOneResult:
        self.calls.append(state)
        raise AssertionError("Jev darf bei JEV_MODE=off nicht aufgerufen werden")


class FakeDeepSeek:
    """Ersatz für ``DeepSeekClient`` – liefert ein festes JSON-Objekt oder wirft."""

    def __init__(
        self,
        payload: Any,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, Optional[str]]] = []

    async def complete_json(
        self, prompt: str, *, system_prompt: Optional[str] = None, **extra: Any
    ) -> Any:
        self.calls.append((prompt, system_prompt))
        if self.error is not None:
            raise self.error
        return self.payload


class FakeHa:
    """Ersatz für ``HomeAssistantClient`` – Katalog, Lese-Cache, Refresh, Calls.

    ``readable`` bildet den :class:`app.ha_client.ReadableCache` ab (P12.T3/C):
    ohne ihn bekommt eine Frage keine HA-Werte und antwortet mit dem
    ehrlichen Fallback statt zu raten.
    """

    def __init__(
        self,
        entities: Optional[Mapping[str, Any]] = None,
        *,
        refresh_entities: Optional[Mapping[str, Any]] = None,
        call_error: Optional[BaseException] = None,
        readable: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.entities: dict[str, Any] = dict(entities or {})
        self.refresh_entities = (
            None if refresh_entities is None else dict(refresh_entities)
        )
        self.call_error = call_error
        self.readable: dict[str, Any] = dict(readable or {})
        self.cached_calls = 0
        self.readable_calls = 0
        self.refresh_calls = 0
        self.service_calls: list[tuple[str, str, str, dict[str, Any]]] = []

    async def cached_entities(self) -> dict[str, Any]:
        self.cached_calls += 1
        return dict(self.entities)

    async def readable_snapshot(self) -> dict[str, Any]:
        self.readable_calls += 1
        return dict(self.readable)

    async def refresh(self) -> int:
        self.refresh_calls += 1
        if self.refresh_entities is not None:
            self.entities = dict(self.refresh_entities)
        return len(self.entities)

    async def call_service(
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> Any:
        self.service_calls.append((domain, service, entity_id, dict(data)))
        if self.call_error is not None:
            raise self.call_error
        return {"status": "ok", "service": f"{domain}.{service}"}


# ── Testdaten ────────────────────────────────────────────────────────────
#: E114: der Light trägt ``supported_features = 6`` (Bit 1 ``SUPPORT_BRIGHTNESS``
#: **plus** Bit 2 ``EFFECT``) – nur so darf ``brightness_pct`` gesendet werden.
#: Als reiner String wäre die Entity **ohne** Attributdaten ⇒ das Feature-Gate
#: würde jeden Helligkeitswert ehrlich ablehnen.
CATALOG: dict[str, Any] = {
    "light.wohnzimmer": {
        "friendly_name": "Wohnzimmerlicht",
        "attributes": {"supported_features": 6},
    },
    "switch.steckdose": {"friendly_name": "Steckdose Flur"},
}

#: DeepSeek liefert **drei** Keys: ``entity_id`` (immer zu entfernen),
#: ``brightness_pct`` (der einzige erlaubte Key) und ``brightness`` (kein
#: HA-Service-Feld ⇒ **muss** verworfen werden, B-3).
COMMAND_PAYLOAD: dict[str, Any] = {
    "entity_id": "light.wohnzimmer",
    "domain": "light",
    "service": "turn_on",
    "service_data": {"entity_id": "light.falsch", "brightness_pct": 40, "brightness": 200},
    "response_text": "Licht im Wohnzimmer ist an.",
}
QUESTION_PAYLOAD: dict[str, Any] = {"response_text": "Es ist 14:30 Uhr."}
#: Ein **frischer** Lese-Wert (P12.T3/C) im Format des ``ReadableCache``:
#: ``friendly_name`` liegt – wie in Home Assistant – **innerhalb** von
#: ``attributes``.  Ein ``sensor`` mit ``device_class: temperature`` passiert
#: die Whitelist; der Zeitstempel kommt aus der Fixture-Zeit.
#: Zweiter Lese-Wert für den Satz "Das Licht ist nicht an" (``illuminance``
#: steht in der Whitelist – echte HA-Instanzen haben solche Sensoren).
READABLE_LIGHT: dict[str, Any] = {
    "sensor.licht_wohnzimmer": {
        "entity_id": "sensor.licht_wohnzimmer",
        "state": "410",
        "attributes": {
            "friendly_name": "Licht Wohnzimmer",
            "device_class": "illuminance",
            "unit_of_measurement": "lx",
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
}
READABLE_TEMP: dict[str, Any] = {
    "sensor.temp_wohnzimmer": {
        "entity_id": "sensor.temp_wohnzimmer",
        "state": "22.4",
        "attributes": {
            "friendly_name": "Temperatur Wohnzimmer",
            "device_class": "temperature",
            "unit_of_measurement": "\u00b0C",
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
}


def make_router(
    *,
    jev: Any = None,
    deepseek: Any = None,
    ha: Any = None,
    jev_mode: Optional[str] = None,
    confidence_gate: Optional[float] = None,
) -> Router:
    return Router(
        jev_client=jev if jev is not None else FakeJev(),
        deepseek_client=deepseek if deepseek is not None else FakeDeepSeek({}),
        ha_client=ha,
        jev_mode=jev_mode,
        confidence_gate=confidence_gate,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 1. COMMAND ───────────────────────────────────────────────────────────
def test_command_resolves_and_executes_service_call() -> None:
    jev = FakeJev(score=0.95, target="light")
    deep = FakeDeepSeek(COMMAND_PAYLOAD)
    ha = FakeHa()

    async def scenario() -> tuple[RouteDecision, RouteDecision]:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        decision = await router.route(
            "Schalte das Licht im Wohnzimmer ein", entities=CATALOG
        )
        # `route()` entscheidet nur — kein Service-Call.
        assert ha.service_calls == []
        return decision, await router.execute(decision)

    decision, executed = run(scenario())

    # Jev: ein Aufruf, State enthält Katalog + Transkript (PLAN §1.1).
    assert len(jev.calls) == 1
    assert "- light.wohnzimmer: Wohnzimmerlicht" in jev.calls[0]
    assert "Schalte das Licht im Wohnzimmer ein" in jev.calls[0]

    # DeepSeek: ein Aufruf, Entity-Liste + Geräteklasse im Prompt.
    assert len(deep.calls) == 1
    prompt, system_prompt = deep.calls[0]
    assert "Erkannte Geräteklasse: light" in prompt
    assert "light.wohnzimmer" in prompt
    assert system_prompt is not None and "Geräte-Router" in system_prompt

    # Entscheidung: COMMAND, Entity autoritativ aufgelöst.
    assert decision.intent == COMMAND
    assert decision.is_command is True
    assert decision.is_question is False
    assert decision.is_error is False
    assert decision.entity_id == "light.wohnzimmer"
    assert decision.domain == "light"
    assert decision.service == "turn_on"
    assert decision.target_class == "light"
    assert decision.confidence == 0.95
    assert decision.source == "jev+deepseek"
    assert decision.response_text == "Licht im Wohnzimmer ist an."

    # `service_data`: **nur** der erlaubte Key überlebt (B-3) – `entity_id`
    # wird entfernt, `brightness` ist kein HA-Service-Feld ⇒ verworfen.
    assert dict(decision.service_data) == {"brightness_pct": 40}

    # Service-Call-Payload exakt (E59) – semantische Einheit, die Wire-
    # Skalierung greift nur bei `volume_level` (B-5).
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]
    assert executed.executed is True
    assert executed.call_result == {"status": "ok", "service": "light.turn_on"}


def test_handle_executes_command_end_to_end() -> None:
    jev = FakeJev(score=0.80, target="switch")
    deep = FakeDeepSeek(
        {
            "entity_id": "switch.steckdose",
            "service": "toggle",
            "service_data": {},
            "response_text": "Steckdose umgeschaltet.",
        }
    )
    ha = FakeHa()

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Steckdose umschalten", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == COMMAND
    assert decision.executed is True
    assert decision.entity_id == "switch.steckdose"
    assert decision.domain == "switch"  # autoritativ aus der entity_id
    assert ha.service_calls == [("switch", "toggle", "switch.steckdose", {})]


def test_command_uses_ha_cache_catalog() -> None:
    """Der Katalog kommt (ohne `entities=`) aus dem HA-Cache (E59)."""
    ha = FakeHa(CATALOG)
    jev = FakeJev(score=0.9, target="light")
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Licht an")

    decision = run(scenario())
    assert ha.cached_calls == 1
    assert ha.refresh_calls == 0  # Cache war nicht leer ⇒ kein Refresh
    assert decision.executed is True
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


def test_route_without_cache_entity_id_in_service_data() -> None:
    """`service_data` liegt im Payload **ohne** `entity_id` (HA-Client setzt es)."""
    ha = FakeHa()
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "service": "turn_on",
            "service_data": {"entity_id": "light.anders", "brightness_pct": 42},
            "response_text": "ok",
        }
    )

    async def scenario() -> None:
        router = make_router(deepseek=deep, ha=ha)
        await router.handle("Licht an", entities=CATALOG)

    run(scenario())
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 42})
    ]


# ── 2. QUESTION ──────────────────────────────────────────────────────────
def test_question_returns_answer_without_service_call() -> None:
    jev = FakeJev(score=0.1, target=None)
    deep = FakeDeepSeek(QUESTION_PAYLOAD)
    # P12.T3/C-3: eine Frage wird **mit** HA-Werten beantwortet; ohne passenden
    # Lese-Wert gäbe es den ehrlichen Fallback statt einer Antwort (C-4,
    # `tests/test_router_state.py::test_select_relevant_states_empty_input_
    # returns_empty_list`).
    ha = FakeHa(readable=READABLE_TEMP)

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        decision = await router.route(
            "Wie warm ist es im Wohnzimmer?", entities=CATALOG
        )
        assert ha.service_calls == []
        return decision

    decision = run(scenario())

    # `score < 0.5` ⇒ QUESTION (E59), DeepSeek liefert nur die Antwort.
    assert decision.intent == QUESTION
    assert decision.is_question is True
    assert decision.is_command is False
    assert decision.response_text == "Es ist 14:30 Uhr."
    assert decision.entity_id is None
    assert decision.service is None

    assert len(jev.calls) == 1
    assert len(deep.calls) == 1
    _, system_prompt = deep.calls[0]
    assert system_prompt is not None
    assert "hilfreicher deutscher Sprachassistent" in system_prompt

    # Kein Service-Call — auch nicht bei `execute`/`handle`.
    assert ha.service_calls == []


def test_execute_ignores_non_command_decision() -> None:
    ha = FakeHa()
    decision = RouteDecision(intent=QUESTION, transcript="t", response_text="a")

    async def scenario() -> RouteDecision:
        router = make_router(ha=ha)
        return await router.execute(decision)

    result = run(scenario())
    assert result is decision  # unverändert zurückgegeben
    assert result.executed is False
    assert ha.service_calls == []


# ── 3. Fehler (LLM/HA) ───────────────────────────────────────────────────
def test_jev_error_raises_turn_error_with_literal_fallback() -> None:
    jev = FakeJev(error=LlmUnavailableError("boom"))
    ha = FakeHa()

    async def scenario() -> None:
        router = make_router(jev=jev, deepseek=FakeDeepSeek({}), ha=ha)
        with pytest.raises(JevError) as excinfo:
            await router.route("Licht an", entities=CATALOG)
        assert isinstance(excinfo.value, RouterTurnError)
        assert isinstance(excinfo.value, RouterError)
        assert excinfo.value.code == "JEV_ERROR"
        assert excinfo.value.response_text == NOT_UNDERSTOOD

    run(scenario())
    assert ha.service_calls == []


def test_jev_error_falls_back_via_route_with_fallback() -> None:
    jev = FakeJev(error=LlmUnavailableError("weg"))

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=FakeDeepSeek({}))
        return await router.route_with_fallback("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.is_error is True
    assert decision.error_code == "JEV_ERROR"
    assert decision.response_text == NOT_UNDERSTOOD
    assert decision.source == "fallback"


def test_deepseek_error_raises_with_deepseek_fallback() -> None:
    deep = FakeDeepSeek({}, error=LlmProtocolError("kaputt"))
    ha = FakeHa()

    async def scenario() -> None:
        router = make_router(deepseek=deep, ha=ha)
        with pytest.raises(DeepSeekError) as excinfo:
            await router.route("Licht an", entities=CATALOG)
        assert excinfo.value.code == "DEEPSEEK_ERROR"
        assert excinfo.value.response_text == DEEPSEEK_FAIL

    run(scenario())
    assert ha.service_calls == []


def test_deepseek_malformed_json_raises_protocol() -> None:
    deep = FakeDeepSeek(["kein", "objekt"])

    async def scenario() -> None:
        router = make_router(deepseek=deep)
        with pytest.raises(RouterProtocolError) as excinfo:
            await router.route("Licht an", entities=CATALOG)
        assert excinfo.value.code == "PROTOCOL_ERROR"
        assert excinfo.value.response_text == DEEPSEEK_FAIL

    run(scenario())


def test_ha_service_error_becomes_ha_fallback() -> None:
    ha = FakeHa(call_error=HaUnavailableError("HTTP 500"))
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        decision = await router.route("Licht an", entities=CATALOG)
        return await router.execute(decision)

    result = run(scenario())
    assert result.intent == ERROR  # kein Absturz, definierter Fallback
    assert result.error_code == "HA_ERROR"
    assert result.response_text == HA_UNAVAILABLE
    assert result.executed is False
    assert len(ha.service_calls) == 1  # Versuch dokumentiert


def test_execute_without_ha_client_returns_ha_fallback() -> None:
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=None)
        decision = await router.route("Licht an", entities=CATALOG)
        return await router.execute(decision)

    result = run(scenario())
    assert result.intent == ERROR
    assert result.error_code == "HA_ERROR"
    assert result.response_text == HA_UNAVAILABLE


def test_ha_cache_error_becomes_ha_fallback() -> None:
    class BrokenHa(FakeHa):
        async def cached_entities(self) -> dict[str, Any]:
            self.cached_calls += 1
            raise HaUnavailableError("Cache kaputt")

    ha = BrokenHa()

    async def scenario() -> RouteDecision:
        router = make_router(ha=ha)
        return await router.route_with_fallback("Licht an")

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "HA_ERROR"
    assert decision.response_text == HA_UNAVAILABLE


def test_config_error_is_not_swallowed() -> None:
    async def scenario() -> None:
        router = make_router()
        with pytest.raises(RouterConfigError):
            await router.route_with_fallback("Licht an", entities=["kein", "mapping"])

    run(scenario())


def test_empty_transcript_raises_protocol() -> None:
    async def scenario() -> None:
        router = make_router()
        with pytest.raises(RouterProtocolError):
            await router.route("   ", entities=CATALOG)

    run(scenario())


# ── 4. Allowlist-Verletzung ──────────────────────────────────────────────
def test_allowlist_violation_raises_without_service_call() -> None:
    deep = FakeDeepSeek(
        {
            "entity_id": "light.gibtsnicht",
            "service": "turn_on",
            "service_data": {},
            "response_text": "x",
        }
    )
    ha = FakeHa()

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        with pytest.raises(AllowlistViolationError) as excinfo:
            await router.route("Licht an", entities=CATALOG)
        assert excinfo.value.code == "ENTITY_NOT_ALLOWED"
        assert excinfo.value.response_text == NOT_UNDERSTOOD
        assert ha.service_calls == []
        # kein Absturz im Fallback-Pfad
        return await router.route_with_fallback("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "ENTITY_NOT_ALLOWED"
    assert ha.service_calls == []


def test_allowlist_violation_null_entity() -> None:
    deep = FakeDeepSeek(
        {
            "entity_id": None,
            "service": "turn_on",
            "service_data": {},
            "response_text": "x",
        }
    )

    async def scenario() -> None:
        router = make_router(deepseek=deep)
        with pytest.raises(AllowlistViolationError):
            await router.route("Licht an", entities=CATALOG)

    run(scenario())


def test_allowlist_positive_uses_cache_catalog() -> None:
    """Nur die im Katalog vorhandene Entity wird durchgelassen."""
    ha = FakeHa(dict(CATALOG))
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        return await router.handle("Licht an")

    decision = run(scenario())
    assert decision.entity_id == "light.wohnzimmer"
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


def test_domain_mismatch_raises_protocol() -> None:
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "domain": "switch",
            "service": "turn_on",
            "service_data": {},
            "response_text": "x",
        }
    )

    async def scenario() -> None:
        router = make_router(deepseek=deep)
        with pytest.raises(RouterProtocolError):
            await router.route("Licht an", entities=CATALOG)

    run(scenario())


def test_missing_service_raises_protocol() -> None:
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "service": "   ",
            "service_data": {},
            "response_text": "x",
        }
    )

    async def scenario() -> None:
        router = make_router(deepseek=deep)
        with pytest.raises(RouterProtocolError):
            await router.route("Licht an", entities=CATALOG)

    run(scenario())


# ── 5. Gate-Unterschreitung ──────────────────────────────────────────────
def test_gate_underflow_rejects_command_without_service_call() -> None:
    jev = FakeJev(score=0.6, target="light")
    deep = FakeDeepSeek(COMMAND_PAYLOAD)
    ha = FakeHa()

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        with pytest.raises(ConfidenceGateError) as excinfo:
            await router.route("Licht an", entities=CATALOG)
        assert excinfo.value.code == "GATE_REJECTED"
        assert excinfo.value.response_text == NOT_UNDERSTOOD
        # Kein DeepSeek-Aufruf, kein Service-Call.
        assert deep.calls == []
        assert ha.service_calls == []
        return await router.route_with_fallback("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == "GATE_REJECTED"
    assert decision.response_text == NOT_UNDERSTOOD
    assert deep.calls == []  # auch im Fallback kein Service/DeepSeek
    assert ha.service_calls == []


@pytest.mark.parametrize(
    "score,expected_intent,expected_code",
    [
        (0.0, QUESTION, None),
        (0.49, QUESTION, None),
        (0.50, ERROR, "GATE_REJECTED"),  # 0.5 ≤ score < 0.75 ⇒ Gate-Ablehnung
        (0.74, ERROR, "GATE_REJECTED"),
        (0.75, COMMAND, None),  # Grenze inklusiv (E59)
        (0.99, COMMAND, None),
    ],
)
def test_gate_boundaries(
    score: float, expected_intent: str, expected_code: Optional[str]
) -> None:
    jev = FakeJev(score=score, target="light")
    deep = FakeDeepSeek(
        COMMAND_PAYLOAD if expected_intent == COMMAND else QUESTION_PAYLOAD
    )

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, confidence_gate=GATE)
        return await router.route_with_fallback("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == expected_intent
    # Der Gate-Fall darf **nicht** als anderer Turn-Fehler durchgehen: die
    # Ablehnung muss GATE_REJECTED sein, sonst testet die Grenze nichts.
    assert decision.error_code == expected_code


def test_default_gate_is_075_and_used() -> None:
    """E3: Der Wert kommt aus `settings.router_confidence_gate`, Default 0.75."""
    router = make_router()
    assert router.confidence_gate == GATE

    jev = FakeJev(score=0.7499, target="light")
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> None:
        # Ohne `confidence_gate`-Override muss der Default greifen.
        guarded = make_router(jev=jev, deepseek=deep)
        with pytest.raises(ConfidenceGateError):
            await guarded.route("Licht an", entities=CATALOG)
        assert deep.calls == []

    run(scenario())


# ── 6. Leerer Entity-Cache ───────────────────────────────────────────────
def test_empty_entity_cache_raises_no_devices_fallback() -> None:
    ha = FakeHa({}, refresh_entities={})

    async def scenario() -> RouteDecision:
        router = make_router(ha=ha)
        with pytest.raises(EmptyEntityCacheError) as excinfo:
            await router.route("Licht an")
        assert excinfo.value.code == "EMPTY_ENTITY_CACHE"
        assert excinfo.value.response_text == NO_DEVICES
        # Genau ein Sofort-Refresh pro `route()`-Aufruf (E59).
        assert ha.refresh_calls == 1
        assert ha.cached_calls == 2
        return await router.route_with_fallback("Licht an")

    decision = run(scenario())
    # Zweiter Aufruf (Fallback-Pfad) ⇒ erneut genau ein Refresh-Versuch.
    assert ha.refresh_calls == 2
    assert ha.cached_calls == 4
    assert decision.intent == ERROR
    assert decision.error_code == "EMPTY_ENTITY_CACHE"
    assert decision.response_text == NO_DEVICES
    assert ha.service_calls == []


def test_empty_cache_recovers_after_refresh() -> None:
    ha = FakeHa({}, refresh_entities=CATALOG)
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        return await router.handle("Licht an")

    decision = run(scenario())
    assert ha.refresh_calls == 1
    assert decision.intent == COMMAND
    assert decision.entity_id == "light.wohnzimmer"
    assert decision.executed is True


def test_empty_catalog_without_ha_client_raises_no_devices() -> None:
    async def scenario() -> None:
        router = make_router(ha=None)
        with pytest.raises(EmptyEntityCacheError) as excinfo:
            await router.route("Licht an")
        assert excinfo.value.response_text == NO_DEVICES

    run(scenario())


def test_empty_injected_catalog_raises_no_devices() -> None:
    async def scenario() -> None:
        router = make_router()
        with pytest.raises(EmptyEntityCacheError):
            await router.route("Licht an", entities={})

    run(scenario())


# ── JEV_MODE=off (E1/C/E59) ──────────────────────────────────────────────
def test_off_mode_question_never_calls_jev() -> None:
    jev = ExplodingJev()
    deep = FakeDeepSeek({"intent": "QUESTION", "response_text": "42."})
    ha = FakeHa(readable=READABLE_TEMP)

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha, jev_mode="off")
        return await router.route_with_fallback(
            "Wie warm ist es im Wohnzimmer?", entities=CATALOG
        )

    decision = run(scenario())
    assert jev.calls == []  # kein Jev-Aufruf (Fake hätte geworfen)
    assert decision.intent == QUESTION
    assert decision.response_text == "42."
    assert decision.source == "deepseek"


def test_off_mode_command_executes_service_call() -> None:
    jev = ExplodingJev()
    deep = FakeDeepSeek(
        {
            "intent": "COMMAND",
            "target_class": "light",
            "entity_id": "light.wohnzimmer",
            "service": "turn_off",
            "service_data": {"brightness_pct": 0},
            "response_text": "Aus.",
        }
    )
    ha = FakeHa()

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha, jev_mode="off")
        return await router.handle("Licht aus", entities=CATALOG)

    decision = run(scenario())
    assert jev.calls == []
    assert decision.intent == COMMAND
    assert decision.target_class == "light"
    assert decision.source == "deepseek"
    assert decision.executed is True
    # `light.turn_off` hat **keinen** erlaubten Param-Key (Ausschalten braucht
    # keinen Wert) ⇒ der Vorschlag `brightness_pct` wird verworfen (B-3).
    assert ha.service_calls == [
        ("light", "turn_off", "light.wohnzimmer", {})
    ]


def test_off_mode_allowlist_still_enforced() -> None:
    deep = FakeDeepSeek(
        {
            "intent": "COMMAND",
            "entity_id": "light.gibtsnicht",
            "service": "turn_on",
            "service_data": {},
            "response_text": "x",
        }
    )

    async def scenario() -> None:
        router = make_router(jev=ExplodingJev(), deepseek=deep, jev_mode="off")
        with pytest.raises(AllowlistViolationError):
            await router.route("Licht an", entities=CATALOG)

    run(scenario())


# ── Konfiguration / Defaults ─────────────────────────────────────────────
def test_invalid_jev_mode_raises_config_error() -> None:
    with pytest.raises(RouterConfigError):
        Router(jev_client=FakeJev(), deepseek_client=FakeDeepSeek({}), jev_mode="gibtsnicht")


def test_mode_falls_back_to_client_mode() -> None:
    router = make_router(jev=FakeJev(mode="gate"))
    assert router.jev_mode == "gate"


def test_router_constructs_without_injected_clients() -> None:
    """Default-Clients werden lazy gebaut (keine I/O bei der Konstruktion)."""
    router = Router()
    assert router.jev_client is not None
    assert router.deepseek_client is not None
    assert router.ha_client is None
    assert router.jev_mode == "intent"


# ── Netz-Nachweis ────────────────────────────────────────────────────────
def test_router_runs_while_network_block_is_active() -> None:
    """Die Netzsperre (E36) ist aktiv; der Router läuft vollständig in-process.

    Ein echter Socket-Kontakt (DNS) blutet als `NetworkAccessBlocked` hoch —
    genau das würde passieren, wenn der Router das Netz träfe.  Der Turn aus
    reinen Fakes ist also ein positiver Beweis für Netzfreiheit (L0/§7.1).
    """
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("mock-router.invalid", 80)

    ha = FakeHa()
    deep = FakeDeepSeek(COMMAND_PAYLOAD)

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        return await router.handle("Licht an", entities=CATALOG)

    decision = run(scenario())
    assert decision.executed is True
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


# ── 7. Negations-Guard (P9.T2a, E98) ─────────────────────────────────────
# Grundregel: **im Zweifel nicht schalten.**  Eine Verneinung darf nie als
# Schalt-Auftrag ausgeführt werden – der Live-Fall war das Gegenteil
# („Schalte Wohnzimmer nicht an." ⇒ `turn_off`, das Licht ging **aus**).
#
# Die Tests prüfen **beide** Guard-Stellen: den Vorabcheck in `route()` (vor
# jedem LLM-Aufruf) und den verbindlichen Guard in `execute()` direkt **vor**
# `ha_client.call_service`.
NEGATION_TEXT = "Ich habe nichts geschaltet. Bitte sag mir klar, was ich tun soll."
NEGATION_CODE = "NEGATION_GUARD"

#: Verneinungs-Sätze, die **nie** schalten dürfen (Live-Fall + Varianten).
NEGATION_TRANSCRIPTS = [
    "Schalte Wohnzimmer nicht an.",  # der Live-Bug, wörtlich
    "schalte Wohnzimmer nicht ein",
    "Schalte das Wohnzimmerlicht nicht aus",
    "Lass das Wohnzimmerlicht aus",
    "lass die Heizung an",
    "mach das Wohnzimmerlicht nicht an",
    "Schalte das Licht bitte nicht ein",
    "schalte nicht nicht aus",  # Doppelverneinung ⇒ konservativ blockiert
]

#: Sätze, die **nicht** geblockt werden dürfen (Fehlalarm-Gegenproben).
POSITIVE_TRANSCRIPTS = [
    "schalte das Wohnzimmerlicht ein",
    "schalte das Wohnzimmerlicht aus",
    "dimme das Wohnzimmerlicht auf 40 Prozent",
    "Schalte nicht nur das Wohnzimmerlicht ein, sondern auch die Heizung",
    "Stelle die Heizung auf niedrig",  # „niedrig" ist kein „nie"-Marker
]


class TripwireHa(FakeHa):
    """HA-Fake, der bei `call_service` **loud** stirbt.

    Damit ist „kein HA-Call" nicht nur am Ergebnis abgelesen, sondern ein
    ausgelöster Fehler – ein Call kann den Test also nicht unauffällig
    passieren.
    """

    async def call_service(  # type: ignore[override]
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> Any:
        raise AssertionError(
            f"call_service wurde aufgerufen ({domain}.{service} {entity_id})"
        )


def test_negation_blocks_and_asks_back_without_touching_ha() -> None:
    """Jeder Verneinungs-Satz ⇒ ERROR-Turn, Rückfrage, **kein** Service-Call."""
    jev = FakeJev(score=0.95, target="light")
    deep = FakeDeepSeek(COMMAND_PAYLOAD)
    ha = TripwireHa()

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        return await router.handle("Schalte Wohnzimmer nicht an.", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == NEGATION_CODE
    assert decision.response_text == NEGATION_TEXT
    assert decision.executed is False
    # Der Vorabcheck steht **vor** jedem LLM-Aufruf ⇒ nichts teuer, nichts
    # geraten, und erst recht kein HA-Call.
    assert jev.calls == []
    assert deep.calls == []


@pytest.mark.parametrize("transcript", NEGATION_TRANSCRIPTS)
def test_negation_never_reaches_service_call(transcript: str) -> None:
    """Live-Fall und Varianten: `route()` lehnt ab, `execute()` ruft nie HA."""
    ha = TripwireHa()

    async def scenario() -> tuple[RouteDecision, RouteDecision]:
        router = make_router(deepseek=FakeDeepSeek(COMMAND_PAYLOAD), ha=ha)
        with pytest.raises(NegationGuardError) as excinfo:
            await router.route(transcript, entities=CATALOG)
        assert excinfo.value.code == NEGATION_CODE
        assert excinfo.value.response_text == NEGATION_TEXT
        fallback = await router.route_with_fallback(transcript, entities=CATALOG)
        # Auch ein von Hand gebauter COMMAND darf nicht durchkommen.
        forced = RouteDecision(
            intent=COMMAND,
            transcript=transcript,
            entity_id="light.wohnzimmer",
            domain="light",
            service="turn_off",
            response_text="Okay, Wohnzimmerlicht ausgeschaltet.",
        )
        return fallback, await router.execute(forced)

    fallback, forced_execute = run(scenario())
    assert fallback.intent == ERROR
    assert fallback.error_code == NEGATION_CODE
    assert fallback.response_text == NEGATION_TEXT
    assert forced_execute.intent == ERROR
    assert forced_execute.error_code == NEGATION_CODE
    assert forced_execute.executed is False
    # `TripwireHa` hätte bei jedem `call_service` geworfen ⇒ 0 Calls.


def test_live_bug_wohnzimmer_nicht_an_never_calls_turn_off() -> None:
    """Der Live-Bug wörtlich: LLM sagt `turn_off` – geschaltet wird trotzdem nicht.

    Das ist der eigentliche Regressionstest: der Fehler war **nicht**, dass
    falsch klassifiziert wurde, sondern dass die Verneinung trotzdem geschaltet
    hat.  Der Guard hängt deshalb am **Call**, nicht am Score.
    """
    ha = FakeHa()
    payload = dict(COMMAND_PAYLOAD, service="turn_off")

    async def scenario() -> RouteDecision:
        router = make_router(
            jev=FakeJev(score=0.99, target="light"),
            deepseek=FakeDeepSeek(payload),
            ha=ha,
        )
        decision = await router.route_with_fallback(
            "Schalte Wohnzimmer nicht an.", entities=CATALOG
        )
        return await router.execute(decision)

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == NEGATION_CODE
    assert decision.response_text == NEGATION_TEXT
    assert decision.executed is False
    assert ha.service_calls == []  # **kein** turn_off, gar kein Call


def test_execute_guard_works_for_manually_built_command() -> None:
    """`execute()` ist die Zusage – auch ohne vorheriges `route()`."""
    ha = FakeHa()
    decision = RouteDecision(
        intent=COMMAND,
        transcript="Mach das Licht an, aber nicht die Heizung",
        entity_id="light.wohnzimmer",
        domain="light",
        service="turn_on",
        response_text="Okay, Wohnzimmerlicht eingeschaltet.",
    )

    async def scenario() -> RouteDecision:
        return await make_router(ha=ha).execute(decision)

    result = run(scenario())
    assert result.intent == ERROR
    assert result.error_code == NEGATION_CODE
    assert result.response_text == NEGATION_TEXT
    assert ha.service_calls == []


def test_execute_guard_does_not_touch_non_command_turns() -> None:
    """Nur COMMAND wird geprüft – QUESTION/ERROR laufen unverändert durch."""
    ha = FakeHa()
    question = RouteDecision(
        intent=QUESTION, transcript="Wie ist der Status?", response_text="14:30 Uhr."
    )

    async def scenario() -> RouteDecision:
        router = make_router(ha=ha)
        assert await router.execute(question) is question
        return await router.execute(
            RouteDecision(intent=ERROR, transcript="nichts", response_text="x")
        )

    error = run(scenario())
    assert error.intent == ERROR
    assert error.error_code is None  # unberührt, kein Guard-Eingriff
    assert ha.service_calls == []


@pytest.mark.parametrize("transcript", POSITIVE_TRANSCRIPTS)
def test_positive_commands_are_not_blocked(transcript: str) -> None:
    """Gegenprobe Fehlalarme: normale Befehle und „nicht nur … sondern" laufen."""
    ha = FakeHa()
    jev = FakeJev(score=0.95, target="light")
    deep = FakeDeepSeek(dict(COMMAND_PAYLOAD, service_data={"brightness_pct": 40}))

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha)
        decision = await router.route(transcript, entities=CATALOG)
        return await router.execute(decision)

    decision = run(scenario())
    assert decision.intent == COMMAND
    assert decision.error_code is None
    assert decision.executed is True
    assert len(ha.service_calls) == 1
    assert ha.service_calls[0][0:3] == ("light", "turn_on", "light.wohnzimmer")
    assert ha.service_calls[0][3] == {"brightness_pct": 40}


def test_dim_40_percent_still_executes_with_its_number() -> None:
    """E92 bleibt unberührt: „40 Prozent" wird als `brightness_pct` übergeben."""
    ha = FakeHa()
    deep = FakeDeepSeek(dict(COMMAND_PAYLOAD, service_data={"brightness_pct": 40}))

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        decision = await router.route(
            "dimme das Wohnzimmerlicht auf 40 Prozent", entities=CATALOG
        )
        return await router.execute(decision)

    decision = run(scenario())
    assert decision.executed is True
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmer", {"brightness_pct": 40})
    ]


def test_status_statement_with_negation_is_answered_not_blocked() -> None:
    """„Das Licht ist nicht an" ist ein **Satz**, kein Befehl ⇒ keine Blockade.

    Der Vorabcheck gilt nur für befehlsförmige Sätze; die Frage/Sachlage läuft
    unverändert durch DeepSeek und wird **beantwortet** (kein Guard-Text).
    """
    # Ein **passender** Lese-Wert zum Satz ("Licht") – sonst gäbe es den
    # C-4-Fallback statt einer Modellantwort; der Test prüft den Guard, nicht C-4.
    ha = FakeHa(readable=READABLE_LIGHT)
    deep = FakeDeepSeek({"response_text": "Nein, das Licht ist aus."})

    async def scenario() -> RouteDecision:
        router = make_router(
            jev=FakeJev(score=0.2, target="light"), deepseek=deep, ha=ha
        )
        return await router.route_with_fallback(
            "Das Licht ist nicht an", entities=CATALOG
        )

    decision = run(scenario())
    assert decision.intent == QUESTION
    assert decision.response_text == "Nein, das Licht ist aus."
    assert decision.error_code is None
    assert ha.service_calls == []


def test_negation_guard_is_independent_of_router_variant() -> None:
    """Auch `JEV_MODE=off` (Variante C) wird geschützt – und ruft Jev nicht."""
    ha = TripwireHa()

    async def scenario() -> RouteDecision:
        router = make_router(
            jev=ExplodingJev(),
            deepseek=FakeDeepSeek(
                dict(COMMAND_PAYLOAD, intent="COMMAND")
            ),
            jev_mode="off",
            ha=ha,
        )
        return await router.route_with_fallback(
            "Schalte das Wohnzimmerlicht nicht aus", entities=CATALOG
        )

    decision = run(scenario())
    assert decision.intent == ERROR
    assert decision.error_code == NEGATION_CODE
    assert decision.response_text == NEGATION_TEXT


def test_detect_negation_markers_and_phrase() -> None:
    """Die reine Erkennung: Marker, Präfix und die „lass"-Form."""
    marked = [
        "Schalte Wohnzimmer nicht an.",
        "Nichts bitte.",
        "Keine Heizung.",
        "Schalte es nie ein.",
        "niemals",
        "Mach das Licht an, ohne zu fragen",
        "Weder an noch aus",
        "nichtein",  # zusammengeschrieben (ASR)
    ]
    for transcript in marked:
        assert detect_negation(transcript) is not None, transcript

    clear = [
        "schalte das Wohnzimmerlicht ein",
        "schalte das Wohnzimmerlicht aus",
        "dimme auf 40 Prozent",
        "Stelle die Heizung auf niedrig",  # „niedrig" ≠ „nie"
        "Wie ist der Status der Spülmaschine?",
        "Wie warm ist es im Schlafzimmer?",
        "Mach das Licht heller",
    ]
    for transcript in clear:
        assert detect_negation(transcript) is None, transcript


def test_detect_negation_respects_nicht_nur_sondern_exception() -> None:
    """Fehlalarm-Schutz: „nicht **nur** … **sondern**" ist positiv."""
    assert detect_negation(
        "Schalte nicht nur das Licht ein, sondern auch die Heizung"
    ) is None
    assert detect_negation(
        "Mach nicht nur das Wohnzimmerlicht an, sondern auch die Heizung mit"
    ) is None
    # Aber das echte „nicht" **ohne** „nur" bleibt eine Verneinung ⇒ blockiert.
    assert detect_negation("Schalte nicht das Licht ein, sondern die Heizung")


def test_detect_command_negation_needs_a_command_shape() -> None:
    """Der Vorabcheck verlangt ein **Befehlsverb** – Fragen laufen durch."""
    assert _detect_command_negation("Schalte Wohnzimmer nicht an.") == "marker:nicht"
    assert _detect_command_negation("Lass das Wohnzimmerlicht aus") == "leave_pattern"
    assert _detect_command_negation("Das Licht ist nicht an") is None
    assert _detect_command_negation("Warum ist das Licht nicht aus?") is None
    assert _detect_command_negation("Wie ist der Status?") is None


# ══════════════════════════════════════════════════════════════════════════
# E113/A-5 (P12.T2) – Katalog-Begrenzung: relevanzbasiert, ≤ JEV_MAX_CHOICES,
# deterministisch, beobachtbar (`deploy/docs/P12_DESIGN.md` A-7 … A-10)
# ══════════════════════════════════════════════════════════════════════════
from app.llm_client import JEV_MAX_CHOICES  # noqa: E402
from app.router import (  # noqa: E402
    CATALOG_SELECTION_KEY,
    JEV_CATALOG_LIMIT,
    PROMPT_CATALOG_LIMIT,
    _fold,
    _select_relevant_entities,
)

#: Katalog mit 6 Räumen – zwei je Domain, damit die Auswahl **greifen** muss.
ROOM_CATALOG: dict[str, Any] = {
    "light.wohnzimmer": "Wohnzimmerlicht",
    "light.kueche": "Küchenlicht",
    "light.schlafzimmer": "Schlafzimmerlicht",
    "switch.wohnzimmer_steckdose": {"friendly_name": "Steckdose Wohnzimmer"},
    "switch.kueche_steckdose": "Steckdose Küche",
    "switch.schlafzimmer_steckdose": "Steckdose Schlafzimmer",
}
#: Klein genug, dass die Begrenzung in den Tests greift (live: 375 > 254/120).
TEST_LIMIT = 4


def test_catalog_limits_sit_below_the_jev_choice_cap() -> None:
    """A-5: 254 (< 255) für die ``choice``-Frage, 120 für Prompt-Listen."""
    assert JEV_CATALOG_LIMIT == JEV_MAX_CHOICES - 1 == 254
    assert PROMPT_CATALOG_LIMIT == 120
    assert JEV_CATALOG_LIMIT < JEV_MAX_CHOICES


def test_entity_relevance_filter_keeps_matching_room_only() -> None:
    """A-7: „Wohnzimmer" ⇒ Küche/Schlafzimmer raus (nur passende Entities)."""
    selection = _select_relevant_entities(
        "Schalte das Licht im Wohnzimmer ein", ROOM_CATALOG, limit=TEST_LIMIT
    )
    assert set(selection.entities) == {
        "light.wohnzimmer",
        "switch.wohnzimmer_steckdose",
    }
    assert selection.mode == "relevance"
    assert selection.matches == 2
    assert selection.candidates == 6
    assert selection.selected == 2
    assert selection.truncated is True
    assert selection.limit == TEST_LIMIT


def test_entity_relevance_filter_uses_aliases_and_entity_id() -> None:
    """A-7: „Lampe"/„Musik" ⇒ **Rangbonus** auf die Domain (kein Match-Filter)."""
    catalog = {
        "light.wohnzimmer": "Wohnzimmerlicht",
        "media_player.wohnzimmer": "Wohnzimmer TV",
        "switch.wohnzimmer_steckdose": "Steckdose Wohnzimmer",
        "switch.kueche_steckdose": "Steckdose Küche",
    }
    # „Lampe“ ⇒ light bekommt den Alias-Bonus und landet **vorn**; die reine
    # Namens-/ID-Treffer derselben Domain bleiben dahinter.
    lampen = _select_relevant_entities(
        "Mach die Lampe im Wohnzimmer an", catalog, limit=2
    )
    assert list(lampen.entities)[0] == "light.wohnzimmer"
    assert lampen.mode == "relevance"
    # Der Alias allein ist **kein** Treffer: media_player.wohnzimmer (nur
    # „wohnzimmer") bleibt drin, switch.kueche_steckdose fliegt raus.
    assert "media_player.wohnzimmer" in lampen.entities
    assert "switch.kueche_steckdose" not in lampen.entities
    # „Musik“ ⇒ media_player gewinnt denselben Wettbewerb.
    musik = _select_relevant_entities("Spiel Musik im Wohnzimmer", catalog, limit=2)
    assert list(musik.entities)[0] == "media_player.wohnzimmer"
    # Entity-ID-Treffer („Küche“ steckt nur in der ``entity_id``/im Namen).
    kueche = _select_relevant_entities(
        "Schalte die Steckdose in der Küche aus", catalog, limit=1
    )
    assert set(kueche.entities) == {"switch.kueche_steckdose"}


def test_entity_relevance_filter_falls_back_to_full_catalog() -> None:
    """A-8: **kein** Treffer ⇒ voller Katalog (bis zum Limit), kein „nichts gefunden"."""
    selection = _select_relevant_entities(
        "Wie ist das Wetter in Buxtehude?", ROOM_CATALOG, limit=TEST_LIMIT
    )
    assert selection.mode == "fallback"
    assert selection.matches == 0
    assert selection.selected == TEST_LIMIT
    # Erste `limit` IDs alphabetisch – deterministisch, nicht zufällig.
    assert list(selection.entities) == sorted(ROOM_CATALOG)[:TEST_LIMIT]


def test_entity_relevance_filter_limit_is_deterministic() -> None:
    """A-9: gleiche Eingabe ⇒ identische Auswahl (Sortierung), Punktgleichstand stabil."""
    first = _select_relevant_entities(
        "Schalte das Licht im Wohnzimmer ein", ROOM_CATALOG, limit=TEST_LIMIT
    )
    second = _select_relevant_entities(
        "Schalte das Licht im Wohnzimmer ein", ROOM_CATALOG, limit=TEST_LIMIT
    )
    assert list(first.entities) == list(second.entities)
    assert first.as_dict() == second.as_dict()

    # Punktgleichstand (alle drei heißen nur „Licht“) ⇒ der Titel entscheidet
    # über die ``entity_id``, und die Rangfolge zeigt sich darin, **wer** die
    # Grenze überlebt (die Liste selbst bleibt alphabetisch, siehe
    # ``CatalogSelection.entities``).
    gleichstand = {
        "light.wohnzimmer": "Licht",
        "light.kueche": "Licht",
        "light.flur": "Licht",
    }
    assert set(
        _select_relevant_entities("Mach das Licht an", gleichstand, limit=1).entities
    ) == {"light.flur"}
    assert set(
        _select_relevant_entities("Mach das Licht an", gleichstand, limit=3).entities
    ) == set(gleichstand)
    # Umlaut-Faltung: „Küche"/„Kueche“ ⇒ derselbe Treffer.
    assert set(
        _select_relevant_entities("Loesch das Licht in der Kueche", ROOM_CATALOG, limit=1).entities
    ) == {"light.kueche"}
    assert set(
        _select_relevant_entities("Lösche das Licht in der Küche", ROOM_CATALOG, limit=1).entities
    ) == {"light.kueche"}


def test_entity_relevance_filter_never_returns_less_than_the_matches() -> None:
    """A-7: nie kleiner als die Treffer – und nie mehr als ``limit``."""
    catalog = {f"light.wohnzimmer_{index}": f"Wohnzimmer {index}" for index in range(20)}
    selection = _select_relevant_entities(
        "Mach alle Wohnzimmer Lichter an", catalog, limit=5
    )
    assert selection.matches == 20
    assert selection.selected == 5
    assert len(selection.entities) == 5


def test_entity_relevance_filter_passes_small_catalog_through() -> None:
    """Der Filter ist eine **Begrenzung**, kein Vorschaufilter.

    Unterhalb des Limits wird der Katalog **unverändert** durchgereicht – sonst
    verlöre der Router ein Gerät ohne jeden Prompt-Gewinn.
    """
    selection = _select_relevant_entities(
        "Wie ist das Wetter in Buxtehude?", ROOM_CATALOG, limit=len(ROOM_CATALOG)
    )
    assert selection.mode == "all"
    assert selection.truncated is False
    assert set(selection.entities) == set(ROOM_CATALOG)


def test_entity_relevance_filter_logs_candidates_and_survivors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A-5: greift die Begrenzung, ist sie im Log **beobachtbar** (INFO)."""
    import logging

    manager_logger = logging.getLogger("manager")
    handler = caplog.handler
    manager_logger.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO, logger="manager"):
            _select_relevant_entities(
                "Schalte das Licht im Wohnzimmer ein", ROOM_CATALOG, limit=TEST_LIMIT
            )
    finally:
        manager_logger.removeHandler(handler)

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "manager.router" and "Katalog begrenzt" in record.getMessage()
    ]
    assert lines, f"kein Katalog-Log in {caplog.text!r}"
    line = lines[-1]
    assert "6 Kandidaten" in line
    assert "-> 2 durchgelassen" in line
    assert "Limit 4" in line
    assert "Texttreffer 2" in line
    assert "Modus=relevance" in line
    assert "gekuerzt=True" in line


def test_command_decision_carries_catalog_selection_in_raw() -> None:
    """A-5: Historie im Turn – Kandidaten/Auswahl/Limit/Treffer/Modus."""
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "Wohnzimmerlicht an.",
        }
    )

    async def scenario() -> RouteDecision:
        router = make_router(jev=FakeJev(), deepseek=deep, ha=FakeHa())
        return await router.route(
            "Schalte das Licht im Wohnzimmer ein", entities=ROOM_CATALOG
        )

    decision = run(scenario())
    selection = decision.raw[CATALOG_SELECTION_KEY]
    assert selection["candidates"] == 6
    assert selection["limit"] == PROMPT_CATALOG_LIMIT
    assert selection["truncated"] is False
    assert selection["mode"] == "all"
    # Der Prompt enthält den **Auswahl-Satz** – hier der volle kleine Katalog.
    prompt = deep.calls[0][0]
    assert "- light.wohnzimmer: Wohnzimmerlicht" in prompt
    assert "- switch.kueche_steckdose: Steckdose Küche" in prompt


def test_command_prompt_and_jev_state_use_the_same_limited_catalog() -> None:
    """A-5: Jev-``state`` und DeepSeek-Prompt sehen **denselben** Entities-Satz.

    Sonst könnte DeepSeek eine Entity wählen, die Jev nie gesehen hat.
    """
    jev = FakeJev()
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "Wohnzimmerlicht an.",
        }
    )

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=FakeHa())
        return await router.route(
            "Schalte das Licht im Wohnzimmer ein", entities=ROOM_CATALOG
        )

    decision = run(scenario())
    state_ids = {
        line.split(":", 1)[0].removeprefix("- ")
        for line in jev.calls[0].splitlines()
        if line.startswith("- ")
    }
    prompt_ids = {
        line.split(":", 1)[0].removeprefix("- ")
        for line in deep.calls[0][0].splitlines()
        if line.startswith("- ")
    }
    assert state_ids == prompt_ids == set(ROOM_CATALOG)
    assert decision.raw[CATALOG_SELECTION_KEY]["selected"] == len(ROOM_CATALOG)


def test_entity_variant_falls_back_to_class_above_255() -> None:
    """A-10: >255 ⇒ **WARN + Fallback auf A** (kein HTTP 400) bleibt bestehen.

    Der Guard wirkt auf den **rohen** Katalog; die Relevanz-Begrenzung darf ihn
    nicht aushebeln (sonst fiele der Cache nie zurück und HTTP 400 wäre möglich).
    """
    big_catalog = {
        f"light.wohnzimmer_{index:03d}": f"Wohnzimmer {index}"
        for index in range(300)
    }
    jev = FakeJev()
    deep = FakeDeepSeek(
        {
            "entity_id": "light.wohnzimmer_000",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "Wohnzimmer 0 an.",
        }
    )

    async def scenario() -> RouteDecision:
        router = Router(
            jev_client=jev,
            deepseek_client=deep,
            ha_client=FakeHa(),
            variant="entity",
        )
        return await router.route(
            "Schalte das Licht im Wohnzimmer 0 ein", entities=big_catalog
        )

    decision = run(scenario())
    # Variante D (kein Jev #2) wurde **nicht** genutzt ⇒ Quelle ist Jev+DeepSeek.
    assert decision.source == "jev+deepseek"
    # Aber der Prompt blieb begrenzt (120 Zeilen) statt 300 zu übergeben.
    prompt = deep.calls[0][0]
    assert prompt.count("\n- ") <= PROMPT_CATALOG_LIMIT
    assert len(big_catalog) == 300 > JEV_MAX_CHOICES


def test_entity_outside_the_sent_catalog_is_rejected_fail_closed() -> None:
    """A-6: **gesendete** Auswahl ist die Schranke, nicht der volle Cache.

    DeepSeek kann aus dem Gedächtnis eine Entity nennen, die im Cache liegt,
    aber in der auf 120 gekürzten Liste **nicht** stand.  Solche Kandidaten
    werden abgewiesen (kein Service-Call) – sonst könnte die Begrenzung das
    Modell nur verärgern, ohne die Sichtbarkeit zu begrenzen.
    """
    catalog: dict[str, Any] = {
        f"light.kueche_{index:03d}": f"Küche Licht {index}" for index in range(121)
    }
    catalog.update(
        {f"switch.wohnzimmer_{index:03d}": f"Wohnzimmer {index}" for index in range(121)}
    )
    dropped = "light.kueche_120"  # alphabetisch letzter Treffer ⇒ unter dem Cut

    async def scenario() -> None:
        router = make_router(
            deepseek=FakeDeepSeek(
                {
                    "entity_id": dropped,
                    "domain": "light",
                    "service": "turn_on",
                    "service_data": {},
                    "response_text": "Küche Licht 120 an.",
                }
            ),
            ha=FakeHa(),
        )
        with pytest.raises(AllowlistViolationError):
            await router.route(
                "Schalte das Licht in der Küche ein", entities=catalog
            )

    run(scenario())


def test_entity_inside_the_sent_catalog_still_passes_after_truncation() -> None:
    """Gegenprobe zum fail-closed-Test: der überlebende Kandidat **arbeitet**."""
    catalog: dict[str, Any] = {
        f"light.kueche_{index:03d}": f"Küche Licht {index}" for index in range(121)
    }
    catalog.update(
        {f"switch.wohnzimmer_{index:03d}": f"Wohnzimmer {index}" for index in range(121)}
    )
    ha = FakeHa()
    deep = FakeDeepSeek(
        {
            "entity_id": "light.kueche_000",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "Küche Licht 0 an.",
        }
    )

    async def scenario() -> RouteDecision:
        router = make_router(deepseek=deep, ha=ha)
        decision = await router.route(
            "Schalte das Licht in der Küche ein", entities=catalog
        )
        await router.execute(decision)
        return decision

    decision = run(scenario())
    assert decision.raw[CATALOG_SELECTION_KEY]["truncated"] is True
    assert decision.raw[CATALOG_SELECTION_KEY]["selected"] == PROMPT_CATALOG_LIMIT
    assert ha.service_calls == [("light", "turn_on", "light.kueche_000", {})]


def test_relevance_folds_uppercase_umlauts() -> None:
    """A-7: Der Fold deckt **Großbuchstaben** – ``KÜCHE`` ist ein Treffer.

    ``_fold`` lowered **nach** dem Translate.  Ohne ``Ä/Ö/Ü/ẞ`` im Mapping
    bliebe ein alleinstehendes ``"Ü"`` ungefaltet, der Namens-Token ``"küche"``
    fände dann keinen Treffer und die Entity fiele aus der Auswahl.
    """
    assert _fold("KÜCHENLICHT") == "kuechenlicht"
    assert _fold("Grüße aus Köln") == "gruesse aus koeln"
    catalog = {"light.a": "KÜCHE", "light.b": "Bad"}
    selection = _select_relevant_entities(
        "Schalte das Licht in der Küche ein", catalog, limit=1
    )
    assert selection.mode == "relevance"
    assert selection.matches == 1
    assert set(selection.entities) == {"light.a"}


# ── 8. P12.T2b / Block B: Service-Parameter (B-1 … B-6, E114/O-2) ──────
# Kerndefekt von T2a: mit ``ROUTER_VARIANT=class`` als **live aktivem** Pfad
# war die Parameter-Allowlist **wirkungslos** – jeder von DeepSeek gelieferte
# Key ging ungefiltert an ``ha_client.call_service``, und der Dienst selbst
# wurde nur auf Leerheit geprüft.  Diese Tests prüfen den **aktiven** Pfad.
B_NO_BRIGHTNESS = "Diese Lampe kann ich nicht dimmen."

#: Katalog mit **Attributdaten**, weil Allowlist-Bereich, Enum-Optionen und
#: das Feature-Gate sie brauchen.  ``supported_features`` 6 = Bit 1
#: ``SUPPORT_BRIGHTNESS`` + Bit 2 ``EFFECT``; 4 = **nur** EFFECT (Live-Fall).
B_CATALOG: dict[str, Any] = {
    "light.dim": {
        "friendly_name": "Dimmlicht",
        "attributes": {"supported_features": 6},
    },
    "light.plain": {
        "friendly_name": "Flurlicht",
        "attributes": {"supported_features": 4},
    },
    "climate.heizung": {
        "friendly_name": "Heizung",
        "attributes": {"min_temp": 18.0, "max_temp": 22.0},
    },
    "media_player.lautsprecher": {
        "friendly_name": "Lautsprecher",
        "attributes": {},
    },
    "select.modus": {
        "friendly_name": "Modus",
        "attributes": {"options": ["automatisch", "manuell"]},
    },
    "number.schwellwert": {
        "friendly_name": "Schwellwert",
        "attributes": {"min": 0.0, "max": 10.0, "step": 0.5},
    },
    "cover.jalousie": {"friendly_name": "Jalousie", "attributes": {}},
    "fan.ventilator": {"friendly_name": "Ventilator", "attributes": {}},
}


def b_route(
    payload: Mapping[str, Any],
    *,
    transcript: str = "mach das",
    entities: Optional[Mapping[str, Any]] = None,
    jev_mode: Optional[str] = None,
) -> tuple[RouteDecision, FakeHa, list[Any]]:
    """Ein class-/`off`-Turn mit festem DeepSeek-Payload; Call **wird** ausgeführt."""
    catalog = dict(B_CATALOG if entities is None else entities)
    ha = FakeHa()
    deep = FakeDeepSeek(dict(payload))
    jev = FakeJev(score=0.95, target="light")

    async def scenario() -> RouteDecision:
        router = make_router(jev=jev, deepseek=deep, ha=ha, jev_mode=jev_mode)
        return await router.handle(transcript, entities=catalog)

    decision = run(scenario())
    return decision, ha, deep.calls


# ── B-1: harte Domain→Service-Whitelist ──────────────────────────────────
def test_domain_service_criteria_covers_every_cache_domain() -> None:
    """Jede der **16** Cache-Domains (E113/A-1) hat einen Eintrag – eine Domain
    ohne Eintrag hieße, jeder Befehl für sie scheitert grundsätzlich."""
    assert set(DOMAIN_SERVICE_CRITERIA) == set(HA_ENTITY_DOMAINS)
    assert len(DOMAIN_SERVICE_CRITERIA) == 16
    for domain, services in DOMAIN_SERVICE_CRITERIA.items():
        assert services, f"{domain} ohne erlaubten Dienst"
        assert all(service.strip() == service for service in services)
    # E113 bleibt: die drei gesperrten Domains bekommen **keine** Dienste.
    for blocked in HA_BLOCKED_DOMAINS:
        assert blocked not in DOMAIN_SERVICE_CRITERIA


def test_class_path_rejects_service_outside_domain_criteria() -> None:
    """``climate.set_fan_mode`` steht nicht in der Tabelle ⇒ **kein** Call,
    sondern ein ehrlicher Fehler-Turn (kein stilles Abschneiden)."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "climate.heizung",
            "domain": "climate",
            "service": "set_fan_mode",
            "service_data": {},
            "response_text": "Lüftermodus gesetzt.",
        },
        transcript="schalte die Heizung auf einen anderen Lüftermodus",
    )

    assert ha.service_calls == []
    assert decision.executed is False
    assert decision.is_error is True
    # Schema-/Protokolldefekt des Modells ⇒ v4-§7.2-DeepSeek-Fallback.
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL


def test_off_path_rejects_service_outside_domain_criteria() -> None:
    """Auch ``JEV_MODE=off`` (Variante C) ist an die Tabelle gebunden – das
    war der Pfad mit der dünnsten Absicherung."""
    decision, ha, _deep = b_route(
        {
            "intent": "COMMAND",
            "target_class": "light",
            "entity_id": "light.dim",
            "service": "flash",
            "service_data": {},
            "response_text": "Blitz.",
        },
        transcript="blitz das licht",
        jev_mode="off",
    )

    assert ha.service_calls == []
    assert decision.is_error is True
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL


# ── B-2/B-3: Allowlist + Validator im aktiven class-Pfad ─────────────────
def test_class_path_uses_climate_set_temperature() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "climate.heizung",
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"temperature": 21.5},
            "response_text": "Heizung auf 21,5 Grad.",
        },
        transcript="mach es auf 21,5 Grad",
    )

    assert decision.service == "set_temperature"
    assert dict(decision.service_data) == {"temperature": 21.5}
    assert ha.service_calls == [
        ("climate", "set_temperature", "climate.heizung", {"temperature": 21.5})
    ]


def test_class_path_rejects_temperature_outside_entity_range() -> None:
    """``max_temp = 22`` der Entity ist strenger als die Plausibilität."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "climate.heizung",
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"temperature": 25.0},
            "response_text": "Heizung auf 25 Grad.",
        },
        transcript="mach es auf 25 Grad",
    )

    assert ha.service_calls == []
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert decision.executed is False


def test_class_path_drops_unknown_service_data_keys() -> None:
    """Nur der erlaubte Key überlebt – ``hvac_mode`` wird verworfen."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "climate.heizung",
            "domain": "climate",
            "service": "set_temperature",
            "service_data": {"temperature": 20.0, "hvac_mode": "heat"},
            "response_text": "Heizung auf 20 Grad.",
        },
        transcript="mach es auf 20 Grad warm",
    )

    assert dict(decision.service_data) == {"temperature": 20.0}
    assert ha.service_calls == [
        ("climate", "set_temperature", "climate.heizung", {"temperature": 20.0})
    ]


def test_class_path_select_option_must_be_entity_option() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "select.modus",
            "domain": "select",
            "service": "select_option",
            "service_data": {"option": "manuell"},
            "response_text": "Modus manuell.",
        },
        transcript="stell den Modus auf manuell",
    )
    assert ha.service_calls == [
        ("select", "select_option", "select.modus", {"option": "manuell"})
    ]

    rejected, ha2, _ = b_route(
        {
            "entity_id": "select.modus",
            "domain": "select",
            "service": "select_option",
            "service_data": {"option": "egal"},
            "response_text": "Modus egal.",
        },
        transcript="stell den Modus auf egal",
    )
    assert ha2.service_calls == []
    assert rejected.error_code == "ENTITY_PARAM_MISSING"


def test_class_path_number_value_is_ranged_and_snapped_to_step() -> None:
    """``min=0``/``max=10``/``step=0.5`` ⇒ 7.3 ⇒ 7.5, 42 ⇒ Ablehnung."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "number.schwellwert",
            "domain": "number",
            "service": "set_value",
            "service_data": {"value": 7.3},
            "response_text": "Schwellwert 7.5.",
        },
        transcript="stell den Schwellwert auf 7,3",
    )
    assert dict(decision.service_data) == {"value": 7.5}
    assert ha.service_calls == [
        ("number", "set_value", "number.schwellwert", {"value": 7.5})
    ]

    rejected, ha2, _ = b_route(
        {
            "entity_id": "number.schwellwert",
            "domain": "number",
            "service": "set_value",
            "service_data": {"value": 42},
            "response_text": "Schwellwert 42.",
        },
        transcript="stell den Schwellwert auf 42",
    )
    assert ha2.service_calls == []
    assert rejected.error_code == "ENTITY_PARAM_MISSING"


def test_class_path_cover_position_and_fan_percentage() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "cover.jalousie",
            "domain": "cover",
            "service": "set_cover_position",
            "service_data": {"position": 30},
            "response_text": "Jalousie auf 30 Prozent.",
        },
        transcript="fahre die Jalousie auf 30 Prozent",
    )
    assert ha.service_calls == [
        ("cover", "set_cover_position", "cover.jalousie", {"position": 30})
    ]

    _fan, ha2, _ = b_route(
        {
            "entity_id": "fan.ventilator",
            "domain": "fan",
            "service": "set_percentage",
            "service_data": {"percentage": 60},
            "response_text": "Ventilator auf 60 Prozent.",
        },
        transcript="dreh den Ventilator auf 60 Prozent",
    )
    assert ha2.service_calls == [
        ("fan", "set_percentage", "fan.ventilator", {"percentage": 60})
    ]


def test_class_path_service_data_must_be_an_object() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "turn_on",
            "service_data": ["brightness_pct", 40],
            "response_text": "Licht an.",
        },
        transcript="mach das Licht auf 40 Prozent",
    )

    assert ha.service_calls == []
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL


# ── B-5: semantisch ≠ Wire (Skalierung genau am Call-Punkt) ─────────────
def test_class_path_keeps_percent_semantically_and_scales_only_at_call() -> None:
    """``service_data`` bleibt **Prozent** (Anzeige „40 Prozent"), der Call
    bekommt ``0.4`` – HA verlangt 0.0–1.0."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "media_player.lautsprecher",
            "domain": "media_player",
            "service": "volume_set",
            "service_data": {"volume_level": 40},
            "response_text": "Lautsprecher auf 40 Prozent.",
        },
        transcript="dreh den lautsprecher auf 40",
    )

    assert decision.response_text == "Lautsprecher auf 40 Prozent."
    assert dict(decision.service_data) == {"volume_level": 40}  # semantisch
    assert ha.service_calls == [
        ("media_player", "volume_set", "media_player.lautsprecher", {"volume_level": 0.4})
    ]


def test_wire_service_data_scales_only_the_allowlisted_key() -> None:
    assert _wire_service_data(
        "media_player", "volume_set", {"volume_level": 100}
    ) == {"volume_level": 1.0}
    assert _wire_service_data(
        "media_player", "volume_set", {"volume_level": 0}
    ) == {"volume_level": 0.0}
    # Alles andere bleibt unangetastet – kein Key ⇒ keine Skalierung, und
    # `volume_level` unter einem Paar ohne Key wird ebenfalls nicht angefasst.
    assert _wire_service_data("light", "turn_on", {"brightness_pct": 40}) == {
        "brightness_pct": 40
    }
    assert _wire_service_data("media_player", "turn_on", {"volume_level": 40}) == {
        "volume_level": 40
    }
    assert _wire_service_data("", "", {}) == {}
    assert dict(ENTITY_PARAM_WIRE_SCALE) == {"volume_level": 0.01}


def test_wire_service_data_survives_non_numeric_payload() -> None:
    """Ungültige Werte hat der Validator schon abgefangen; die Skalierung
    darf trotzdem **keine** Exception in den Call-Pfad tragen."""
    assert _wire_service_data(
        "media_player", "volume_set", {"volume_level": "hell"}
    ) == {"volume_level": "hell"}


# ── E114/O-2: Light ohne SUPPORT_BRIGHTNESS ─────────────────────────────
def test_class_path_light_without_brightness_support_is_rejected_honestly() -> None:
    """Der Kernfall des Users: 21 von 21 Live-Lights haben
    ``supported_features = 4`` ⇒ ``brightness_pct`` käme **nicht** an.

    Erwartung: **ehrliche Ablehnung mit eigenem Satz**, **kein** Service-Call –
    insbesondere **kein** stilles Einschalten („Mach das Licht auf 40 Prozent"
    ⇒ ``turn_on`` ohne Wert wäre eine gelogene Bestätigung).
    """
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.plain",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 40},
            "response_text": "Flurlicht auf 40 Prozent.",
        },
        transcript="mach das Flurlicht auf 40 Prozent",
    )

    assert ha.service_calls == []
    assert decision.executed is False
    assert decision.error_code == "ENTITY_PARAM_MISSING"
    assert decision.response_text == B_NO_BRIGHTNESS
    # Der Satz steht **nicht** in der Codebasis an zweiter Stelle ⇒ der Test
    # prüft die Zeichenkette selbst.
    assert B_NO_BRIGHTNESS != NOT_UNDERSTOOD
    assert decision.response_text == LIGHT_NO_BRIGHTNESS_TEXT


def test_class_path_light_without_brightness_support_ignores_plain_on() -> None:
    """Reines An/Aus **ohne** Zahlenwert bleibt möglich – sonst wäre jedes
    Light ohne ``SUPPORT_BRIGHTNESS`` tot."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.plain",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "Flurlicht an.",
        },
        transcript="mach das Flurlicht an",
    )

    assert decision.is_error is False
    assert ha.service_calls == [("light", "turn_on", "light.plain", {})]


def test_class_path_light_with_brightness_support_sends_brightness_pct() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 40},
            "response_text": "Dimmlicht auf 40 Prozent.",
        },
        transcript="mach das Licht auf 40 Prozent",
    )

    assert decision.is_error is False
    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert ha.service_calls == [
        ("light", "turn_on", "light.dim", {"brightness_pct": 40})
    ]


def test_class_path_light_without_support_rejects_even_without_number_in_text() -> None:
    """„dimme das Licht" **ohne** Zahl: DeepSeek hat trotzdem einen Wert
    geliefert ⇒ das Gate greift auch dann (kein „dann eben an")."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.plain",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 40},
            "response_text": "Flurlicht gedimmt.",
        },
        transcript="dimme das Flurlicht",
    )

    assert ha.service_calls == []
    assert decision.response_text == B_NO_BRIGHTNESS


# ── B-6: Prompt-Härtung ─────────────────────────────────────────────────
def test_command_prompt_lists_allowed_services_and_params() -> None:
    _decision, _ha, calls = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "an",
        },
        transcript="mach das Licht an",
    )

    prompt, system_prompt = calls[0]
    # Erlaubte Dienste **ausgeschrieben** statt „z. B. turn_on|turn_off|toggle".
    assert "Erlaubte Dienste" in prompt
    assert "light: toggle, turn_off, turn_on" in prompt
    assert "climate: " in prompt and "set_temperature" in prompt
    # Erlaubte Keys mit Bereich/Einheit.
    assert "light.turn_on: brightness_pct" in prompt
    assert "climate.set_temperature: temperature" in prompt
    assert "Prozent" in prompt or "0-100" in prompt
    # Anti-Raten-Regel im System-Prompt.
    assert system_prompt is not None
    assert "Erfinde weder einen Dienst noch einen Parameterwert" in system_prompt
    # Keine Platzhalter-Formulierung mehr.
    assert "z.B. turn_on|turn_off|toggle" not in prompt


def test_prompt_hint_only_lists_domains_from_the_sent_catalog() -> None:
    """Der Hinweis wächst **nicht** mit dem 16-Domain-Default, sondern folgt
    dem tatsächlich gesendeten Katalog (A-5)."""
    _decision, _ha, calls = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "turn_on",
            "service_data": {},
            "response_text": "an",
        },
        transcript="mach das Licht an",
        entities={
            "light.dim": {"friendly_name": "Dimmlicht", "attributes": {}},
            "switch.stecke": {"friendly_name": "Stecke", "attributes": {}},
        },
    )

    prompt = calls[0][0]
    assert "light: toggle, turn_off, turn_on" in prompt
    assert "switch: toggle, turn_off, turn_on" in prompt
    assert "climate: " not in prompt
    assert "media_player: " not in prompt


def test_off_prompt_also_carries_the_param_rules() -> None:
    _decision, _ha, calls = b_route(
        {
            "intent": "COMMAND",
            "target_class": "light",
            "entity_id": "light.dim",
            "service": "turn_on",
            "service_data": {},
            "response_text": "an",
        },
        transcript="mach das Licht an",
        jev_mode="off",
    )

    prompt, system_prompt = calls[0]
    assert "Erlaubte Dienste" in prompt
    assert "light.turn_on: brightness_pct" in prompt
    assert system_prompt is not None
    assert "Erfinde weder einen Dienst noch einen Parameterwert" in system_prompt


# ── Der eine Validator: identisches Verhalten in **allen** Varianten ─────
@pytest.mark.parametrize(
    "payload, transcript, expected",
    [
        # unbekannter Key ⇒ verworfen, erlaubter Key bleibt
        (
            {
                "entity_id": "light.dim",
                "domain": "light",
                "service": "turn_on",
                "service_data": {"brightness_pct": 40, "brightness": 200},
                "response_text": "Licht an.",
            },
            "mach das Licht auf 40 Prozent",
            {"brightness_pct": 40},
        ),
        # Key außerhalb der Range ⇒ kein Call
        (
            {
                "entity_id": "light.dim",
                "domain": "light",
                "service": "turn_on",
                "service_data": {"brightness_pct": 150},
                "response_text": "Licht an.",
            },
            "mach das Licht auf 150 Prozent",
            None,
        ),
        # Enum-Wert nicht in den Optionen ⇒ kein Call
        (
            {
                "entity_id": "select.modus",
                "domain": "select",
                "service": "select_option",
                "service_data": {"option": "egal"},
                "response_text": "Modus egal.",
            },
            "stell den Modus auf egal",
            None,
        ),
    ],
)
def test_validator_behaves_identically_in_all_variants(
    payload: Mapping[str, Any], transcript: str, expected: Optional[Mapping[str, Any]]
) -> None:
    """**Zaun:** derselbe Vorschlag, dasselbe Ergebnis in Variante D, ``class``
    und ``off`` – und im 255er-Cap-Rückfall (⇒ class)."""
    results: dict[str, Optional[dict[str, Any]]] = {}

    # class
    _d, ha_class, _ = b_route(payload, transcript=transcript)
    results["class"] = None if not ha_class.service_calls else ha_class.service_calls[0][3]

    # off (Variante C)
    off_payload = {
        "intent": "COMMAND",
        "target_class": payload["domain"],
        "entity_id": payload["entity_id"],
        "service": payload["service"],
        "service_data": payload["service_data"],
        "response_text": payload["response_text"],
    }
    _d, ha_off, _ = b_route(off_payload, transcript=transcript, jev_mode="off")
    results["off"] = None if not ha_off.service_calls else ha_off.service_calls[0][3]

    # Variante D (Jev wählt Entity **und** Dienst; DeepSeek liefert nur den Wert)
    from tests.test_router_entity import FakeEntityJev as _FakeEntityJev

    d_deep = FakeDeepSeek(dict(payload["service_data"]))
    d_jev = _FakeEntityJev(
        target=payload["entity_id"], service=payload["service"], needs_param=0.9
    )
    d_ha = FakeHa(dict(B_CATALOG))

    async def d_scenario() -> RouteDecision:
        return await Router(
            jev_client=d_jev,
            deepseek_client=d_deep,
            ha_client=d_ha,
            jev_mode="intent",
            variant="entity",
        ).handle(transcript, entities=dict(B_CATALOG))

    run(d_scenario())
    results["D"] = None if not d_ha.service_calls else d_ha.service_calls[0][3]

    for variant, value in results.items():
        assert value == expected, f"Variante {variant}: {value!r} != {expected!r}"


def test_cap_fallback_to_class_applies_the_same_validator() -> None:
    """Der 255er-Cap (A-10) fällt auf ``class`` zurück – und **dort** greift
    derselbe Validator: der unbekannte Key ``brightness`` fällt auch im
    Rückfall-Pfad weg.

    Die Füll-Entities tragen **keine Ziffern** und liegen in einer Domain, die
    der Transkript nicht nennt – der Zahlen-Token „40" wäre sonst selbst ein
    A-5-Treffer und die Payload-Entity fiele aus der Auswahl.
    """
    big = {
        f"switch.fuellung_{letters[index // 676 % 676]}{letters[index // 26 % 26]}"
        f"{letters[index % 26]}": {
            "friendly_name": f"Füllung {letters[index // 26 % 26]}",
            "attributes": {},
        }
        for index in range(300)
    }
    big["light.wohnzimmerlicht"] = {
        "friendly_name": "Wohnzimmerlicht",
        "attributes": {"supported_features": 6},
    }

    decision, ha, _deep = b_route(
        {
            "entity_id": "light.wohnzimmerlicht",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 40, "brightness": 200},
            "response_text": "Wohnzimmerlicht an.",
        },
        transcript="mach das Wohnzimmerlicht auf 40 Prozent",
        entities=big,
    )

    assert decision.raw[CATALOG_SELECTION_KEY]["truncated"] is True
    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert ha.service_calls == [
        ("light", "turn_on", "light.wohnzimmerlicht", {"brightness_pct": 40})
    ]


def test_allowlist_and_kind_tables_stay_consistent() -> None:
    """Jeder erlaubte Key braucht Art, Bereich/Plausibilität und Template –
    sonst wäre eine neue Domain still unvalidiert."""
    for key in ENTITY_PARAM_ALLOWLIST.values():
        assert key in ENTITY_PARAM_KIND
        assert key in ENTITY_PARAM_RESPONSE_TEMPLATES
    for key, kind in ENTITY_PARAM_KIND.items():
        if kind == "enum":
            continue
        assert key in ENTITY_PARAM_RANGES
    # Ausschalten braucht keinen Wert ⇒ kein Eintrag.
    assert not [pair for pair in ENTITY_PARAM_ALLOWLIST if pair[1].endswith("turn_off")]


# ══════════════════════════════════════════════════════════════════════════
# P12.T3b / E115 — der Name liegt in `attributes`, nicht auf oberster Ebene
#
# Home Assistant liefert ``{"entity_id": …, "state": …, "attributes":
# {"friendly_name": …}}``; der `EntityCache` cacht genau diesen vollen State
# (`app/ha_client.py:271`).  `_friendly_name()` las aber `value.get(
# "friendly_name")` ⇒ bei **echten** Entities **immer** leer: der Namensanteil
# des A-5-Relevance-Scores war live **0** (nur die `entity_id` zählte) und
# `_entity_lines()`/`_entity_criteria()` lieferten **namenlose** Zeilen — „Schalte
# das **Wohnzimmerlicht** an" fand das Licht also schlechter.
#
# **Bestehende flache Fixtures oben bleiben bewusst flach** (`CATALOG`,
# `B_CATALOG`, `ROOM_CATALOG`): sie belegen die Rückwärtskompatibilität des
# Fallbacks.  Die HA-realistischen Fixtures für den eigentlichen Bug stehen
# **hier** und nur hier.
# ══════════════════════════════════════════════════════════════════════════
from app.router import (  # noqa: E402
    FACTS_AVAILABLE_KEY,
    QUESTION_NO_DATA_TEXT,
    _entity_criteria,
    _entity_lines,
    _friendly_name,
    _relevance_score,
    _relevance_tokens,
    _state_display_name,
)

#: Wörtlich die Form aus einem echten `/api/states`-Response (gekürzt).
E115_HA_STATE: dict[str, Any] = {
    "entity_id": "light.wohnzimmer",
    "state": "off",
    "attributes": {"friendly_name": "Wohnzimmerlicht", "supported_features": 6},
}


# ── E115-1: `attributes.friendly_name` ist der Ort (HA-realistisch) ──────
def test_e115_01_attributes_friendly_name_is_the_source() -> None:
    assert _friendly_name(dict(E115_HA_STATE)) == "Wohnzimmerlicht"
    # Auch wenn ein flaches `friendly_name` **zusätzlich** dasteht, gewinnt der
    # HA-Ort – sonst hinge die Anzeige an einem Feld, das HA nie liefert.
    both = {
        "friendly_name": "FLACHER NAME",
        "attributes": {"friendly_name": "Wohnzimmerlicht"},
    }
    assert _friendly_name(both) == "Wohnzimmerlicht"


# ── E115-2: flaches `friendly_name` bleibt gültiger Fallback ─────────────
def test_e115_02_flat_friendly_name_still_works() -> None:
    assert _friendly_name({"friendly_name": "Steckdose Flur", "attributes": {}}) == (
        "Steckdose Flur"
    )
    # Reine Anzeigenamen (`str`-Kataloge) sind unverändert gültig.
    assert _friendly_name("Wohnzimmerlicht") == "Wohnzimmerlicht"
    # Der bestehende flache Testkatalog bleibt also unangetastet grün.
    assert _friendly_name(CATALOG["light.wohnzimmer"]) == "Wohnzimmerlicht"


# ── E115-3: ohne Namen dient die `entity_id` (Notnagel) ──────────────────
def test_e115_03_entity_id_is_the_safety_net_without_a_name() -> None:
    nameless = {"light.wohnzimmer": {"state": "off", "attributes": {}}}
    assert _entity_lines(nameless) == "- light.wohnzimmer"
    assert _entity_criteria(nameless) == {"light.wohnzimmer": "light.wohnzimmer"}
    assert (
        _state_display_name({"state": "off", "attributes": {}}, "light.wohnzimmer")
        == "light.wohnzimmer"
    )


# ── E115-4: kaputter Name ⇒ **keine** Exception, Notnagel greift ─────────
@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        [],
        {"attributes": None},
        {"attributes": "kein Mapping"},
        {"friendly_name": None},
        {"friendly_name": 42},
        {"friendly_name": "   "},
        {"attributes": {"friendly_name": None}},
    ],
)
def test_e115_04_broken_names_never_raise(value: Any) -> None:
    assert _friendly_name(value) == ""
    assert _entity_criteria({"light.x": value}) == {"light.x": "light.x"}


# ── E115-5: der eigentliche Bug — Namens-Score live > 0 ─────────────────
#: Drei Kandidaten für „Schalte das **Wohnzimmerlicht** an":
#:
#: * ``light.zzz_spot`` – der **Name** passt, die ``entity_id`` sagt nichts
#:   (alphabetisch **letzter** ⇒ ohne Namens-Signal verliert er);
#: * ``light.wohnzimmerlicht`` – **nur** die ``entity_id`` passt, es gibt dort
#:   gar keinen Anzeigenamen;
#: * ``light.aaa`` – passt nicht und steht alphabetisch ganz vorn.
E115_CATALOG: dict[str, Any] = {
    "light.zzz_spot": {
        "state": "off",
        "attributes": {"friendly_name": "Wohnzimmerlicht"},
    },
    "light.wohnzimmerlicht": {"state": "off", "attributes": {}},
    "light.aaa": {"state": "off", "attributes": {"friendly_name": "Abstelllicht"}},
}
E115_TRANSCRIPT = "Schalte das Wohnzimmerlicht an"


def test_e115_05_name_from_attributes_scores_and_beats_entity_id() -> None:
    tokens = _relevance_tokens(E115_TRANSCRIPT)
    assert tokens == frozenset({"wohnzimmerlicht"})

    # **Vor** dem Fix war der Namensanteil live 0 …
    assert _relevance_score(tokens, "light.zzz_spot", "") == 0
    # … jetzt trägt der Name 3 (Worttreffer) + 1 (Teiltreffer).
    with_name = _relevance_score(tokens, "light.zzz_spot", "Wohnzimmerlicht")
    only_id = _relevance_score(tokens, "light.wohnzimmerlicht", "")
    assert with_name > 0
    assert with_name > _relevance_score(tokens, "light.zzz_spot", "")
    assert with_name > only_id

    # Die Auswahl folgt dem Score: bei Limit 1 gewinnt der **Namens**-Treffer –
    # nicht der `entity_id`-Treffer und nicht der alphabetisch vordere.
    top = _select_relevant_entities(E115_TRANSCRIPT, E115_CATALOG, limit=1)
    assert set(top.entities) == {"light.zzz_spot"}
    assert top.mode == "relevance"
    assert top.matches == 2
    assert top.candidates == 3
    assert top.truncated is True

    # Prompt-Zeilen und Jev-``criteria`` tragen jetzt den **Namen**.
    assert "- light.zzz_spot: Wohnzimmerlicht" in _entity_lines(E115_CATALOG)
    assert _entity_criteria(E115_CATALOG)["light.zzz_spot"] == "Wohnzimmerlicht"
    assert _entity_criteria(E115_CATALOG)["light.aaa"] == "Abstelllicht"
    # Der namenlose Kandidat behält seinen Notnagel.
    assert _entity_criteria(E115_CATALOG)["light.wohnzimmerlicht"] == (
        "light.wohnzimmerlicht"
    )


# ── E115-6: Regression — Negation, B-Validator, C-Sensor-Fallback ────────
def test_e115_06a_negation_still_blocks_the_named_light() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.wohnzimmer",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.wohnzimmer"},
            "response_text": "Wohnzimmerlicht an.",
        },
        transcript="Schalte das Wohnzimmerlicht nicht an",
    )
    assert decision.intent == ERROR
    assert decision.error_code == NEGATION_CODE
    assert decision.response_text == NEGATION_TEXT
    assert ha.service_calls == []


def test_e115_06b_parameter_validator_still_drops_unknown_keys() -> None:
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "turn_on",
            "service_data": {
                "entity_id": "light.dim",
                "brightness_pct": 40,
                "brightness": 200,
            },
            "response_text": "Dimmlicht an.",
        },
        transcript="mach das Licht auf 40 Prozent",
    )
    assert dict(decision.service_data) == {"brightness_pct": 40}
    assert ha.service_calls == [
        ("light", "turn_on", "light.dim", {"brightness_pct": 40})
    ]


def test_e115_06c_sensor_question_without_value_stays_honest() -> None:
    """C-4: ohne passenden HA-Wert der ehrliche Fallback, **kein** LLM-Call."""
    ha = FakeHa()
    deep = FakeDeepSeek(QUESTION_PAYLOAD)

    async def scenario() -> RouteDecision:
        return await make_router(
            jev=FakeJev(score=0.1, target=None), deepseek=deep, ha=ha
        ).handle("Wie warm ist es im Wohnzimmer?", entities=CATALOG)

    decision = run(scenario())
    assert decision.intent == QUESTION
    assert decision.response_text == QUESTION_NO_DATA_TEXT
    assert decision.raw[FACTS_AVAILABLE_KEY] is False
    assert deep.calls == []
    assert ha.service_calls == []


# ── E115-7: A-5-Hilfe und C-Helfer nutzen denselben Pfad ─────────────────
@pytest.mark.parametrize(
    "state",
    [
        {"attributes": {"friendly_name": "Wohnzimmerlicht"}},
        {"friendly_name": "Flacher Name"},
        {
            "friendly_name": "FLACH",
            "attributes": {"friendly_name": "Wohnzimmerlicht"},
        },
        {"attributes": {}},
        {"attributes": None, "friendly_name": None},
        {},
    ],
)
def test_e115_07_both_name_helpers_resolve_identically(state: dict[str, Any]) -> None:
    entity_id = "light.wohnzimmer"
    assert (_friendly_name(state) or entity_id) == _state_display_name(
        state, entity_id
    )


# ── 9. P12.T4-4 / B-1: nackt **oder** qualifiziert (`light.turn_on`) ───────
# Live-Befund T4-3a: DeepSeek lieferte ``light.turn_on``, die Kriterien sind
# **nackt** (HA-Notation) ⇒ ``PROTOCOL_ERROR`` ⇒ die ganze ``light``-Domain war
# live blockiert.  Die Form wird jetzt an **einer** Stelle vereinheitlicht
# (``_normalize_service_name``); die Schranke selbst bleibt unangetastet.
SWITCH_CATALOG: dict[str, Any] = {
    **B_CATALOG,
    "switch.steckdose": {"friendly_name": "Steckdose Flur", "attributes": {}},
}


@pytest.mark.parametrize(
    "raw,domain,expected",
    [
        ("turn_on", "light", "turn_on"),
        ("  turn_off  ", "light", "turn_off"),
        ("light.turn_on", "light", "turn_on"),
        ("light.toggle", "light", "toggle"),
        ("switch.turn_on", "switch", "turn_on"),
        ("media_player.volume_set", "media_player", "volume_set"),
    ],
)
def test_normalize_service_name_accepts_both_forms(
    raw: str, domain: str, expected: str
) -> None:
    assert _normalize_service_name(raw, domain) == expected


@pytest.mark.parametrize(
    "raw,domain",
    [
        ("light.turn_on", "switch"),  # Domain-Mismatch
        ("switch.turn_on", "light"),
        ("homeassistant.turn_on", "light"),  # unerlaubte Domain
        ("light.a.b", "light"),  # mehr als ein Punkt
        ("light.", "light"),  # kein Dienst
        (" light.turn_on", "switch"),
    ],
)
def test_normalize_service_name_rejects_foreign_or_broken(raw: str, domain: str) -> None:
    """Falsches Präfix bzw. kaputte Form ⇒ :class:`RouterProtocolError` ⇒
    fail-closed, **kein** Call.

    Die *Erlaubnis* entscheidet weiterhin :data:`DOMAIN_SERVICE_CRITERIA`: ein
    syntaktisch gültiger, aber unbekannter Dienst (``light.explode``) wird vom
    Normalizer **nicht** bewertet, sondern von der Kriterien-Tabelle (siehe
    ``test_class_path_rejects_foreign_or_unknown_qualified_name``)."""
    with pytest.raises(RouterProtocolError):
        _normalize_service_name(raw, domain)


def test_normalize_service_name_does_not_grant_permission() -> None:
    """Der Normalizer ist **nur** Form – die Schranke bleibt B-1."""
    assert _normalize_service_name("light.explode", "light") == "explode"
    assert "explode" not in DOMAIN_SERVICE_CRITERIA["light"]


def test_class_path_live_regression_qualified_light_turn_on() -> None:
    """**Der Live-Fall** aus T4-3a („dimme die Lichtsteuerung im Wohnzimmer auf
    40 %"): DeepSeek liefert ``light.turn_on`` ⇒ previously ``PROTOCOL_ERROR``.

    Heute: nackter Dienst, ``service_data`` mit ``brightness_pct``, ein Call."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "light.dim",
            "domain": "light",
            "service": "light.turn_on",
            "service_data": {"brightness_pct": 40},
            "response_text": "Licht an.",
        },
        transcript="dimme die Lichtsteuerung im Wohnzimmer auf 40 Prozent",
    )

    assert decision.executed is True
    assert decision.error_code is None
    assert decision.service == "turn_on"
    assert dict(decision.service_data) == {"brightness_pct": 40}
    # HA bekommt den **nackten** Dienst (so erwartet es `call_service`).
    assert ha.service_calls == [
        ("light", "turn_on", "light.dim", {"brightness_pct": 40})
    ]


@pytest.mark.parametrize(
    "service",
    [
        "light.turn_on",  # Domain-Mismatch: Ziel ist ein `switch`
        "homeassistant.turn_on",  # unerlaubte Domain
        "light.explode",  # unbekannter Dienst
        "light.a.b",  # kaputte Form
    ],
)
def test_class_path_rejects_foreign_or_unknown_qualified_name(service: str) -> None:
    """Fail-closed unverändert: **kein** Call, ``PROTOCOL_ERROR``, wörtlicher
    v4-§7.2-Fallback."""
    decision, ha, _deep = b_route(
        {
            "entity_id": "switch.steckdose",
            "domain": "switch",
            "service": service,
            "service_data": {},
            "response_text": "Eingeschaltet.",
        },
        transcript="schalte die Steckdose ein",
        entities=SWITCH_CATALOG,
    )

    assert ha.service_calls == []
    assert decision.executed is False
    assert decision.error_code == "PROTOCOL_ERROR"
    assert decision.response_text == DEEPSEEK_FAIL


@pytest.mark.parametrize(
    "entity_id,domain,service,service_data,transcript,expected_data",
    [
        (
            "light.dim", "light", "turn_on", {"brightness_pct": 40},
            "dimme das Licht auf 40 Prozent", {"brightness_pct": 40},
        ),
        (
            "climate.heizung", "climate", "set_temperature", {"temperature": 21.0},
            "mach die Heizung auf 21 Grad", {"temperature": 21.0},
        ),
        (
            "media_player.lautsprecher", "media_player", "volume_set",
            {"volume_level": 40}, "mach den Lautsprecher auf 40 Prozent",
            {"volume_level": 0.4},  # B-5: Wire-Skalierung am Call-Punkt
        ),
        (
            "cover.jalousie", "cover", "set_cover_position", {"position": 60},
            "fahre die Jalousie auf 60 Prozent", {"position": 60},
        ),
        (
            "fan.ventilator", "fan", "set_percentage", {"percentage": 30},
            "stell den Ventilator auf 30 Prozent", {"percentage": 30},
        ),
    ],
)
@pytest.mark.parametrize("qualified", [False, True])
def test_both_service_name_forms_agree_in_every_domain(
    entity_id: str,
    domain: str,
    service: str,
    service_data: Mapping[str, Any],
    transcript: str,
    expected_data: Mapping[str, Any],
    qualified: bool,
) -> None:
    """**Beide** Formen ⇒ derselbe Call – in fünf Domains (nicht nur ``light``),
    mit Parameter, Wire-Skalierung inklusive."""
    decision, ha, _deep = b_route(
        {
            "entity_id": entity_id,
            "domain": domain,
            "service": f"{domain}.{service}" if qualified else service,
            "service_data": dict(service_data),
            "response_text": "Mach ich.",
        },
        transcript=transcript,
    )

    assert decision.executed is True
    assert decision.service == service
    assert ha.service_calls == [(domain, service, entity_id, dict(expected_data))]


def test_off_path_accepts_qualified_name_and_scales_to_wire() -> None:
    """Auch der dünnste Pfad (``JEV_MODE=off``) normalisiert – und skaliert
    weiterhin **genau einmal** am Call-Punkt."""
    decision, ha, _deep = b_route(
        {
            "intent": "COMMAND",
            "target_class": "media_player",
            "entity_id": "media_player.lautsprecher",
            "service": "media_player.volume_set",
            "service_data": {"volume_level": 40},
            "response_text": "Lauter.",
        },
        transcript="mach den Lautsprecher auf 40 Prozent",
        jev_mode="off",
    )

    assert decision.executed is True
    assert decision.service == "volume_set"
    assert dict(decision.service_data) == {"volume_level": 40}  # semantisch
    assert ha.service_calls == [
        ("media_player", "volume_set", "media_player.lautsprecher", {"volume_level": 0.4})
    ]


@pytest.mark.parametrize("qualified", [False, True])
def test_qualified_names_behave_identically_in_all_variants(qualified: bool) -> None:
    """**Zaun über alle vier Pfade:** Variante D (``entity``), ``class``,
    ``off`` und der 255er-Cap-Rückfall (⇒ ``class``) führen mit qualifiziertem
    Namen zum **selben** Call – mit nacktem Dienst am HA."""
    payload = {
        "entity_id": "light.dim",
        "domain": "light",
        "service": "light.turn_on" if qualified else "turn_on",
        "service_data": {"brightness_pct": 40},
        "response_text": "Licht an.",
    }
    results: dict[str, Any] = {}

    # class (live aktiver Pfad)
    _d, ha_class, _ = b_route(payload, transcript="dimme das Licht auf 40 Prozent")
    results["class"] = ha_class.service_calls

    # off (Variante C)
    off_payload = {
        "intent": "COMMAND",
        "target_class": "light",
        "entity_id": payload["entity_id"],
        "service": payload["service"],
        "service_data": payload["service_data"],
        "response_text": payload["response_text"],
    }
    _d, ha_off, _ = b_route(
        off_payload, transcript="dimme das Licht auf 40 Prozent", jev_mode="off"
    )
    results["off"] = ha_off.service_calls

    # Variante D (Jev wählt Entity **und** Dienst, DeepSeek nur den Wert)
    from tests.test_router_entity import FakeEntityJev as _FakeEntityJev

    d_jev = _FakeEntityJev(
        target="light.dim",
        service="light.turn_on" if qualified else "turn_on",
        needs_param=0.9,
    )
    d_ha = FakeHa(dict(B_CATALOG))
    d_deep = FakeDeepSeek({"brightness_pct": 40})

    async def d_scenario() -> RouteDecision:
        return await Router(
            jev_client=d_jev,
            deepseek_client=d_deep,
            ha_client=d_ha,
            jev_mode="intent",
            variant="entity",
        ).handle("dimme das Licht auf 40 Prozent", entities=dict(B_CATALOG))

    run(d_scenario())
    results["D"] = d_ha.service_calls

    # 255er-Cap-Rückfall (A-10) ⇒ derselbe class-Pfad
    big = {
        f"switch.fuellung_{letters[index // 676 % 676]}{letters[index // 26 % 26]}"
        f"{letters[index % 26]}": {
            "friendly_name": f"Füllung {letters[index // 26 % 26]}",
            "attributes": {},
        }
        for index in range(300)
    }
    big["light.wohnzimmerlicht"] = {
        "friendly_name": "Wohnzimmerlicht",
        "attributes": {"supported_features": 6},
    }
    cap_payload = {
        **payload,
        "entity_id": "light.wohnzimmerlicht",
    }
    _d, ha_cap, _ = b_route(
        cap_payload,
        transcript="dimme das Wohnzimmerlicht auf 40 Prozent",
        entities=big,
    )
    results["cap"] = ha_cap.service_calls

    expected_class = [("light", "turn_on", "light.dim", {"brightness_pct": 40})]
    expected_cap = [("light", "turn_on", "light.wohnzimmerlicht", {"brightness_pct": 40})]
    assert results["class"] == expected_class
    assert results["off"] == expected_class
    assert results["D"] == expected_class
    assert results["cap"] == expected_cap


def test_service_name_form_is_normalized_before_every_lookup() -> None:
    """**Konsistenz der Vergleichsstellen:** die nackten Schlüssel
    (``DOMAIN_SERVICE_CRITERIA``, ``ENTITY_PARAM_ALLOWLIST``,
    ``ENTITY_RESPONSE_TEMPLATES``) sind nur gültig, wenn **jede** Stelle den
    normalisierten Namen sieht – der Test friert genau das ein."""
    for domain, services in DOMAIN_SERVICE_CRITERIA.items():
        for service in services:
            assert _normalize_service_name(f"{domain}.{service}", domain) == service
            assert _normalize_service_name(service, domain) == service
            # Ein fremdes Präfix darf **nie** in den nackten Schlüssel kippen.
            with pytest.raises(RouterProtocolError):
                _normalize_service_name(f"{domain}.{service}", "unbekannt")
            assert "." not in service
    for (domain, service) in ENTITY_PARAM_ALLOWLIST:
        assert "." not in service, (domain, service)
        assert _normalize_service_name(f"{domain}.{service}", domain) == service
