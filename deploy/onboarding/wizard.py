#!/usr/bin/env python3
"""EVA – CLI-Onboarding-Wizard (P10.T2; PLAN §7 P10, E102/E103/E104).

Der Ein-Befehls-Flow für den Freund ist ``git clone && ./deploy/install.sh``:
der Installer (ohne ``--test``) endet in diesem Wizard. Python 3 stdlib
bevorzugt — kein Framework, keine Abhängigkeiten.

Ablauf (E104 „schlank"):

1. Willkommen + Kurzbeschreibung (was wird installiert, Secrets nur lokal).
2. Topologie (E102): „Alles auf einer Maschine? [J/n]“ — Default Ja.
   Bei „nein“: Adresse des zweiten Hosts (STT/TTS) ⇒ ``WHISPER_HOST``/
   ``PIPER_HOST`` werden abgeleitet und ein **zweites Env/Compose-Set**
   für den Remote-Host generiert (Anleitung scp + ``docker compose up``
   dort; SSH-Automatisierung ist bewusst NICHT Teil des Wizards).
   Single-Host: RAM-Check (freier RAM < ~1,5 GB ⇒ Warnung mit den
   ~700 MB Whisper small+int8). Verteilt: keine
   RAM-Warnung (der STT/TTS-Host trägt die Last).
3. Pflichtfelder: ``HA_BASE_URL`` (Vorschlag ``http://<LAN-IP>:8123``),
   ``HA_TOKEN`` (verdeckt via getpass), ``LLM_API_KEY``. Validierung:
   HA ``GET /api/`` mit Bearer ⇒ 200 („Token ok“) vs. 401 („erreichbar,
   Token falsch“) vs. nicht erreichbar; LLM: Format-Check (erwartet
   ~51 Zeichen) + optionale, **nicht blockierende** Reachability.
   Validierung ist Warnung, kein Blocker (interactive: Nachfrage,
   non-interactive: WARN + fortfahren).
4. Defaults bestätigen statt abfragen: kompakte Tabelle ([Enter] =
   übernehmen), nur bei „e“ (expert) je Feld nachfragen.
5. Zusammenfassung + Bestätigung vor dem Hochfahren.
6. Generieren + Hochfahren: ``.env`` (0600), Compose rendern,
   ``docker compose up -d``, Health-Wait, Summary.

Non-Interactive-Modus (Pflicht für Testbarkeit):
``--non-interactive --answers-file <datei>`` — env-Format mit genau den
Wizard-Fragen (``TOPOLOGY``, ``REMOTE_HOST`` bei verteilt, ``HA_BASE_URL``,
``HA_TOKEN``, ``LLM_API_KEY``, optional ``WIZARD_ENV_MODE=keep|fresh`` und
beliebige weitere ``KEY=VALUE``-Overrides). Ohne Answers-File im
Non-Interactive-Modus ⇒ Fehler mit Usage. Validierungen laufen auch dort
(WARN im Log), damit Tests automatisierbar bleiben.

Re-Run/idempotent: existiert ``.env`` bereits ⇒ fragen übernehmen/bearbeiten/
neu (non-interactive: ``WIZARD_ENV_MODE``). Ctrl-C bricht jederzeit sauber ab
(Meldung, keine halbe Aktion ohne Hinweis). **Keine Secrets in Logs** —
Werte erscheinen höchstens als ``len=N``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ── Konstanten (E104-Defaults, Quellen: `app/config.py`) ────────────
WIZARD_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY_DIR = WIZARD_ROOT / "deploy"
ENV_TEMPLATE = DEPLOY_DIR / "env.template"
COMPOSE_TEMPLATE = DEPLOY_DIR / "compose" / "docker-compose.yml.tmpl"
INITIAL_PROMPT_FILE = DEPLOY_DIR / "initial-prompt.txt"

MANAGER_PORT_DEFAULT = "8767"
MANAGER_MEM_LIMIT_DEFAULT = "512m"
WHISPER_MODEL_DEFAULT = "small"
WHISPER_COMPUTE_TYPE_DEFAULT = "int8"
WHISPER_MEM_LIMIT_DEFAULT = "3g"
PIPER_VOICE_DEFAULT = "de_DE-thorsten-high"
PIPER_MEM_LIMIT_DEFAULT = "1g"
TZ_DEFAULT = "Europe/Berlin"

RAM_WARN_THRESHOLD_BYTES = int(1.5 * 1024**3)   # Auftrag: < ~1,5 GB ⇒ Warnung
LLM_KEY_EXPECTED_LEN = 51                        # erwartete Länge (len=51)
LLM_KEY_MIN_LEN = 20                             # darunter: klar falsch
HTTP_TIMEOUT_S = 6.0
HEALTH_TIMEOUT_S = 900                           # erster Start: Modell-Download
WHISPER_PORT = 10300
PIPER_PORT = 10200

REQUIRED_KEYS = ("HA_BASE_URL", "HA_TOKEN", "LLM_API_KEY")

# Kompakte Default-Tabelle (Schritt 4): Env-Key (None = nur Anzeige), Wert,
# einzeilige Bedeutung. Im expert-Modus wird je echtem Key nachgefragt.
DEFAULTS_TABLE: tuple[tuple[Optional[str], str, str, str], ...] = (
    ("WHISPER_MODEL", "Whisper-Modell", "small + int8",
     "STT (Compose-Arg); gemessen 2,47 s / 4 von 8 exakt (E97/E104)"),
    ("OWW_THRESHOLD", "OWW_THRESHOLD", "0.80",
     "Wake-Schwelle, datenbasiert (P9.T1); Feintuning später im Dashboard"),
    ("DASHBOARD_API_TOKEN", "DASHBOARD_API_TOKEN", "",
     "Auth für POST /api/config/* (E110); leer = offen, gesetzt = Bearer-Pflicht"),
    ("TURN_NO_SPEECH_SECONDS", "TURN_NO_SPEECH_SECONDS", "8",
     "Turn-Abbruch ohne Sprache; VAD läuft geräteseitig (VAD_DEVICE_SIDED)"),
    (None, "initial-prompt", "63 Begriffe (deploy/initial-prompt.txt)",
     "HA-Vokabular für STT; bei anderer HA-Instanz kuratieren"),
    ("MANAGER_MDNS_ENABLED", "MANAGER_MDNS_ENABLED", "true",
     "Dot findet den Manager per mDNS selbst (zustandsloses Pairing)"),
    ("MANAGER_PORT", "MANAGER_PORT", MANAGER_PORT_DEFAULT,
     "WS + Dashboard + /health (mDNS-Payload hängt daran — nicht ändern)"),
)


class WizardError(Exception):
    """Defekte Eingaben/Umgebung — saubere Meldung statt Traceback."""


# ══ 1. Answers-File ══════════════════════════════════════════════════════

def parse_answers(text: str) -> dict[str, str]:
    """Parst ein Answers-File im env-Format (``KEY=VALUE``, ``#`` = Kommentar)."""
    answers: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise WizardError(f"Answers-File Zeile {lineno}: kein '=' in {line!r}")
        key, _, value = line.partition("=")
        answers[key.strip()] = value.strip()
    return answers


def validate_answers(answers: dict[str, str]) -> dict[str, str]:
    """Prüft Pflichtfragen; wirft `WizardError` mit klarem Usage-Hinweis."""
    missing = [k for k in REQUIRED_KEYS if not answers.get(k)]
    if missing:
        raise WizardError(
            "Answers-File unvollständig — fehlen: " + ", ".join(missing)
        )
    topology = answers.get("TOPOLOGY", "single").strip().lower()
    if topology not in ("single", "distributed"):
        raise WizardError("TOPOLOGY muss 'single' oder 'distributed' sein.")
    remote_host = answers.get("REMOTE_HOST", "").strip()
    if topology == "distributed" and not remote_host:
        raise WizardError("TOPOLOGY=distributed braucht REMOTE_HOST (IP des STT/TTS-Hosts).")
    env_mode = answers.get("WIZARD_ENV_MODE", "keep").strip().lower()
    if env_mode not in ("keep", "fresh"):
        raise WizardError("WIZARD_ENV_MODE muss 'keep' oder 'fresh' sein.")
    return dict(answers)


def mask(key: str, value: str) -> str:
    """Loggt Werte nie im Klartext — Secrets erscheinen nur als len=N."""
    upper = key.upper()
    if any(s in upper for s in ("TOKEN", "KEY", "SECRET", "PASSWORD", "PSK")):
        return f"<SECRET, len={len(value)}>"
    return value


# ══ 2. Validierung (HA / LLM / RAM / Remote-Host) ════════════════════════

def http_status(url: str, token: str, timeout: float = HTTP_TIMEOUT_S) -> tuple[Optional[int], Optional[str]]:
    """GET mit Bearer; liefert (status, fehler) — Netzfehler werden NICHT geworfen."""
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return None, str(exc.reason if hasattr(exc, "reason") else exc)


def validate_ha(base_url: str, token: str) -> tuple[str, str]:
    """HA-Check: (verdict, meldung) mit verdict ok|auth|http|unreachable.

    Beleg-Muster (Auftrag): 200 = „erreichbar, Token ok“, 401 = „erreichbar,
    Token falsch“, sonst = „nicht erreichbar“.
    """
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return "unreachable", f"{base_url!r} ist keine http(s)-URL."
    status, error = http_status(f"{base}/api/", token)
    if status == 200:
        return "ok", "HA erreichbar, Token ok (GET /api/ → 200)."
    if status in (401, 403):
        return "auth", f"HA erreichbar, Token falsch (GET /api/ → {status})."
    if status is not None:
        return "http", f"HA erreichbar, unerwarteter Status {status} (GET /api/)."
    return "unreachable", f"HA nicht erreichbar (GET /api/): {error}"


def validate_llm(api_key: str, base_url: str = "https://opencode.ai/zen/v1") -> tuple[bool, str, str]:
    """LLM-Key: (format_ok, verdict, meldung). Reachability ist NICHT blockierend."""
    format_ok = len(api_key) >= LLM_KEY_MIN_LEN
    if len(api_key) == LLM_KEY_EXPECTED_LEN:
        format_note = f"Format ok (len={LLM_KEY_EXPECTED_LEN}, wie erwartet)."
    elif format_ok:
        format_note = f"Format-Abweichung: len={len(api_key)} (erwartet ~{LLM_KEY_EXPECTED_LEN})."
    else:
        format_note = f"Format verdächtig kurz: len={len(api_key)} (erwartet ~{LLM_KEY_EXPECTED_LEN})."
    status, error = http_status(f"{base_url.rstrip('/')}/models", api_key)
    if status == 200:
        return format_ok, "ok", f"{format_note} Gateway erreichbar (/models → 200)."
    if status is not None:
        return format_ok, "ok", f"{format_note} Gateway antwortete {status} (nicht blockierend)."
    return format_ok, "ok", f"{format_note} Gateway nicht prüfbar ({error}) — nicht blockierend."


def read_mem_available(proc_path: str = "/proc/meminfo") -> Optional[int]:
    """MemAvailable in Bytes (None, wenn nicht lesbar — z. B. macOS/Windows)."""
    try:
        with open(proc_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def ram_warning(available: Optional[int]) -> Optional[str]:
    """Single-Host-RAM-Warnung ; None = keine Warnung."""
    if available is None:
        return None
    if available >= RAM_WARN_THRESHOLD_BYTES:
        return None
    free_gib = available / 1024**3
    return (
        f"Wenig freier RAM: {free_gib:.2f} GB verfügbar (Schwelle ~1,5 GB). "
        "Der E104-Default braucht allein für Whisper small+int8 ~700 MB idle, "
        "dazu Manager (~350–450 MB) und Piper (~120–250 MB). "
        "Erwäge Topologie „verteilt“ (STT/TTS auf dem zweiten Host)."
    )


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """TCP-Connect-Check (Wyoming-Ports bei verteilter Topologie)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} erreichbar."
    except OSError as exc:
        return False, f"{host}:{port} nicht erreichbar ({exc})."


# ══ 3. Rendering (.env + Compose, beide Topologien) ══════════════════════

def env_set_text(text: str, key: str, value: str) -> str:
    """Setzt KEY=WERT: ersetzt eine existierende Zeile oder hängt an (E106 b)."""
    marker = f"{key}="
    lines = text.splitlines()
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.startswith(marker):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def render_env(template_text: str, sets: dict[str, str]) -> str:
    """`.env`-Text: Template + alle Setzungen (jeder Key steht wörtlich)."""
    rendered = template_text
    for key, value in sets.items():
        rendered = env_set_text(rendered, key, value)
    return rendered


def initial_prompt_text() -> str:
    """Der E104-Default-Prompt (63 Begriffe) aus deploy/initial-prompt.txt."""
    prompt = INITIAL_PROMPT_FILE.read_text(encoding="utf-8").strip()
    if not prompt:
        raise WizardError(f"{INITIAL_PROMPT_FILE} ist leer.")
    return prompt


def sed_escape(value: str) -> str:
    """Shell/YAML-sicheres Quoting für Command-Argumente (wie install.sh)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def substitute_placeholders(text: str, mapping: dict[str, str]) -> str:
    """Ersetzt @VAR@-Platzhalter; Non-Comment-Rest ⇒ WizardError."""
    for key, value in mapping.items():
        text = text.replace(f"@{key}@", value)
    leftovers = [
        line for line in text.splitlines()
        if "@" in line and not line.lstrip().startswith("#")
    ]
    if leftovers:
        raise WizardError(f"Render unvollständig, Platzhalter übrig: {leftovers[0]!r}")
    return text


def compose_mapping(repo_root: Path, data_dir: Path, initial_prompt: str) -> dict[str, str]:
    """Gemeinsame Platzhalter-Belegung (Werte wie install.sh --test, E104)."""
    return {
        "REPO_ROOT": str(repo_root),
        "DATA_DIR": str(data_dir),
        "TZ": TZ_DEFAULT,
        "MANAGER_MEM_LIMIT": MANAGER_MEM_LIMIT_DEFAULT,
        "WHISPER_MODEL": WHISPER_MODEL_DEFAULT,
        "WHISPER_COMPUTE_TYPE": WHISPER_COMPUTE_TYPE_DEFAULT,
        "WHISPER_MEM_LIMIT": WHISPER_MEM_LIMIT_DEFAULT,
        "PIPER_VOICE": PIPER_VOICE_DEFAULT,
        "PIPER_MEM_LIMIT": PIPER_MEM_LIMIT_DEFAULT,
        "INITIAL_PROMPT": f'"{sed_escape(initial_prompt)}"',
    }


def _split_services(template_text: str) -> tuple[list[str], int]:
    """Liefert (Zeilen, Index von `  wyoming-whisper:`) — Whisper+Piper sind
    im Template die letzten beiden Services, der Manager-Block liegt davor."""
    lines = template_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "wyoming-whisper:" and line.startswith("  "):
            return lines, idx
    raise WizardError("Template defekt: Service 'wyoming-whisper:' nicht gefunden.")


def render_compose_single(template_text: str, repo_root: Path, data_dir: Path) -> str:
    """Single-Host: alle 3 Services im host-Netz, kein Port-Publish."""
    mapping = compose_mapping(repo_root, data_dir, initial_prompt_text())
    publish_comment = "    # (Port-Publish: nur bei verteilter Topologie — hier host-Netz.)"
    mapping["WHISPER_PUBLISH"] = publish_comment
    mapping["PIPER_PUBLISH"] = publish_comment
    return substitute_placeholders(template_text, mapping) + "\n"


def render_compose_manager_only(template_text: str, repo_root: Path, data_dir: Path) -> str:
    """Verteilt, Host A: nur der Manager-Service (Whisper/Piper-Blöcke entfernt)."""
    lines, whisper_idx = _split_services(template_text)
    return render_compose_single("\n".join(lines[:whisper_idx]) + "\n", repo_root, data_dir)


def render_compose_remote(template_text: str, remote_data_dir: str) -> str:
    """Verteilt, Host B: Whisper+Piper im Bridge-Netz mit Port-Publish.

    Nur die beiden letzten Services (whisper/piper) + ein eigener Kopf — der
    Manager-Service gehört auf Host A (docker-compose.yml, manager_only).
    """
    lines, whisper_idx = _split_services(template_text)
    services = "\n".join(lines[whisper_idx:])
    # host-Netz ⇒ Bridge + Publish (Muster der verteilten Compose).
    services = services.replace("    network_mode: host\n", "")
    services = services.replace(
        "@WHISPER_PUBLISH@\n",
        "    ports:\n      - \"0.0.0.0:10300:10300/tcp\"\n",
    )
    services = services.replace(
        "@PIPER_PUBLISH@\n",
        "    ports:\n      - \"0.0.0.0:10200:10200/tcp\"\n",
    )
    header = (
        "# EVA – Remote-Set (STT/TTS, verteilt; generiert von deploy/onboarding/wizard.py)\n"
        "# Host B: whisper + piper im Bridge-Netz mit Port-Publish (Live-.106-Muster).\n"
        "# Auf Host B deployen (SSH-Automatisierung bewusst später):\n"
        "#   scp dies nach /opt/eva/docker-compose.yml, dann:\n"
        "#   mkdir -p /opt/eva/data/whisper /opt/eva/data/piper\n"
        "#   docker compose --project-directory /opt/eva up -d\n"
        "\n"
        "services:\n"
    )
    mapping = compose_mapping(Path("/opt/eva-src"), Path(remote_data_dir), initial_prompt_text())
    return substitute_placeholders(header + services + "\n", mapping) + "\n"


def write_env_0600(path: Path, text: str) -> None:
    """Schreibt `.env` mit 0600 (auch wenn die Datei schon lockerere Rechte hat)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


# ══ 4. Docker / Health ═══════════════════════════════════════════════════

def compose(dest: Path, *args: str) -> list[str]:
    return ["docker", "compose", "--project-directory", str(dest), *args]


def run_compose(dest: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        compose(dest, *args), check=False, capture_output=capture, text=True,
    )


def wait_healthy(dest: Path, expected: int, timeout_s: int, log: Callable[[str], None]) -> bool:
    """Pollt `compose ps` bis `expected` Container healthy sind (oder Timeout)."""
    elapsed = 0
    while True:
        proc = run_compose(dest, "ps", "--format", "{{.Name}} {{.Health}}", capture=True)
        states = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        healthy = sum(1 for ln in states if ln.endswith(" healthy"))
        if healthy >= expected:
            for line in sorted(states):
                log(f"  {line}")
            return True
        if elapsed >= timeout_s:
            for line in states:
                log(f"  {line}")
            return False
        log(f"Warte auf {expected} healthy Container ({elapsed}s / {timeout_s}s) …")
        time.sleep(5)
        elapsed += 5


def manager_health(port: str = MANAGER_PORT_DEFAULT) -> tuple[bool, str]:
    """GET /health am Manager (127.0.0.1) — Liefert (ok, body/fehler)."""
    status, error = http_status(f"http://127.0.0.1:{port}/health", token="", timeout=10.0)
    if status == 200:
        return True, "/health 200"
    return False, f"/health nicht 200 (status={status}, fehler={error})"


PAIRING_TIMEOUT_S = 3.0   # Summary darf niemals blockieren (P10.T3)


def fetch_pairing(port: str = MANAGER_PORT_DEFAULT) -> tuple[Optional[dict], Optional[str]]:
    """GET /api/pairing am Manager (127.0.0.1) — (payload, fehler), nie Blocker.

    Kleiner Timeout (3 s) und alle Fehler werden als `(None, fehler)`
    zurückgegeben statt geworfen: die Summary zeigt im Zweifel nur den
    Hinweistext „Status nicht abfragbar".
    """
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pairing")
        with urllib.request.urlopen(request, timeout=PAIRING_TIMEOUT_S) as resp:
            import json as _json

            return _json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:  # noqa: BLE001 – bewusst: Summary darf nie scheitern
        return None, str(exc)


def pairing_summary_lines(payload: Optional[dict], error: Optional[str]) -> list[str]:
    """Summary-Zeilen aus dem Pairing-Payload (P10.T3; ehrlich, nicht blockierend)."""
    if payload is None:
        return [f"  Dot-Pairing : Status nicht abfragbar ({error}) — "
                "später prüfen: curl http://127.0.0.1:8767/api/pairing"]
    status = str(payload.get("status"))
    devices = payload.get("devices") or []
    ids = ", ".join(str(d.get("device_id")) for d in devices) or "keine"
    if status == "paired":
        synced = all(d.get("mic_synced") is True for d in devices)
        note = "" if synced else " (Mic-Stream noch nicht gelaufen — wartet auf ersten Frame)"
        return [f"  Dot-Pairing : verbunden: {ids}{note}"]
    if status == "waiting":
        return [
            "  Dot-Pairing : mDNS an — warte auf deinen Dot… (verbunden: keine)",
            "                Kein Dot nach ~1 Minute? Siehe INSTALL.md §Fehlersuche "
            "(WLAN, Firewall-Port 8767, Doppel-Announce).",
        ]
    if status == "mdns_off":
        return [
            "  Dot-Pairing : mDNS AUS (MANAGER_MDNS_ENABLED=false) — der Dot kann "
            "den Manager nicht selbst finden. Zum Pairing auf true stellen.",
        ]
    return [f"  Dot-Pairing : Status unbekannt ({payload.get('issues') or 'issues leer'})"]


# ══ 5. Wizard-Konfiguration ══════════════════════════════════════════════

@dataclass
class WizardConfig:
    """Alle Antworten des Wizards — renderbar in .env + Compose."""
    topology: str                       # "single" | "distributed"
    remote_host: str = ""
    ha_base_url: str = ""
    ha_token: str = ""
    llm_api_key: str = ""
    overrides: dict[str, str] = field(default_factory=dict)   # beliebige KEY=VALUE
    fresh_env: bool = False             # existierende .env verwerfen?

    @property
    def whisper_host(self) -> str:
        return "127.0.0.1" if self.topology == "single" else self.remote_host

    @property
    def piper_host(self) -> str:
        return self.whisper_host        # STT/TTS sitzen immer auf demselben Host

    def env_sets(self) -> dict[str, str]:
        """Explizite Setzungen: Pflichtfelder + Abgeleitete + Overrides (E106 b)."""
        sets = {
            "HA_BASE_URL": self.ha_base_url,
            "HA_TOKEN": self.ha_token,
            "LLM_API_KEY": self.llm_api_key,
            "WHISPER_HOST": self.whisper_host,
            "PIPER_HOST": self.piper_host,
        }
        sets.update(self.overrides)
        return sets

    def summary_lines(self) -> list[str]:
        lines = [
            f"  Topologie   : {'ein Host (Manager + STT + TTS)' if self.topology == 'single' else 'verteilt (Manager hier, STT/TTS auf ' + self.remote_host + ')'}",
            f"  WHISPER_HOST: {self.whisper_host}",
            f"  PIPER_HOST  : {self.piper_host}",
            f"  HA_BASE_URL : {self.ha_base_url}",
            f"  HA_TOKEN    : {mask('HA_TOKEN', self.ha_token)}",
            f"  LLM_API_KEY : {mask('LLM_API_KEY', self.llm_api_key)}",
        ]
        for key in sorted(self.overrides):
            lines.append(f"  Override    : {key}={mask(key, self.overrides[key])}")
        return lines


# ══ 6. Interaktive Sammlung ══════════════════════════════════════════════

def ask_bool(prompt: str, default_yes: bool) -> bool:
    suffix = "[J/n]" if default_yes else "[j/N]"
    while True:
        raw = input(f"{prompt} {suffix} ").strip().lower()
        if not raw:
            return default_yes
        if raw in ("j", "y", "ja", "yes"):
            return True
        if raw in ("n", "nein", "no"):
            return False
        print("  Bitte 'j' oder 'n'.")


def ask_text(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt}" + (f" [{default}]" if default else "") + " ").strip()
    return raw or default


def lan_ip_suggestion() -> str:
    """Erste IPv4 des Hosts als Vorschlag für HA_BASE_URL (best effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # kein Paket geht raus, nur Routing
            return sock.getsockname()[0]
    except OSError:
        return "<LAN-IP>"


def collect_interactive() -> WizardConfig:
    """Der E104-Dialog (Schritte 1–5)."""
    print(
        "\nWillkommen beim EVA-Onboarding.\n"
        "  Installiert wird: Manager (Wake-Word, Router, Dashboard) + Whisper (STT) + Piper (TTS).\n"
        "  Abgefragt werden nur 3 Pflichtfelder (Home-Assistant-URL/Token, LLM-Key).\n"
        "  Secrets landen ausschließlich lokal in der .env (chmod 600) — nie ins Git.\n"
        "  Der Echo-Dot verbindet sich danach selbst (mDNS, zustandsloses Pairing).\n"
    )
    distributed = not ask_bool("Alles auf einer Maschine?", default_yes=True)
    remote_host = ""
    if distributed:
        remote_host = ask_text("Adresse des zweiten Hosts (STT/TTS): ")
        if not remote_host:
            raise WizardError("Verteilte Topologie braucht die Adresse des zweiten Hosts.")
        reachable, detail = tcp_reachable(remote_host, WHISPER_PORT)
        print(f"  Hinweis: Wyoming-STT auf {remote_host}:10300 "
              + ("erreichbar." if reachable else "noch nicht erreichbar — erwartet, wenn der zweite Host erst noch deployt wird (Anleitung folgt am Ende)."))
    config = WizardConfig(topology="distributed" if distributed else "single",
                          remote_host=remote_host)
    if not distributed:
        available = read_mem_available()
        warning = ram_warning(available)
        if warning:
            print(f"  ⚠ WARNUNG: {warning}")
    else:
        print("  (RAM-Warnung entfällt: STT/TTS laufen auf dem zweiten Host.)")

    # Pflichtfelder
    suggestion = f"http://{lan_ip_suggestion()}:8123"
    config.ha_base_url = ask_text("Home-Assistant-Basis-URL", suggestion)
    while True:
        config.ha_token = getpass.getpass("Home-Assistant-Token (Eingabe verdeckt): ")
        if config.ha_token:
            break
        print("  Der Token darf nicht leer sein.")
    config.llm_api_key = getpass.getpass("LLM-API-Key (Eingabe verdeckt): ")
    if not config.llm_api_key:
        raise WizardError("LLM_API_KEY darf nicht leer sein.")

    # Validierung (Warnung, nicht Blocker)
    verdict, detail = validate_ha(config.ha_base_url, config.ha_token)
    print(f"  HA-Check: {detail}")
    if verdict != "ok" and not ask_bool("Trotzdem fortfahren?", default_yes=False):
        raise WizardError("Abgebrochen — HA-URL/Token prüfen und erneut starten.")
    format_ok, _, llm_detail = validate_llm(config.llm_api_key)
    print(f"  LLM-Check: {llm_detail}")
    if not format_ok and not ask_bool("Trotzdem fortfahren?", default_yes=False):
        raise WizardError("Abgebrochen — LLM-Key prüfen und erneut starten.")

    # Defaults (E104: bestätigen statt abfragen; 'e' = expert ⇒ je Feld).
    print("\nSinnvoll-Defaults (aus den Messdaten, E104):")
    for _key, name, value, why in DEFAULTS_TABLE:
        print(f"  {name:<26} = {value:<40} ({why})")
    choice = input("Defaults übernehmen? ([Enter] = ja / n = abbrechen / e = expert) ").strip().lower()
    if choice == "n":
        raise WizardError("Abgebrochen — nichts verändert.")
    if choice == "e":
        for key, _name, value, _why in DEFAULTS_TABLE:
            if key is None:
                continue
            new = ask_text(f"  {key}", value)
            if new != value:
                config.overrides[key] = new
        while True:
            raw = ask_text("Weitere KEY=VALUE (leer = fertig): ")
            if not raw:
                break
            if "=" not in raw:
                print("  Erwartet KEY=VALUE.")
                continue
            key, _, val = raw.partition("=")
            config.overrides[key.strip().upper()] = val.strip()
    return config


def collect_from_answers(answers: dict[str, str]) -> WizardConfig:
    """Non-Interactive: Config aus dem geprüften Answers-File."""
    reserved = {"TOPOLOGY", "REMOTE_HOST", "HA_BASE_URL", "HA_TOKEN", "LLM_API_KEY", "WIZARD_ENV_MODE"}
    return WizardConfig(
        topology=answers["TOPOLOGY"],
        remote_host=answers.get("REMOTE_HOST", ""),
        ha_base_url=answers["HA_BASE_URL"],
        ha_token=answers["HA_TOKEN"],
        llm_api_key=answers["LLM_API_KEY"],
        overrides={k: v for k, v in answers.items() if k not in reserved},
        fresh_env=answers.get("WIZARD_ENV_MODE", "keep") == "fresh",
    )


# ══ 7. Hauptablauf ═══════════════════════════════════════════════════════

def run(args: argparse.Namespace, log: Callable[[str], None] = print) -> int:
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "data" / "whisper").mkdir(parents=True, exist_ok=True)
    (dest / "data" / "piper").mkdir(parents=True, exist_ok=True)
    env_path = dest / ".env"
    compose_path = dest / "docker-compose.yml"

    answers: dict[str, str] = {}
    if args.non_interactive:
        if not args.answers_file:
            raise WizardError("--non-interactive braucht --answers-file DATEI "
                              "(env-Format mit TOPOLOGY/HA_BASE_URL/HA_TOKEN/LLM_API_KEY).")
        raw = Path(args.answers_file).read_text(encoding="utf-8")
        answers = validate_answers(parse_answers(raw))
        config = collect_from_answers(answers)
    else:
        config = collect_interactive()

    # Re-Run/idempotent: existierende .env respektieren oder ersetzen.
    if env_path.exists() and not config.fresh_env:
        log(f"Existierende {env_path} gefunden — Wizard-Keys werden aktualisiert, "
            "der Rest bleibt (idempotent).")
        base_env = env_path.read_text(encoding="utf-8")
    elif env_path.exists():
        log(f"Existierende {env_path} wird neu erzeugt (WIZARD_ENV_MODE=fresh).")
        base_env = ENV_TEMPLATE.read_text(encoding="utf-8")
    else:
        base_env = ENV_TEMPLATE.read_text(encoding="utf-8")

    # Validierung auch non-interactive (WARN, fortfahren) — Testbarkeit.
    # Interaktiv wurde bereits in collect_interactive() validiert + nachgefragt.
    if args.non_interactive:
        verdict, detail = validate_ha(config.ha_base_url, config.ha_token)
        log(f"WARN (HA-Validierung, verdict={verdict}): {detail}")
        format_ok, _, llm_detail = validate_llm(config.llm_api_key)
        log(f"WARN (LLM-Validierung, format_ok={format_ok}): {llm_detail}")

    # Nicht-interaktiv: Defaults-Tabelle + Overrides zeigen (Werte maskiert).
    if args.non_interactive:
        log("E104-Defaults aktiv: "
            + "; ".join(f"{k}={v}" for k, _n, v, _w in DEFAULTS_TABLE if k is not None))
        for key in sorted(config.overrides):
            log(f"Override: {key}={mask(key, config.overrides[key])}")

    # Zusammenfassung
    log("So wird deployt:")
    for line in config.summary_lines():
        log(line)

    if not args.non_interactive and not ask_bool("Starten?", default_yes=True):
        log("Abgebrochen — nichts verändert.")
        return 0

    # Generieren: .env (0600) + Compose (beide Topologien aus derselben Vorlage).
    write_env_0600(env_path, render_env(base_env, config.env_sets()))
    template_text = COMPOSE_TEMPLATE.read_text(encoding="utf-8")
    if config.topology == "single":
        compose_path.write_text(
            render_compose_single(template_text, WIZARD_ROOT, dest / "data"),
            encoding="utf-8",
        )
    else:
        compose_path.write_text(
            render_compose_manager_only(template_text, WIZARD_ROOT, dest / "data"),
            encoding="utf-8",
        )
        remote_compose = dest / "docker-compose-remote.yml"
        remote_compose.write_text(
            render_compose_remote(template_text, "/opt/eva/data"),
            encoding="utf-8",
        )
        log(
            f"Remote-Set erzeugt: {remote_compose}\n"
            f"  Auf {config.remote_host} ausführen (SSH-Automatisierung kommt später):\n"
            f"    scp {remote_compose} {config.remote_host}:/opt/eva/docker-compose.yml\n"
            f"    ssh {config.remote_host} 'mkdir -p /opt/eva/data/whisper /opt/eva/data/piper && "
            f"docker compose --project-directory /opt/eva up -d'\n"
            f"  (Modelle laden beim ersten Start ~574 MB aus dem Internet.)"
        )

    check = run_compose(dest, "config", "-q", capture=True)
    if check.returncode != 0:
        raise WizardError(f"Gerenderte Compose ist invalide: {check.stderr.strip()}")

    if args.no_up:
        log("--no-up: gerendert, nicht gestartet "
            f"({env_path}, {compose_path}"
            + (", docker-compose-remote.yml" if config.topology == "distributed" else "") + ").")
        return 0

    log("Starte Stack (docker compose up -d --build; erster Start lädt Modelle ~574 MB) …")
    up = run_compose(dest, "up", "-d", "--build")
    if up.returncode != 0:
        raise WizardError(f"docker compose up fehlgeschlagen:\n{up.stderr.strip()}")

    expected = 3 if config.topology == "single" else 1
    if not wait_healthy(dest, expected, HEALTH_TIMEOUT_S, log):
        raise WizardError(
            f"Health-Timeout nach {HEALTH_TIMEOUT_S}s. Logs: "
            f"docker compose --project-directory {dest} logs"
        )
    ok, health = manager_health()
    if not ok:
        raise WizardError(f"Manager nicht healthy: {health}")

    # P10.T3: Pairing-Status in der Summary (nicht blockierend, kleiner Timeout).
    pairing_payload, pairing_error = fetch_pairing()

    # Summary (Schritt 6)
    log("═══ EVA-Deploy fertig ═══")
    for line in config.summary_lines():
        log(line)
    log(f"  .env        : {env_path} (chmod 600, Secrets nur lokal)")
    log(f"  /health     : {health}")
    for line in pairing_summary_lines(pairing_payload, pairing_error):
        log(line)
    host_ip = lan_ip_suggestion()
    log(f"  Dashboard   : http://{host_ip}:{MANAGER_PORT_DEFAULT}/dashboard "
        "(Auth kommt später — erst im LAN nutzen)")
    log("  Als nächstes: Echo-Dot verbindet sich selbst (mDNS, zustandsloses Pairing); "
        "erste Sätze am Gerät testen.")
    if config.topology == "distributed":
        log(f"  WICHTIG: erst auf {config.remote_host} das Remote-Set hochfahren "
            "(Anleitung oben), sonst bleiben STT/TTS tot.")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EVA CLI-Onboarding-Wizard (P10.T2).",
        epilog="Non-Interactive: --non-interactive --answers-file DATEI",
    )
    parser.add_argument("--dest", default="/opt/eva", help="Zielverzeichnis (Default /opt/eva).")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Keine Fragen: Antworten aus --answers-file.")
    parser.add_argument("--answers-file", default=None,
                        help="env-Format: TOPOLOGY, REMOTE_HOST, HA_BASE_URL, HA_TOKEN, "
                             "LLM_API_KEY, optional WIZARD_ENV_MODE + KEY=VALUE-Overrides.")
    parser.add_argument("--no-up", action="store_true",
                        help="Nur rendern (.env/Compose), Stack nicht hochfahren.")
    args = parser.parse_args(argv)
    if args.non_interactive and not args.answers_file:
        parser.error("--non-interactive braucht --answers-file DATEI")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        print("\n[eva] Abgebrochen (Ctrl-C).", file=sys.stderr)
        print("Falls der Stack schon hochfuhr: docker compose --project-directory "
              "<dest> down — nichts läuft halb weiter.", file=sys.stderr)
        return 130
    except WizardError as exc:
        print(f"[eva] FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
