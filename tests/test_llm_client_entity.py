"""LLM-Client-Tests für **Variante D** (E90, P7.T4) — Layer **L0/`unit`**.

Prüfling sind die additiven Variante-D-Bausteine in `app/llm_client.py`
(Jev #1: `classify_intent` / `build_intent_only_questions` /
`parse_systemone_intent`; Jev #2: `classify_entity` / `build_entity_questions` /
`parse_systemone_entity`).  Der bestehende Variante-A-Vertrag bleibt unberührt.

**Kein echtes Netz.**  Gegenüber ist ein `httpx.MockTransport` (in-process, kein
Socket), Basis `http://mock-llm.invalid` (RFC 2606, nicht auflösbar).  Die
Prüfwerte sind Literale, nicht aus dem Prüfling zurückgelesen.

Abgedeckt:
* Request-Form Jev #1 (nur `intent`, `state` = Transkript).
* Request-Form Jev #2 (`target`+`service`, `criteria` inkl. Pflicht-`none`).
* Parsing beider Antwortformen (Wert+Score, `none`, fehlende Felder).
* `JEV_MODE=off` ⇒ `JevDisabledError` **ohne** HTTP.
* Optionsgrenze 255.
* **E91:** gemeldete Modell-ID in der Debug-Zeile beider Wege, Abweichung
  bzw. Fehlen ⇒ **eine** ``WARNING`` je Wert (kein Flooding).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import httpx
import pytest

from app.llm_client import (
    EntityChoiceResult,
    JevClient,
    JevDisabledError,
    JEV_MAX_CHOICES,
    JEV_SERVICE_CRITERIA,
    JEV_TARGET_NONE_LABEL,
    LlmConfigError,
    LlmProtocolError,
    NoulResult,
    build_entity_questions,
    build_intent_only_questions,
    parse_systemone_entity,
    parse_systemone_intent,
)

pytestmark = pytest.mark.unit

TOKEN = "test-token"
MOCK_BASE_URL = "http://mock-llm.invalid"
SYSTEMONE_PATH = "/systemone"
JEV_MODEL = "jev-1.13"

EXPECTED_INTENT_QUESTION = {
    "type": "noul",
    "instructions": "Will der Nutzer ein Gerät steuern?",
}

Responder = Callable[[httpx.Request], httpx.Response]


# ── Testdaten ────────────────────────────────────────────────────────────
def intent_payload(noul: float = 0.97) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "answers": {"intent": {"type": "noul", "noul": noul}},
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


def entity_payload(
    target: str = "light.wohnzimmer",
    target_conf: float = 1.0,
    service: str = "turn_on",
    service_conf: float = 1.0,
) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "answers": {
            "target": {
                "type": "choice",
                "choice": target,
                "confidence": target_conf,
                "probabilities": {target: 0.9},
            },
            "service": {
                "type": "choice",
                "choice": service,
                "confidence": service_conf,
            },
        },
        "usage": {"prompt_tokens": 20, "completion_tokens": 2},
    }


def make_transport(responder: Responder) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return responder(request)

    inner = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=MOCK_BASE_URL
    )
    return inner, recorded


def jev_client(responder: Responder, **kwargs: Any) -> tuple[JevClient, list[httpx.Request]]:
    inner, recorded = make_transport(responder)
    client = JevClient(
        client=inner, api_key=TOKEN, base_url=MOCK_BASE_URL, **kwargs
    )
    return client, recorded


def json_responder(payload: Any, *, status: int = 200) -> Responder:
    return lambda request: httpx.Response(status, json=payload)


# ── Builder ──────────────────────────────────────────────────────────────
def test_build_intent_only_questions_is_only_intent() -> None:
    questions = build_intent_only_questions()
    assert set(questions) == {"intent"}
    assert questions["intent"] == EXPECTED_INTENT_QUESTION


def test_build_entity_questions_adds_none_service_and_needs_param() -> None:
    """E92: dritte Frage `needs_param` (`noul`) im **selben** Jev-#2-Call."""
    criteria = {"light.wohnzimmer": "Wohnzimmerlicht", "switch.steckdose": "Steckdose Flur"}
    questions = build_entity_questions(criteria)

    assert set(questions) == {"target", "service", "needs_param"}
    assert questions["target"]["type"] == "choice"
    assert questions["target"]["instructions"] == "Welches Zielgerät?"
    assert questions["target"]["criteria"] == {
        "light.wohnzimmer": "Wohnzimmerlicht",
        "switch.steckdose": "Steckdose Flur",
        "none": JEV_TARGET_NONE_LABEL,
    }
    assert questions["service"]["criteria"] == {
        "turn_on": "einschalten",
        "turn_off": "ausschalten",
        "toggle": "umschalten",
    }
    assert questions["service"]["criteria"] == dict(JEV_SERVICE_CRITERIA)
    # `needs_param` ist eine reine Ja/Nein-Frage (noul) – **kein** Zahlenwert,
    # **kein** eigener Call, **keine** `criteria`.
    assert questions["needs_param"] == {
        "type": "noul",
        "instructions": (
            "Muss für diesen Befehl ein Zahlenwert angegeben werden "
            "(z. B. Helligkeit, Temperatur, Lautstärke)?"
        ),
    }


def test_build_entity_questions_keeps_explicit_none() -> None:
    questions = build_entity_questions(
        {"light.x": "X", "none": "mein eigener Text"}
    )
    assert questions["target"]["criteria"]["none"] == "mein eigener Text"


def test_build_entity_questions_rejects_empty() -> None:
    with pytest.raises(LlmConfigError):
        build_entity_questions({})


def test_jev_max_choices_is_255() -> None:
    assert JEV_MAX_CHOICES == 255


# ── Parser ───────────────────────────────────────────────────────────────
def test_parse_systemone_intent_reads_noul_without_target() -> None:
    result = parse_systemone_intent(intent_payload(0.97))
    assert result.intent.score == 0.97
    assert result.intent.value is True
    assert result.target is None
    assert result.model == JEV_MODEL


def test_parse_systemone_intent_rejects_missing_intent() -> None:
    with pytest.raises(LlmProtocolError):
        parse_systemone_intent({"answers": {}})
    with pytest.raises(LlmProtocolError):
        parse_systemone_intent({"model": "x"})


def test_parse_systemone_entity_reads_target_and_service() -> None:
    result = parse_systemone_entity(entity_payload("switch.steckdose", 0.88, "toggle"))
    assert isinstance(result, EntityChoiceResult)
    assert result.target.value == "switch.steckdose"
    assert result.target.score == 0.88
    assert result.service.value == "toggle"
    assert result.service.score == 1.0
    assert result.target.probabilities == {"switch.steckdose": 0.9}


def test_parse_systemone_entity_reads_none() -> None:
    result = parse_systemone_entity(entity_payload("none", 0.91, "turn_off"))
    assert result.target.value == "none"
    assert result.target.score == 0.91
    assert result.service.value == "turn_off"


def test_parse_systemone_entity_rejects_missing_service() -> None:
    payload = entity_payload()
    del payload["answers"]["service"]
    with pytest.raises(LlmProtocolError):
        parse_systemone_entity(payload)


def test_parse_systemone_entity_rejects_bool_confidence() -> None:
    payload = entity_payload()
    payload["answers"]["target"]["confidence"] = True
    with pytest.raises(LlmProtocolError):
        parse_systemone_entity(payload)


# ── End-to-End über den echten Client (MockTransport) ────────────────────
def test_classify_intent_posts_intent_only_and_parses() -> None:
    client, recorded = jev_client(json_responder(intent_payload(0.14)))

    async def scenario() -> Any:
        await client.start()
        try:
            return await client.classify_intent("Wie spät ist es?")
        finally:
            await client.stop()

    result = asyncio.run(scenario())

    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert request.url.path == SYSTEMONE_PATH
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["model"] == JEV_MODEL
    assert body["state"] == "Wie spät ist es?"  # nur Transkript, keine Entities
    assert body["questions"] == {"intent": EXPECTED_INTENT_QUESTION}
    assert result.intent.score == 0.14
    assert result.intent.value is False
    assert result.target is None


def test_classify_entity_posts_target_service_and_needs_param() -> None:
    client, recorded = jev_client(json_responder(entity_payload()))
    state = "Entitäten:\n- light.wohnzimmer: Wohnzimmerlicht\n\nTranskript: Licht an"

    async def scenario() -> Any:
        await client.start()
        try:
            return await client.classify_entity(
                state, {"light.wohnzimmer": "Wohnzimmerlicht"}
            )
        finally:
            await client.stop()

    result = asyncio.run(scenario())

    # E92: **ein** Call mit **drei** Fragen (kein zusätzlicher Round-Trip).
    assert len(recorded) == 1
    body = json.loads(recorded[0].content)
    assert body["state"] == state
    assert set(body["questions"]) == {"target", "service", "needs_param"}
    assert body["questions"]["target"]["criteria"] == {
        "light.wohnzimmer": "Wohnzimmerlicht",
        "none": JEV_TARGET_NONE_LABEL,
    }
    assert set(body["questions"]["service"]["criteria"]) == {
        "turn_on",
        "turn_off",
        "toggle",
    }
    assert body["questions"]["needs_param"]["type"] == "noul"
    assert result.target.value == "light.wohnzimmer"
    assert result.service.value == "turn_on"
    # Die Antwort im Payload enthält kein `needs_param` ⇒ konservativ `None`
    # (Router bleibt im v1-Verhalten, kein Param-Pfad).
    assert result.needs_param is None


def test_classify_intent_off_raises_without_http() -> None:
    client, recorded = jev_client(json_responder(intent_payload()), mode="off")

    async def scenario() -> None:
        with pytest.raises(JevDisabledError):
            await client.classify_intent("Licht an")

    asyncio.run(scenario())
    assert recorded == []


def test_classify_entity_off_raises_without_http() -> None:
    client, recorded = jev_client(json_responder(entity_payload()), mode="off")

    async def scenario() -> None:
        with pytest.raises(JevDisabledError):
            await client.classify_entity("Licht an", {"light.x": "X"})

    asyncio.run(scenario())
    assert recorded == []


def test_noul_threshold_still_half() -> None:
    assert NoulResult(score=0.49).value is False
    assert NoulResult(score=0.50).value is True


# ── E91: Modellidentität lokal beweisbar (Jev #1 / Jev #2) ───────────────
def _payload_model(payload: dict[str, Any], model: Any) -> dict[str, Any]:
    """Antwort mit wählbarer Modell-ID; ``_NO_MODEL`` ⇒ Schlüssel fehlt (E91)."""
    if model is _NO_MODEL:
        payload.pop("model", None)
    else:
        payload["model"] = model
    return payload


_NO_MODEL: Any = object()


def _records(caplog: Any, level: int) -> list[str]:
    """Log-Meldungen **dieses** Loggers auf ``level`` (fremde stören nicht)."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == "manager.llm_client"
    ]


def test_classify_intent_debug_line_carries_model(caplog: Any) -> None:
    client, _ = jev_client(json_responder(intent_payload(0.97)))

    async def scenario() -> Any:
        return await client.classify_intent("Licht an")

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    assert result.model == JEV_MODEL
    debug_lines = [line for line in _records(caplog, logging.DEBUG) if "model=" in line]
    assert len(debug_lines) == 1
    assert f"model={JEV_MODEL}" in debug_lines[0]
    assert _records(caplog, logging.WARNING) == []


def test_classify_entity_debug_line_carries_model(caplog: Any) -> None:
    client, _ = jev_client(json_responder(entity_payload()))

    async def scenario() -> Any:
        return await client.classify_entity("Licht an", {"light.x": "X"})

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    assert result.model == JEV_MODEL
    debug_lines = [line for line in _records(caplog, logging.DEBUG) if "model=" in line]
    assert len(debug_lines) == 1
    assert f"model={JEV_MODEL}" in debug_lines[0]
    assert _records(caplog, logging.WARNING) == []


@pytest.mark.parametrize(
    "call,payload_factory",
    [
        ("classify_intent", lambda model: intent_payload(0.97)),
        ("classify_entity", lambda model: entity_payload()),
    ],
    ids=["jev-1", "jev-2"],
)
def test_variante_d_mismatch_warns_once(
    call: str, payload_factory: Any, caplog: Any
) -> None:
    """Beide D-Wege: abweichende ID ⇒ **eine** WARNING, auch beim Wiederholen."""
    client, _ = jev_client(json_responder(_payload_model(payload_factory(None), "jev-1.14")))

    async def scenario() -> Any:
        for _ in range(2):
            if call == "classify_intent":
                await client.classify_intent("Licht an")
            else:
                await client.classify_entity("Licht an", {"light.x": "X"})

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        asyncio.run(scenario())

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert JEV_MODEL in warnings[0]
    assert "jev-1.14" in warnings[0]
    assert TOKEN not in warnings[0]
    assert any("model=jev-1.14" in line for line in _records(caplog, logging.DEBUG))


def test_classify_entity_mismatch_warns_once(caplog: Any) -> None:
    client, _ = jev_client(
        json_responder(_payload_model(entity_payload(), "jev-1.14"))
    )

    async def scenario() -> Any:
        await client.classify_entity("Licht an", {"light.x": "X"})
        return await client.classify_entity("Licht an", {"light.x": "X"})

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert JEV_MODEL in warnings[0] and "jev-1.14" in warnings[0]
    assert any(
        "model=jev-1.14" in line for line in _records(caplog, logging.DEBUG)
    )
    # Das Ergebnis bleibt intakt – reine Diagnose.
    assert result.target.value == "light.wohnzimmer"
    assert result.service.value == "turn_on"


@pytest.mark.parametrize(
    "missing", [_NO_MODEL, None, "", "  "], ids=["key-fehlt", "json-null", "leer", "ws"]
)
def test_variante_d_missing_model_warns_once(missing: Any, caplog: Any) -> None:
    """Fehlende ID auf beiden D-Wegen ⇒ `model=?` + **eine** WARNING, kein Crash."""
    intent = _payload_model(intent_payload(0.97), missing)
    client, _ = jev_client(json_responder(intent))

    async def scenario() -> Any:
        first = await client.classify_intent("Licht an")
        second = await client.classify_intent("Licht an")
        return first, second

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        first, second = asyncio.run(scenario())

    assert first.intent.score == 0.97 and second.intent.score == 0.97
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert JEV_MODEL in warnings[0]
    assert any("model=?" in line for line in _records(caplog, logging.DEBUG))


def test_entity_missing_model_warns_once(caplog: Any) -> None:
    client, _ = jev_client(
        json_responder(_payload_model(entity_payload(), _NO_MODEL))
    )

    async def scenario() -> Any:
        await client.classify_entity("Licht an", {"light.x": "X"})
        return await client.classify_entity("Licht an", {"light.x": "X"})

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    assert len(_records(caplog, logging.WARNING)) == 1
    assert any("model=?" in line for line in _records(caplog, logging.DEBUG))
    assert result.service.value == "turn_on"


def test_model_mismatch_is_exact_not_case_folded(caplog: Any) -> None:
    """`jev-1.13` ≠ `jev-1.13 ` – Whitespace wird **nicht** still entfernt."""
    client, _ = jev_client(
        json_responder(_payload_model(intent_payload(0.97), "jev-1.13 "))
    )

    async def scenario() -> Any:
        return await client.classify_intent("Licht an")

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        asyncio.run(scenario())

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "jev-1.13 " in warnings[0]
    assert any("model=jev-1.13 " in line for line in _records(caplog, logging.DEBUG))
