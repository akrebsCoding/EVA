"""LLM-Client-Tests (P4.T3, `PLAN.md` §7 → P4.T3, Layer **L0/`unit`**).

Prüfling ist `app/llm_client.py` (P4.T2).  Getestet wird gegen den
**verifizierten Vertrag** aus `STATE.md` §3 („LLM-Client (P4.T2)") und
`PLAN.md:458`, nicht gegen eine Wunschfassung.  Der Prüfling bleibt
**unangetastet** (kein Patch, kein Bug-Workaround).

Abdeckung der fünf Pflichtpunkte aus `PLAN.md:458`:

1. **SystemOne-Request-Form** — `POST {base}/systemone` mit `state` +
   `questions` (`noul`/`choice`), Bearer-Auth-Header, Timeout →
   `test_classify_posts_expected_systemone_request`, `test_classify_*_model_*`,
   `test_*timeout*`
2. **Antwort-Parsing (Wert + Score)** — `intent noul=0.97` (value `True`),
   `target choice=light` (Konfidenz `1.0`) →
   `test_parse_systemone_literal_sample`, `test_noul_value_*`
3. **Chat-Completion-Parsing** — Antwort + JSON-Modus →
   `test_parse_chat_completion_literal`,
   `test_complete_prompt_json_mode_request_body`, `test_complete_json_*`
4. **Timeout-/Fehlerpfade** — 500/`ReadTimeout` ⇒ `LlmUnavailableError`,
   ungültiges JSON ⇒ `LlmProtocolError`, fehlender Key ⇒ `LlmConfigError` →
   `test_classify_http_500_*`, `test_*read_timeout_*`, `test_*invalid_json_*`,
   `test_missing_api_key_*`
5. **`JEV_MODE`-Varianten** — `intent` (2 Fragen), `gate` (nur `intent`),
   `off` ⇒ `JevDisabledError` **ohne HTTP-Aufruf** (Zähler 0) →
   `test_build_systemone_questions_*`, `test_classify_gate_*`,
   `test_classify_off_*`

**Kein echtes Netz.**  Das Gegenüber ist ein `httpx.MockTransport` —
**in-process**, kein Socket, kein `connect`/`getaddrinfo`.  Die injizierten
`httpx.AsyncClient` tragen eine bewusst unresolvable Mock-URL
(`http://mock-llm.invalid`); die aktive Netzsperre aus `tests/conftest.py`
(E36) würde jeden echten Socket-Kontakt als `NetworkAccessBlocked` hochbluten
lassen — die Tests belegen damit positiv, dass keiner stattfindet.

**Deterministisch:** kein `sleep`, keine echte Zeitabhängigkeit, keine
Zufallswerte; alle Handler antworten synchron und reproduzierbar.

**Prüfwerte sind Literale** (E56/E58), nicht aus dem Prüfling zurückgelesen —
genau die tautologische Assertion hatte in P4.T1 eine Mutation wirkungslos
gemacht.

**Zusätzlich E91 (Modellidentität):** Die **serverseitig gemeldete** Modell-ID
(`payload["model"]`) muss in der Debug-Zeile jedes Jev-Aufrufs stehen, und
eine Abweichung von der **angefragten** ID (auch „fehlend", auch abweichende
Schreibweise) erzeugt **genau eine** ``WARNING`` je Wert – **ohne** zweites
Log-Drama im Mic-Pfad (es ist keine neue Zeile, sondern eine Ergänzung).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import httpx
import pytest

from app.llm_client import (
    ChoiceResult,
    DeepSeekClient,
    JevClient,
    JevDisabledError,
    LlmClientError,
    LlmConfigError,
    LlmProtocolError,
    LlmUnavailableError,
    NoulResult,
    build_systemone_questions,
    build_systemone_request,
    parse_chat_completion,
    parse_systemone,
)
from tests.conftest import NetworkAccessBlocked

pytestmark = pytest.mark.unit

#: Test-Token — bewusst **kein** Geheimnis, nur ein Marker im Auth-Header.
TOKEN = "test-token"
#: Reservierte, **niemals auflösbare** Mock-Basis (`RFC 2606`).  Sie steht nur
#: am Mock-Transport; es wird kein Paket gesendet.
MOCK_BASE_URL = "http://mock-llm.invalid"
#: Wörtliche Pfade (PLAN:457/§1.1) — bewusst als Literal.
SYSTEMONE_PATH = "/systemone"
CHAT_PATH = "/chat/completions"
#: Wörtlicher Jev-/DeepSeek-Default (PLAN §4, STATE §3).
JEV_MODEL = "jev-1.13"
DEEPSEEK_MODEL = "deepseek-v4.1-flash"
#: Wörtliche Jev-Timeouts aus PLAN §4 (Default 8,0 s; hier eigener Wert).
DEFAULT_JEV_TIMEOUT = 8.0

#: Fragen-Vorgaben wörtlich `PLAN.md` §1.1 (STATE §3) — **unabhängig**
#: konstruiert, nicht aus `app/llm_client.py` gelesen.
EXPECTED_CRITERIA = {
    "light": "Licht",
    "switch": "Schalter",
    "climate": "Heizung/Klima",
    "media_player": "Musik",
    "cover": "Rollo/Jalousie",
    "scene": "Szene",
    "script": "Skript",
    "other": "Sonstiges",
}
EXPECTED_INTENT_QUESTION = {
    "type": "noul",
    "instructions": "Will der Nutzer ein Gerät steuern?",
}
EXPECTED_TARGET_QUESTION = {
    "type": "choice",
    "instructions": "Welche Geräteklasse?",
    "criteria": EXPECTED_CRITERIA,
}

Responder = Callable[[httpx.Request], httpx.Response]


# ── Testdaten (verifizierte Formen, P0.T2/STATE §3) ──────────────────────
def systemone_payload(
    noul: float = 0.97,
    choice: str = "light",
    confidence: float = 1.0,
) -> dict[str, Any]:
    """`systemone`-Antwort **wörtlich** der P0.T2-Form (Wert + Score)."""
    return {
        "model": JEV_MODEL,
        "answers": {
            "intent": {"type": "noul", "noul": noul},
            "target": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {"light": 0.99, "switch": 0.01},
            },
        },
        "usage": {"prompt_tokens": 444, "completion_tokens": 91},
    }


def chat_payload(
    content: str = '{"antwort":"ok","grad":2}',
    *,
    finish_reason: str = "stop",
    reasoning: str = "kurz",
) -> dict[str, Any]:
    """OpenAI-kompatible Chat-Antwort (`choices[0].message.content`)."""
    return {
        "model": DEEPSEEK_MODEL,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                },
            }
        ],
        "usage": {"prompt_tokens": 118, "completion_tokens": 163, "total_tokens": 281},
    }


# ── Helfer (in-process, kein Socket) ─────────────────────────────────────
def make_transport(responder: Responder) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """`MockTransport`-Client + Request-Mitschnitt (in-process, kein Socket)."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return responder(request)

    inner = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=MOCK_BASE_URL
    )
    return inner, recorded


def json_responder(payload: Any, *, status: int = 200) -> Responder:
    return lambda request: httpx.Response(status, json=payload)


def jev_client(
    responder: Responder, **kwargs: Any
) -> tuple[JevClient, list[httpx.Request]]:
    inner, recorded = make_transport(responder)
    client = JevClient(
        client=inner,
        api_key=TOKEN,
        base_url=MOCK_BASE_URL,
        **kwargs,
    )
    return client, recorded


def deepseek_client(
    responder: Responder, **kwargs: Any
) -> tuple[DeepSeekClient, list[httpx.Request]]:
    inner, recorded = make_transport(responder)
    client = DeepSeekClient(
        client=inner,
        api_key=TOKEN,
        base_url=MOCK_BASE_URL,
        **kwargs,
    )
    return client, recorded


# ── 1. SystemOne-Request-Form ────────────────────────────────────────────
def test_classify_posts_expected_systemone_request() -> None:
    client, recorded = jev_client(json_responder(systemone_payload()))
    state = "Schalte das Licht im Wohnzimmer ein\n\nEntities: light.wohnzimmer"

    async def scenario() -> None:
        await client.start()
        try:
            await client.classify(state)
        finally:
            await client.stop()

    asyncio.run(scenario())

    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert request.url.path == SYSTEMONE_PATH
    assert request.url.host == "mock-llm.invalid"
    assert str(request.url) == MOCK_BASE_URL + SYSTEMONE_PATH
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "model": JEV_MODEL,
        "state": state,
        "questions": {
            "intent": EXPECTED_INTENT_QUESTION,
            "target": EXPECTED_TARGET_QUESTION,
        },
    }


def test_classify_model_override_is_sent() -> None:
    client, recorded = jev_client(json_responder(systemone_payload()))

    async def scenario() -> None:
        await client.classify("Licht an", model="jev-1.13-free")

    asyncio.run(scenario())
    assert json.loads(recorded[0].content)["model"] == "jev-1.13-free"


def test_classify_custom_timeout_is_used_for_error_detail() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu spät", request=request)

    client, _ = jev_client(responder, timeout=2.5)

    async def scenario() -> None:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.classify("Licht an")
        assert "2.5" in (excinfo.value.detail or "")

    asyncio.run(scenario())


def test_default_timeout_is_applied_to_built_http_client() -> None:
    # Kein injizierter Client ⇒ `_build_client()` baut einen echten
    # `httpx.AsyncClient` mit dem konfigurierten Timeout.  Es wird **kein**
    # Request gesendet, also kein Netz.
    client = JevClient(api_key=TOKEN)

    async def scenario() -> None:
        await client.start()
        try:
            assert client.timeout == DEFAULT_JEV_TIMEOUT
            assert client.client is not None
            assert client.client.timeout.read == DEFAULT_JEV_TIMEOUT
            assert client.client.timeout.connect == DEFAULT_JEV_TIMEOUT
        finally:
            await client.stop()

    asyncio.run(scenario())


# ── 2. Antwort-Parsing (Wert + Score) ────────────────────────────────────
def test_parse_systemone_literal_sample() -> None:
    result = parse_systemone(systemone_payload(), mode="intent")

    assert result.intent.score == 0.97
    assert result.intent.value is True
    assert result.target is not None
    assert result.target.value == "light"
    assert result.target.score == 1.0
    assert result.target.probabilities == {"light": 0.99, "switch": 0.01}
    assert result.model == JEV_MODEL
    assert result.usage == {"prompt_tokens": 444, "completion_tokens": 91}


def test_classify_parses_value_and_score_end_to_end() -> None:
    client, recorded = jev_client(json_responder(systemone_payload()))

    async def scenario() -> Any:
        return await client.classify("Licht an")

    result = asyncio.run(scenario())
    assert len(recorded) == 1
    assert result.intent.score == 0.97
    assert result.intent.value is True
    assert result.target is not None
    assert result.target.value == "light"
    assert result.target.score == 1.0


@pytest.mark.parametrize(
    "score,expected",
    [(0.97, True), (0.50, True), (0.49, False), (0.0, False)],
)
def test_noul_value_threshold(score: float, expected: bool) -> None:
    assert NoulResult(score=score).value is expected


def test_noul_at_least_uses_raw_score() -> None:
    result = NoulResult(score=0.74)
    assert result.at_least(0.75) is False
    assert result.at_least(0.74) is True
    assert ChoiceResult(value="light", score=0.4).score == 0.4


def test_parse_systemone_gate_ignores_target() -> None:
    # Bei `gate` fragt der Client die `choice`-Frage gar nicht (E58); ein
    # trotzdem geliefertes `target` wird **nicht** gelesen.
    result = parse_systemone(systemone_payload(), mode="gate")
    assert result.intent.score == 0.97
    assert result.target is None


def test_parse_systemone_rejects_missing_intent() -> None:
    with pytest.raises(LlmProtocolError):
        parse_systemone({"answers": {}}, mode="intent")
    with pytest.raises(LlmProtocolError):
        parse_systemone({"answers": {"intent": {}}}, mode="intent")
    with pytest.raises(LlmProtocolError):
        parse_systemone({"model": "x"}, mode="intent")


def test_parse_systemone_rejects_bool_score() -> None:
    payload = {"answers": {"intent": {"type": "noul", "noul": True}}}
    with pytest.raises(LlmProtocolError):
        parse_systemone(payload, mode="intent")


def test_parse_systemone_intent_requires_target() -> None:
    payload = {"answers": {"intent": {"type": "noul", "noul": 0.97}}}
    with pytest.raises(LlmProtocolError):
        parse_systemone(payload, mode="intent")
    # Im `gate`-Modus ist dieselbe Antwort gültig.
    assert parse_systemone(payload, mode="gate").target is None


def test_parse_systemone_rejects_non_mapping() -> None:
    with pytest.raises(LlmProtocolError):
        parse_systemone([1, 2, 3])  # type: ignore[arg-type]


# ── 3. Chat-Completion-Parsing (Antwort + JSON-Modus) ────────────────────
def test_parse_chat_completion_literal() -> None:
    result = parse_chat_completion(chat_payload('{"antwort":"ok","grad":2}'))
    assert result.content == '{"antwort":"ok","grad":2}'
    assert result.model == DEEPSEEK_MODEL
    assert result.finish_reason == "stop"
    assert result.reasoning_content == "kurz"
    assert result.usage == {
        "prompt_tokens": 118,
        "completion_tokens": 163,
        "total_tokens": 281,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": ["kein Objekt"]},
    ],
)
def test_parse_chat_completion_rejects_malformed(payload: Any) -> None:
    with pytest.raises(LlmProtocolError):
        parse_chat_completion(payload)


def test_complete_prompt_json_mode_request_body() -> None:
    client, recorded = deepseek_client(json_responder(chat_payload()))
    system_prompt = "Antworte nur mit JSON."

    async def scenario() -> Any:
        await client.start()
        try:
            return await client.complete_prompt(
                "Frage",
                system_prompt=system_prompt,
                json_mode=True,
            )
        finally:
            await client.stop()

    result = asyncio.run(scenario())

    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert request.url.path == CHAT_PATH
    assert str(request.url) == MOCK_BASE_URL + CHAT_PATH
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body == {
        "model": DEEPSEEK_MODEL,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Frage"},
        ],
        "response_format": {"type": "json_object"},
    }
    assert result.content == '{"antwort":"ok","grad":2}'


def test_complete_without_json_mode_omits_response_format() -> None:
    client, recorded = deepseek_client(json_responder(chat_payload()))

    async def scenario() -> None:
        await client.complete_prompt("Frage", system_prompt="S")

    asyncio.run(scenario())
    body = json.loads(recorded[0].content)
    assert body["temperature"] == 0.0
    assert "response_format" not in body


def test_complete_json_parses_content_and_sends_json_mode() -> None:
    client, recorded = deepseek_client(
        json_responder(chat_payload('{"antwort":"ok","grad":2}'))
    )

    async def scenario() -> Any:
        return await client.complete_json("Frage", system_prompt="S")

    parsed = asyncio.run(scenario())
    assert parsed == {"antwort": "ok", "grad": 2}
    assert json.loads(recorded[0].content)["response_format"] == {
        "type": "json_object"
    }


def test_complete_json_invalid_content_raises_protocol() -> None:
    client, _ = deepseek_client(json_responder(chat_payload("kein json")))

    async def scenario() -> None:
        with pytest.raises(LlmProtocolError):
            await client.complete_json("Frage", system_prompt="S")

    asyncio.run(scenario())


# ── 4. Timeout-/Fehlerpfade ──────────────────────────────────────────────
def test_classify_http_500_raises_unavailable() -> None:
    client, recorded = jev_client(lambda r: httpx.Response(500, text="boom"))

    async def scenario() -> None:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.classify("Licht an")
        assert isinstance(excinfo.value, LlmClientError)
        assert "500" in (excinfo.value.detail or "")
        # Secret-Hygiene: der Token darf nie in der Exception landen.
        assert TOKEN not in str(excinfo.value)
        assert TOKEN not in (excinfo.value.detail or "")

    asyncio.run(scenario())
    assert len(recorded) == 1


def test_classify_read_timeout_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu spät", request=request)

    client, _ = jev_client(responder)

    async def scenario() -> None:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.classify("Licht an")
        assert "Timeout" in (excinfo.value.detail or "")
        assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)

    asyncio.run(scenario())


def test_complete_read_timeout_raises_unavailable() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu spät", request=request)

    client, _ = deepseek_client(responder)

    async def scenario() -> None:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.complete_prompt("Frage", system_prompt="S")
        assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)

    asyncio.run(scenario())


def test_classify_invalid_json_raises_protocol() -> None:
    client, _ = jev_client(lambda r: httpx.Response(200, content=b"nicht json"))

    async def scenario() -> None:
        with pytest.raises(LlmProtocolError):
            await client.classify("Licht an")

    asyncio.run(scenario())


def test_classify_non_object_json_raises_protocol() -> None:
    client, _ = jev_client(json_responder([1, 2, 3]))

    async def scenario() -> None:
        with pytest.raises(LlmProtocolError):
            await client.classify("Licht an")

    asyncio.run(scenario())


def test_missing_api_key_raises_config_without_http() -> None:
    inner, recorded = make_transport(json_responder(systemone_payload()))
    jev = JevClient(client=inner, api_key="", base_url=MOCK_BASE_URL)
    deep = DeepSeekClient(client=inner, api_key="", base_url=MOCK_BASE_URL)

    async def scenario() -> None:
        with pytest.raises(LlmConfigError):
            await jev.classify("Licht an")
        with pytest.raises(LlmConfigError):
            await deep.complete_prompt("Frage", system_prompt="S")

    asyncio.run(scenario())
    assert recorded == []  # kein HTTP-Aufruf ohne Key


def test_classify_without_start_raises_unavailable() -> None:
    client = JevClient(api_key=TOKEN)

    async def scenario() -> None:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.classify("Licht an")
        assert "nicht gestartet" in (excinfo.value.detail or "")

    asyncio.run(scenario())


# ── 5. JEV_MODE-Varianten ────────────────────────────────────────────────
def test_build_systemone_questions_intent_has_two_questions() -> None:
    questions = build_systemone_questions("intent")
    assert set(questions) == {"intent", "target"}
    assert questions["intent"] == EXPECTED_INTENT_QUESTION
    assert questions["target"] == EXPECTED_TARGET_QUESTION


def test_build_systemone_questions_gate_has_only_intent() -> None:
    questions = build_systemone_questions("gate")
    assert set(questions) == {"intent"}
    assert questions["intent"] == EXPECTED_INTENT_QUESTION


def test_build_systemone_questions_off_is_empty() -> None:
    assert build_systemone_questions("off") == {}


def test_invalid_mode_raises_config() -> None:
    with pytest.raises(LlmConfigError):
        build_systemone_questions("gibtsnicht")
    with pytest.raises(LlmConfigError):
        build_systemone_request("Licht an", mode="gibtsnicht")
    with pytest.raises(LlmConfigError):
        JevClient(mode="gibtsnicht")


def test_classify_gate_sends_only_intent_and_returns_no_target() -> None:
    client, recorded = jev_client(json_responder(systemone_payload()), mode="gate")

    async def scenario() -> Any:
        await client.start()
        try:
            return await client.classify("Licht an")
        finally:
            await client.stop()

    result = asyncio.run(scenario())

    assert len(recorded) == 1
    body = json.loads(recorded[0].content)
    assert set(body["questions"]) == {"intent"}
    assert body["questions"]["intent"] == EXPECTED_INTENT_QUESTION
    assert result.intent.score == 0.97
    assert result.target is None


def test_classify_off_raises_disabled_without_http() -> None:
    client, recorded = jev_client(json_responder(systemone_payload()), mode="off")
    assert client.enabled is False

    async def scenario() -> None:
        with pytest.raises(JevDisabledError):
            await client.classify("Licht an")

    asyncio.run(scenario())
    assert recorded == []  # Mock-Zähler 0: kein HTTP-Aufruf
    assert client.enabled is False


def test_enabled_for_intent_and_gate() -> None:
    assert JevClient(api_key=TOKEN, mode="intent").enabled is True
    assert JevClient(api_key=TOKEN, mode="gate").enabled is True


# ── E91: Modellidentität lokal beweisbar (`classify`, Variante A) ──────────
#: Gemeldete IDs, die **nicht** „fehlend" sind – nur die zählen als Befund.
_NO_MODEL: Any = object()


def _payload_reported_model(model: Any) -> dict[str, Any]:
    """`systemone`-Antwort mit wählbarer Modell-ID (E91).

    ``_NO_MODEL`` ⇒ ``"model"``-**Schlüssel fehlt**; ``None`` ⇒ JSON-``null``;
    ``""``/``"   "`` ⇒ leer/reine Whitespace.  Alle vier Formen müssen als
    „keine Modell-ID" gelten (DEBUG ``model=?`` + **eine** WARNING).
    """
    payload = systemone_payload()
    if model is _NO_MODEL:
        payload.pop("model", None)
    else:
        payload["model"] = model
    return payload


def _llm_warnings(caplog: Any) -> list[str]:
    """Nur die WARNINGs **dieses** Loggers (fremde Logger stören nicht)."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "manager.llm_client"
    ]


def _llm_debugs(caplog: Any) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG and record.name == "manager.llm_client"
    ]


def test_classify_debug_line_carries_reported_model(caplog: Any) -> None:
    """Passende Modell-ID ⇒ `model=` **in** der Debug-Zeile, **keine** WARNING."""
    client, _ = jev_client(json_responder(_payload_reported_model(JEV_MODEL)))

    async def scenario() -> Any:
        return await client.classify("Licht an")

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    assert result.model == JEV_MODEL
    debug_lines = [line for line in _llm_debugs(caplog) if "model=" in line]
    assert len(debug_lines) == 1
    assert f"model={JEV_MODEL}" in debug_lines[0]
    assert _llm_warnings(caplog) == []


def test_classify_model_mismatch_warns_once(caplog: Any) -> None:
    """Abweichende ID ⇒ **genau eine** WARNING (beide IDs genannt), kein Flooding."""
    client, recorded = jev_client(json_responder(_payload_reported_model("jev-1.99")))

    async def scenario() -> Any:
        await client.classify("Licht an")
        await client.classify("Licht an")  # gleiche Abweichung: kein 2. Mal

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        asyncio.run(scenario())

    warnings = _llm_warnings(caplog)
    assert len(warnings) == 1
    assert JEV_MODEL in warnings[0]  # angefragt
    assert "jev-1.99" in warnings[0]  # gemeldet
    assert TOKEN not in warnings[0]  # kein Secret in der Message
    assert len(recorded) == 2  # beide Aufrufe liefen normal
    # Die Debug-Zeile nennt trotzdem die *tatsächlich gemeldete* ID.
    assert any(
        "model=jev-1.99" in line for line in _llm_debugs(caplog)
    ), _llm_debugs(caplog)


@pytest.mark.parametrize(
    "reported",
    ["JEV-1.13", "jev-1.13 ", " jev-1.13", "jev-1.13\n"],
    ids=["case", "trailing-space", "leading-space", "newline"],
)
def test_classify_compares_model_exactly(reported: str, caplog: Any) -> None:
    """Kein stilles Case-Folding, kein Strippen: jede Abweichung warnt (E91)."""
    client, _ = jev_client(json_responder(_payload_reported_model(reported)))

    async def scenario() -> Any:
        return await client.classify("Licht an")

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        asyncio.run(scenario())

    warnings = _llm_warnings(caplog)
    assert len(warnings) == 1
    assert reported in warnings[0]
    assert any(f"model={reported}" in line for line in _llm_debugs(caplog))


@pytest.mark.parametrize(
    "missing", [_NO_MODEL, None, "", "   "], ids=["key-fehlt", "json-null", "leer", "ws"]
)
def test_classify_missing_model_warns_once_and_keeps_going(
    missing: Any, caplog: Any
) -> None:
    """Fehlende/``null``/leere ID ⇒ `model=?` + **eine** WARNING, kein Crash."""
    client, _ = jev_client(json_responder(_payload_reported_model(missing)))

    async def scenario() -> Any:
        await client.classify("Licht an")
        return await client.classify("Licht an")  # Flooding-Schutz greift auch hier

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    # Das Ergebnis bleibt vollständig nutzbar – nur die Diagnose ändert sich.
    assert result.intent.score == 0.97
    assert result.target is not None and result.target.value == "light"
    warnings = _llm_warnings(caplog)
    assert len(warnings) == 1
    assert JEV_MODEL in warnings[0]
    assert any("model=?" in line for line in _llm_debugs(caplog))


def test_classify_model_none_falls_back_to_client_default(caplog: Any) -> None:
    """`classify(..., model=None)` ⇒ **kein** Fehler, der Client-Default gilt."""
    client, recorded = jev_client(json_responder(_payload_reported_model(JEV_MODEL)))

    async def scenario() -> Any:
        return await client.classify("Licht an", model=None)

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        result = asyncio.run(scenario())

    assert json.loads(recorded[0].content)["model"] == JEV_MODEL
    assert result.model == JEV_MODEL
    assert _llm_warnings(caplog) == []


def test_classify_model_override_mismatch_names_the_requested_id(caplog: Any) -> None:
    """Bei `model=`-Override warnt die Message gegen die **angefragte** ID."""
    client, recorded = jev_client(
        json_responder(_payload_reported_model(JEV_MODEL))
    )

    async def scenario() -> Any:
        return await client.classify("Licht an", model="jev-1.13-free")

    with caplog.at_level(logging.DEBUG, logger="manager.llm_client"):
        asyncio.run(scenario())

    assert json.loads(recorded[0].content)["model"] == "jev-1.13-free"
    warnings = _llm_warnings(caplog)
    assert len(warnings) == 1
    assert "jev-1.13-free" in warnings[0]  # angefragt
    assert JEV_MODEL in warnings[0]  # gemeldet


# ── Netz-Nachweis ────────────────────────────────────────────────────────
def test_mocktransport_is_in_process_while_netz_block_is_active() -> None:
    """Die Netzsperre ist aktiv; `MockTransport` umgeht sie **legitim**.

    Ein echter Socket-Kontakt (DNS) blutet als `NetworkAccessBlocked` hoch —
    genau das würde passieren, wenn ein Test das echte Netz träfe.  Dieselbe
    Mock-URL über `MockTransport` antwortet dagegen **in-process** (kein
    Socket), der Request ist aufgezeichnet.
    """
    with pytest.raises(NetworkAccessBlocked):
        import socket

        socket.getaddrinfo("mock-llm.invalid", 80)

    inner, recorded = make_transport(json_responder(systemone_payload()))

    async def scenario() -> None:
        response = await inner.post(MOCK_BASE_URL + SYSTEMONE_PATH, json={})
        assert response.status_code == 200
        await inner.aclose()

    asyncio.run(scenario())
    assert len(recorded) == 1
    assert recorded[0].url.host == "mock-llm.invalid"
    assert isinstance(inner._transport, httpx.MockTransport)
