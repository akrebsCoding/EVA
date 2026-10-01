"""Selbsttest des L2-Harness `manager_proc` (P9.T0, `PLAN.md:538`, §7.1 L2).

Der Harness startet den Manager als **echten uvicorn-Subprozess**.  Diese
Datei belegt genau die vier zugesagten Eigenschaften des Fixtures:

1. der Subprozess **startet** und ist erreichbar (`/health` **HTTP 200**),
2. die **Log-Datei** wird gefüllt (Capture),
3. die `/internal/*`-Test-Hooks sind bei `ENABLE_TEST_HOOKS=true` erreichbar,
4. der **harte Teardown** beendet und reapt den Prozess (kein Zombie) und der
   Port ist danach wieder frei.

Marker: `integration` (L2) — nur dieser Marker hebt die E36-Netzsperre für den
Test-Prozess gezielt auf; er ist für echten localhost-Verkehr zwingend.
"""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest

from tests.conftest import (
    start_manager_process,
    stop_manager_process,
)

pytestmark = pytest.mark.integration


def test_manager_proc_starts_health_200_and_fills_log(manager_proc) -> None:
    """Subprozess lebt, `/health` liefert 200, Log-Datei ist gefüllt."""
    assert manager_proc.is_alive(), "Manager-Subprozess ist nicht gestartet"

    with urllib.request.urlopen(
        f"{manager_proc.base_url}/health", timeout=2.0
    ) as response:
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))

    assert payload["status"] == "ok"
    assert payload["service"] == "wyoming-manager"
    assert payload["mdns"] is False, "mDNS muss im Testlauf aus sein (.env.test)"
    assert payload["devices"] == 0

    text = manager_proc.log_text()
    assert text, f"Log-Datei ist leer: {manager_proc.log_path}"
    assert "Uvicorn running on" in text
    assert f":{manager_proc.port}" in text
    assert manager_proc.log_path.stat().st_size > 0


def test_manager_proc_exposes_internal_test_hooks(manager_proc) -> None:
    """`ENABLE_TEST_HOOKS=true` ⇒ `/internal/status` ist geroutet (HTTP 200)."""
    with urllib.request.urlopen(
        f"{manager_proc.base_url}/internal/status", timeout=2.0
    ) as response:
        assert response.status == 200
        body = json.loads(response.read().decode("utf-8"))
    assert body["status"] == "ok"
    assert body["sessions"] == []
    assert body["states"] == {}


def test_manager_proc_teardown_reaps_and_frees_port() -> None:
    """Eigener, kurzlebiger Manager: Teardown reapt den Prozess, Port frei.

    Bewusst ein **zweiter** Prozess (eigener Port), damit der session-weite
    `manager_proc` unberührt bleibt und der Teardown isoliert geprüft wird.
    """
    port = 18769
    mp = start_manager_process(port=port)
    try:
        assert mp.is_alive()
    finally:
        returncode = stop_manager_process(mp)

    # `wait()` ist gelaufen ⇒ `returncode` gesetzt und `poll()` != None:
    # der Prozess wurde eingesammelt, es bleibt kein Zombie zurück.
    assert returncode is not None
    assert mp.process.poll() is not None
    assert returncode == 0, "SIGTERM muss uvicorn sauber mit rc=0 beenden (P5.T4)"

    # Kein Listener überlebt: ein Verbindungsversuch muss abgelehnt werden.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", port))


# ═══════════════════════════════════════════════════════════════════════════
# P8.D3 – die drei Live-Befunde, am **echten** Prozess geprüft
# ═══════════════════════════════════════════════════════════════════════════
# Dieser Abschnitt ist die einzige Stelle, die den **echten `lifespan`** aus
# `app/main.py` ausführt (echter uvicorn-Subprozess, echtes HTTP). Genau dort
# entstehen die drei live gemessenen Symptome auf `.123`:
#
#   Bug 1  `/api/logs`   ⇒ `source:"none"`, 0 Einträge, `degraded:true`
#   Bug 2  `/api/config` ⇒ `degraded:true` + „app.state.settings fehlt"
#   Bug 3  `/api/history` ⇒ `available:false`
#
# Ein Attrappen-Build (`ASGITransport`) würde den Lifespan **nicht** ausführen
# und könnte keinen dieser drei Fehler überhaupt reproduzieren.


def get_json(mp: Any, path: str) -> Any:
    """`GET <base_url><path>` als JSON (L2, echtes HTTP auf localhost)."""
    with urllib.request.urlopen(f"{mp.base_url}{path}", timeout=2.0) as response:
        assert response.status == 200, path
        return json.loads(response.read().decode("utf-8"))


def test_dashboard_apis_ohne_degraded_im_echten_prozess(manager_proc) -> None:
    """Alle drei Endpunkte antworten nach dem P8.D3-Fix ohne Ausfallhinweis."""
    assert manager_proc.is_alive()

    # Bug 1: der Lifespan hängt den Puffer an ⇒ Quelle `memory`, nicht `none`.
    logs = get_json(manager_proc, "/api/logs?limit=20")
    assert logs["source"] == "memory", "Lifespan hat den Logpuffer nicht angehängt"
    assert logs["degraded"] is False, logs["issues"]
    assert logs["entries"], "Logpuffer ist angehängt, aber leer (unmöglich)"
    # Die Startzeile des Lifespans ist genau der Beweis dafür.
    assert any("wyoming-manager startet" in entry["message"] for entry in logs["entries"])

    # Bug 2: `app.state.settings` ist gesetzt ⇒ `app.state`, kein `degraded`.
    config = get_json(manager_proc, "/api/config")
    assert config["settings_source"] == "app.state"
    assert config["degraded"] is False, config["issues"]
    assert not [issue for issue in config["issues"] if "app.state.settings" in issue]
    # Kein Secret im Antworttext (unveränderte Allowlist).
    assert "ha_token" not in json.dumps(config) and "llm_api_key" not in json.dumps(config)

    # Bug 3: der Pipeline-Puffer existiert ⇒ `available`, auch wenn noch kein
    # Turn gelaufen ist.
    history = get_json(manager_proc, "/api/history")
    assert history["available"] is True
    assert history["reason"] is None
    assert history["degraded"] is False
