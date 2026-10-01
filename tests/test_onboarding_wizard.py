"""Tests des CLI-Onboarding-Wizards (P10.T2, Layer **L0** – rein, kein Netz).

Geprüft wird:

* **Answers-Parsing/Validierung** – Pflichtfelder, Topologie-Wahl,
  `WIZARD_ENV_MODE`, Fehler mit klarer Meldung statt Traceback.
* **Validierungs-Logik mit gefakten HTTP-Antworten** – HA 200/401/500/
  unerreichbar getrennt (Auftrags-Muster: „erreichbar, Token ok“ vs.
  „erreichbar, Token falsch“ vs. „nicht erreichbar“), LLM-Format-Check
  (erwartet ~51 Zeichen) + nicht blockierende Reachability.
* **Topologie-Ableitung** – single ⇒ `127.0.0.1`, distributed ⇒ Remote-Host;
  Render: manager-only (ohne STT/TTS) vs. Remote-Set (Port-Publish).
* **RAM-Warnung** – unter/über der ~1,5-GB-Schwelle (gemessene Werte).
* **0600-Perms** – `.env` auch bei vor lockerer Rechte existierender Datei.
* **CLI** – Non-Interactive ohne Answers-File ⇒ Usage-Fehler (rc 2);
  Ctrl-C ⇒ 130 ohne Traceback; `--no-up` rendert ohne Docker-Aufruf.

Alle HTTP-/Docker-Aufrufe sind injiziert (`http_status`/`run_compose`
monkeypatcht) — L0 hat kein Netz (`conftest`).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

WIZARD_PATH = Path(__file__).resolve().parent.parent / "deploy" / "onboarding" / "wizard.py"


def load_wizard() -> ModuleType:
    """Lädt den Wizard als Modul (kein Package — `deploy/` ist keins)."""
    spec = importlib.util.spec_from_file_location("onboarding_wizard_under_test", WIZARD_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses löst Klassen beim exec über sys.modules[cls.__module__] auf —
    # ohne Registrierung stirbt der Import mit AttributeError (Python ≥3.12).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def wizard() -> ModuleType:
    return load_wizard()


FULL_ANSWERS = (
    "TOPOLOGY=single\n"
    "HA_BASE_URL=http://10.0.0.10:8123\n"
    "HA_TOKEN=FAKE-HA-TOKEN\n"
    "LLM_API_KEY=FAKE-LLM-KEY-0123456789abcdef\n"
)


# ── Answers-Parsing / -Validierung ────────────────────────────────────────

def test_parse_answers_reads_env_format(wizard: ModuleType) -> None:
    answers = wizard.parse_answers(
        "# Kommentar\n\nTOPOLOGY=single\nHA_BASE_URL=http://h:8123\n"
        "HA_TOKEN=T\nLLM_API_KEY=K\nMANAGER_MDNS_ENABLED=false\n"
    )
    assert answers == {
        "TOPOLOGY": "single",
        "HA_BASE_URL": "http://h:8123",
        "HA_TOKEN": "T",
        "LLM_API_KEY": "K",
        "MANAGER_MDNS_ENABLED": "false",
    }


def test_parse_answers_rejects_line_without_equals(wizard: ModuleType) -> None:
    with pytest.raises(wizard.WizardError, match="kein '='"):
        wizard.parse_answers("TOPOLOGY single\n")


@pytest.mark.parametrize("content,expected", [
    ("TOPOLOGY=single\nHA_TOKEN=T\nLLM_API_KEY=K\n", "HA_BASE_URL"),
    ("TOPOLOGY=single\nHA_BASE_URL=http://h\nLLM_API_KEY=K\n", "HA_TOKEN"),
    ("TOPOLOGY=single\nHA_BASE_URL=http://h\nHA_TOKEN=T\n", "LLM_API_KEY"),
])
def test_validate_answers_flags_missing_required(wizard: ModuleType, content: str, expected: str) -> None:
    with pytest.raises(wizard.WizardError, match=expected):
        wizard.validate_answers(wizard.parse_answers(content))


def test_validate_answers_distributed_needs_remote_host(wizard: ModuleType) -> None:
    answers = wizard.validate_answers(wizard.parse_answers(FULL_ANSWERS))
    assert answers["TOPOLOGY"] == "single"
    distributed = FULL_ANSWERS.replace("TOPOLOGY=single", "TOPOLOGY=distributed")
    with pytest.raises(wizard.WizardError, match="REMOTE_HOST"):
        wizard.validate_answers(wizard.parse_answers(distributed))


def test_validate_answers_rejects_unknown_topology_and_env_mode(wizard: ModuleType) -> None:
    with pytest.raises(wizard.WizardError, match="TOPOLOGY"):
        wizard.validate_answers(wizard.parse_answers(FULL_ANSWERS.replace("TOPOLOGY=single", "TOPOLOGY=cluster")))
    with pytest.raises(wizard.WizardError, match="WIZARD_ENV_MODE"):
        wizard.validate_answers(wizard.parse_answers(FULL_ANSWERS + "WIZARD_ENV_MODE=delete\n"))


# ── HA-Validierung (gefake HTTP) ─────────────────────────────────────────

def test_validate_ha_200_means_token_ok(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    verdict, message = wizard.validate_ha("http://10.0.0.10:8123", "token")
    assert verdict == "ok"
    assert "Token ok" in message


def test_validate_ha_401_means_reachable_but_token_wrong(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Auftrags-Belegfall: erreichbar, aber Token falsch — klar getrennt."""
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (401, None))
    verdict, message = wizard.validate_ha("http://10.0.0.10:8123", "wrong")
    assert verdict == "auth"
    assert "erreichbar" in message and "Token falsch" in message and "401" in message


def test_validate_ha_403_also_counts_as_auth_error(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (403, None))
    verdict, _ = wizard.validate_ha("http://h:8123", "wrong")
    assert verdict == "auth"


def test_validate_ha_unreachable_is_separate(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (None, "connection refused"))
    verdict, message = wizard.validate_ha("http://10.0.0.1:8123", "token")
    assert verdict == "unreachable"
    assert "nicht erreichbar" in message


def test_validate_ha_5xx_reported_but_distinct(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (500, None))
    verdict, message = wizard.validate_ha("http://h:8123", "token")
    assert verdict == "http"
    assert "500" in message


def test_validate_ha_rejects_non_http_url(wizard: ModuleType) -> None:
    verdict, message = wizard.validate_ha("ftp://h:8123", "token")
    assert verdict == "unreachable"
    assert "http(s)" in message


# ── LLM-Validierung (Format + nicht blockierende Reachability) ───────────

def test_validate_llm_expected_length_is_ok(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    format_ok, verdict, message = wizard.validate_llm("K" * 51)
    assert format_ok and verdict == "ok" and "len=51" in message


def test_validate_llm_short_key_flags_format(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (None, "no network"))
    format_ok, verdict, message = wizard.validate_llm("zu-kurz")
    assert not format_ok
    assert verdict == "ok"  # Warnung, kein Blocker
    assert "kurz" in message and "nicht blockierend" in message


def test_validate_llm_unreachable_gateway_never_blocks(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (None, "timeout"))
    format_ok, verdict, message = wizard.validate_llm("K" * 51)
    assert format_ok and verdict == "ok" and "nicht blockierend" in message


# ── RAM-Warnung ───────────────────────────────────────────────────────────

def test_read_mem_available_parses_proc(tmp_path: Path, wizard: ModuleType) -> None:
    proc = tmp_path / "meminfo"
    proc.write_text("MemTotal:       4096000 kB\nMemAvailable:   1536000 kB\n")
    assert wizard.read_mem_available(str(proc)) == 1536000 * 1024


def test_read_mem_available_missing_file_is_none(tmp_path: Path, wizard: ModuleType) -> None:
    assert wizard.read_mem_available(str(tmp_path / "fehlt")) is None


def test_ram_warning_fires_below_threshold(wizard: ModuleType) -> None:
    warning = wizard.ram_warning(int(1.0 * 1024**3))
    assert warning is not None
    assert "~700 MB" in warning and "verteilt" in warning


def test_ram_warning_silent_when_enough(wizard: ModuleType) -> None:
    assert wizard.ram_warning(int(2.5 * 1024**3)) is None


def test_ram_warning_none_without_data(wizard: ModuleType) -> None:
    assert wizard.ram_warning(None) is None


# ── Topologie-Ableitung + Maskierung ─────────────────────────────────────

def test_single_host_derives_loopback(wizard: ModuleType) -> None:
    config = wizard.WizardConfig(topology="single", ha_base_url="http://h", ha_token="T", llm_api_key="K")
    assert config.whisper_host == "127.0.0.1"
    assert config.piper_host == "127.0.0.1"


def test_distributed_derives_remote_host(wizard: ModuleType) -> None:
    config = wizard.WizardConfig(
        topology="distributed", remote_host="10.0.0.11",
        ha_base_url="http://h", ha_token="T", llm_api_key="K",
    )
    assert config.whisper_host == "10.0.0.11"
    assert config.piper_host == "10.0.0.11"
    sets = config.env_sets()
    assert sets["WHISPER_HOST"] == "10.0.0.11" and sets["PIPER_HOST"] == "10.0.0.11"


def test_env_sets_carries_overrides(wizard: ModuleType) -> None:
    config = wizard.WizardConfig(
        topology="single", ha_base_url="http://h", ha_token="T", llm_api_key="K",
        overrides={"MANAGER_MDNS_ENABLED": "false"},
    )
    assert config.env_sets()["MANAGER_MDNS_ENABLED"] == "false"


def test_mask_hides_secret_values(wizard: ModuleType) -> None:
    assert wizard.mask("HA_TOKEN", "x" * 183) == "<SECRET, len=183>"
    assert wizard.mask("LLM_API_KEY", "k" * 51) == "<SECRET, len=51>"
    assert wizard.mask("HA_BASE_URL", "http://h:8123") == "http://h:8123"


# ── .env-Render + 0600 ───────────────────────────────────────────────────

def test_render_env_replaces_and_appends(wizard: ModuleType) -> None:
    rendered = wizard.render_env("A=1\nWHISPER_HOST=127.0.0.1\n# Kommentar\n", {"WHISPER_HOST": "10.0.0.9", "NEW_KEY": "v"})
    assert "WHISPER_HOST=10.0.0.9" in rendered
    assert "WHISPER_HOST=127.0.0.1" not in rendered
    assert "NEW_KEY=v" in rendered
    assert "# Kommentar" in rendered  # Template-Kommentare bleiben


def test_write_env_0600_sets_permissions(tmp_path: Path, wizard: ModuleType) -> None:
    env_path = tmp_path / ".env"
    wizard.write_env_0600(env_path, "A=1\n")
    assert os.stat(env_path).st_mode & 0o777 == 0o600


def test_write_env_0600_tightens_loose_existing_file(tmp_path: Path, wizard: ModuleType) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n")
    env_path.chmod(0o644)
    wizard.write_env_0600(env_path, "A=2\n")
    assert os.stat(env_path).st_mode & 0o777 == 0o600
    assert env_path.read_text() == "A=2\n"


# ── Compose-Render (beide Topologien, E102) ──────────────────────────────

def test_render_compose_single_keeps_all_services(wizard: ModuleType) -> None:
    rendered = wizard.render_compose_single(wizard.COMPOSE_TEMPLATE.read_text(), wizard.WIZARD_ROOT, Path("/tmp/d"))
    services = [l.strip() for l in rendered.splitlines() if re.match(r"^  \w[\w-]*:$", l)]
    assert services == ["wyoming-manager:", "wyoming-whisper:", "wyoming-piper:"]
    assert rendered.count("network_mode: host") == 3
    assert "0.0.0.0:10300" not in rendered  # kein Publish im host-Netz


def test_render_compose_resolves_all_placeholders(wizard: ModuleType) -> None:
    rendered = wizard.render_compose_single(wizard.COMPOSE_TEMPLATE.read_text(), wizard.WIZARD_ROOT, Path("/tmp/d"))
    leftovers = [l for l in rendered.splitlines() if "@" in l and not l.lstrip().startswith("#")]
    assert leftovers == []
    assert wizard.initial_prompt_text() in rendered  # 63-Begriffe-Prompt drin


def test_render_compose_manager_only_drops_stt_tts(wizard: ModuleType) -> None:
    rendered = wizard.render_compose_manager_only(wizard.COMPOSE_TEMPLATE.read_text(), wizard.WIZARD_ROOT, Path("/tmp/d"))
    services = [l.strip() for l in rendered.splitlines() if re.match(r"^  \w[\w-]*:$", l)]
    assert services == ["wyoming-manager:"]


def test_render_compose_remote_publishes_ports(wizard: ModuleType) -> None:
    rendered = wizard.render_compose_remote(wizard.COMPOSE_TEMPLATE.read_text(), "/opt/eva/data")
    services = [l.strip() for l in rendered.splitlines() if re.match(r"^  \w[\w-]*:$", l)]
    assert services == ["wyoming-whisper:", "wyoming-piper:"]
    assert '"0.0.0.0:10300:10300/tcp"' in rendered
    assert '"0.0.0.0:10200:10200/tcp"' in rendered
    # kein host-Netz im Remote-Set (Bridge + Publish, Live-.106-Muster)
    assert "network_mode: host" not in rendered
    assert "/opt/eva/data/whisper:/data" in rendered


# ── CLI / main() ─────────────────────────────────────────────────────────

def test_non_interactive_without_answers_file_is_usage_error(wizard: ModuleType, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        wizard.parse_args(["--non-interactive"])
    assert excinfo.value.code == 2
    assert "answers-file" in capsys.readouterr().err


def test_run_no_up_renders_env_and_compose_without_docker(
    wizard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    def fake_compose(dest: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(wizard, "run_compose", fake_compose)
    answers = tmp_path / "answers.env"
    answers.write_text(FULL_ANSWERS + "MANAGER_MDNS_ENABLED=false\n")
    rc = wizard.main(["--dest", str(tmp_path / "dest"), "--no-up",
                      "--non-interactive", "--answers-file", str(answers)])
    assert rc == 0
    dest = tmp_path / "dest"
    env_path = dest / ".env"
    assert env_path.exists()
    assert os.stat(env_path).st_mode & 0o777 == 0o600
    env = env_path.read_text()
    assert "HA_TOKEN=FAKE-HA-TOKEN" in env
    assert "WHISPER_HOST=127.0.0.1" in env and "PIPER_HOST=127.0.0.1" in env
    assert "MANAGER_MDNS_ENABLED=false" in env  # Override ist gelandet
    assert (dest / "docker-compose.yml").exists()
    output = capsys.readouterr().out
    assert "FAKE-HA-TOKEN" not in output  # kein Secret im Log
    assert "<SECRET, len=" in output      # maskiert


def test_run_re_run_keep_preserves_existing_env(wizard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    monkeypatch.setattr(wizard, "run_compose",
                        lambda dest, *a, capture=False: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".env").write_text("MY_LOCAL_KEY=behaltet-mich\n")
    answers = tmp_path / "answers.env"
    answers.write_text(FULL_ANSWERS)  # WIZARD_ENV_MODE default = keep
    rc = wizard.main(["--dest", str(dest), "--no-up", "--non-interactive", "--answers-file", str(answers)])
    assert rc == 0
    env = (dest / ".env").read_text()
    assert "MY_LOCAL_KEY=behaltet-mich" in env      # keep: lokale Zeile bleibt
    assert "HA_TOKEN=FAKE-HA-TOKEN" in env          # Wizard-Keys aktualisiert
    assert "WHISPER_HOST=127.0.0.1" in env


def test_run_re_run_fresh_regenerates_from_template(wizard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    monkeypatch.setattr(wizard, "run_compose",
                        lambda dest, *a, capture=False: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".env").write_text("MY_LOCAL_KEY=weg-mit-mir\n")
    answers = tmp_path / "answers.env"
    answers.write_text(FULL_ANSWERS + "WIZARD_ENV_MODE=fresh\n")
    rc = wizard.main(["--dest", str(dest), "--no-up", "--non-interactive", "--answers-file", str(answers)])
    assert rc == 0
    env = (dest / ".env").read_text()
    assert "MY_LOCAL_KEY" not in env  # fresh: Template-Neuerzeugung


def test_run_distributed_generates_remote_set(wizard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "http_status", lambda *a, **k: (200, None))
    monkeypatch.setattr(wizard, "run_compose",
                        lambda dest, *a, capture=False: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    dest = tmp_path / "dest"
    answers = tmp_path / "answers.env"
    answers.write_text(FULL_ANSWERS.replace("TOPOLOGY=single", "TOPOLOGY=distributed") + "REMOTE_HOST=10.0.0.11\n")
    rc = wizard.main(["--dest", str(dest), "--no-up", "--non-interactive", "--answers-file", str(answers)])
    assert rc == 0
    env = (dest / ".env").read_text()
    assert "WHISPER_HOST=10.0.0.11" in env and "PIPER_HOST=10.0.0.11" in env
    remote = (dest / "docker-compose-remote.yml").read_text()
    assert '"0.0.0.0:10300:10300/tcp"' in remote and '"0.0.0.0:10200:10200/tcp"' in remote
    assert "wyoming-manager:" not in remote.split("services:")[1]


def test_main_returns_1_on_wizard_error(wizard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_args: object) -> int:
        raise wizard.WizardError("kaputte Eingabe")
    monkeypatch.setattr(wizard, "run", boom)
    assert wizard.main([]) == 1


def test_main_returns_130_on_ctrl_c_without_traceback(wizard: ModuleType, capsys: pytest.CaptureFixture) -> None:
    def interrupted(_args: object) -> int:
        raise KeyboardInterrupt
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(wizard, "run", interrupted)
    try:
        assert wizard.main([]) == 130
    finally:
        monkeypatch.undo()
    err = capsys.readouterr().err
    assert "Abgebrochen" in err and "Traceback" not in err
