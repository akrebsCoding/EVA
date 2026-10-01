"""Gemeinsame Test-Fixtures des wyoming-managers (P1.T4, `PLAN.md` §7 → P1.T4).

Auftrag laut Plan: **minimale, deterministische Fixtures `tmp_wav` und
`mic_frame`, ein Cleanup, kein Netzzugang.**  Kein echtes Gerät, kein
Home-Assistant, kein LLM, kein Wake-Modell — alles Layer **L0** (`PLAN.md` §7.1:
„rein, kein Netz", ausführbar lokal auf `.22`).

Die Audio-Konstanten sind **verifizierte Ist-Werte**, keine Annahmen:

============================  =====  ==============================================
Konstante                     Wert   Quelle
============================  =====  ==============================================
`audio_mic_rate`              16000  PLAN §4 / `app/config.py`; Mic-Stream 16 kHz
`audio_speaker_rate`          48000  PLAN §4; Piper nativ 22050, Manager sendet
                                     48000 (`docs/ECOMUSE_PROTOCOL.md` §D5)
`audio_width` / `audio_channels`  2 / 1  S16_LE mono
`oww_chunk_bytes`             2560   80 ms @ 16 kHz S16_LE mono
Mic-Frame-Typ                 0x01   `docs/reference/em_controller.py:276`
Mic-Header                    3 B    `[0x01][seq_hi][seq_lo]` (`…:290`)
Mic-Frame gesamt              2563 B  3 + 2560 (K2, `STATE.md` §4/E28)
Sequenznummer                 uint16 **big-endian**, Geräteseite
                                     `binary.BigEndian.PutUint16(frame[1:3], seq)`
                                     (`docs/reference/…` entspricht
                                     `/tmp/echomuse/device/internal/client/data.go:1046`)
============================  =====  ==============================================

Die Zahlen werden **aus `app.config.settings` gelesen** (Single Source of
Truth) und nur für den Betrieb ohne `pydantic` hart hinterlegt — dieselbe
Degradationsidee wie in `app/logger.py`.  Die Testdatei prüft beide Wege
gegeneinander.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import wave
from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import pytest

# `app.logger` läuft ohne `pydantic` (try/except im Modul) – `app.config` nicht.
# Für die Konstanten wird deshalb auf fest belegte Werte zurückgefallen.
import app.logger as logger_mod
from app.logger import LOGGER_NAMESPACE

try:
    from app.config import settings
except ImportError:  # pragma: no cover - nur ohne pydantic.
    settings = None  # type: ignore[assignment]

#: Projektwurzel – eine Ebene über `tests/`, wie `app.config.PROJECT_ROOT`.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# ── Audio-Konstanten (Settings ⇒ Fallback) ──────────────────────────────
MIC_RATE: Final[int] = getattr(settings, "audio_mic_rate", 16000)
SPEAKER_RATE: Final[int] = getattr(settings, "audio_speaker_rate", 48000)
SAMPLE_WIDTH: Final[int] = getattr(settings, "audio_width", 2)
CHANNELS: Final[int] = getattr(settings, "audio_channels", 1)
CHUNK_BYTES: Final[int] = getattr(settings, "oww_chunk_bytes", 2560)
#: Bytes pro Mikrofon-Frame: 3-Byte-Header + `CHUNK_BYTES` PCM (K2/E28).
MIC_FRAME_BYTES: Final[int] = 3 + CHUNK_BYTES
#: Länge eines Mic-Chunks in Millisekunden — **die** 80 ms aus P0.T6/E28.
MIC_CHUNK_MS: Final[float] = (
    CHUNK_BYTES / (SAMPLE_WIDTH * CHANNELS * MIC_RATE) * 1000.0
)
#: Mikrofon-Frame auf der Wire: `[0x01][seq_hi][seq_lo]` + PCM.
MIC_FRAME_TYPE: Final[int] = 0x01
MIC_HEADER_LEN: Final[int] = 3
#: `seq` ist uint16 ⇒ gültig sind 0…65535.  Geräteseite ist `var seqNum uint16`
#: mit `seqNum++` (**Wrap** 65535 → 0, keine Fehlermeldung), deshalb begrenzt
#: die Factory den Wert hart, statt still zu überlaufen.
MIC_SEQ_MAX: Final[int] = 0xFFFF

#: Dauer des `tmp_wav`-Fixtures (klein genug für jeden Lauf, groß genug, um
#: „Bytes pro Sekunde" rechnerisch zu prüfen).
TMP_WAV_SECONDS: Final[float] = 0.2
TMP_WAV_FRAMES: Final[int] = int(SPEAKER_RATE * TMP_WAV_SECONDS)

#: Temporäre Artefakte nach `.gitignore:38-42` — dieselbe Konvention, damit
#: „was temporär ist" nur an einer Stelle steht.
TEMP_ARTIFACT_PATTERNS: Final[tuple[str, ...]] = (
    "*.wav.tmp",
    "*.raw.tmp",
    "*.pcm.tmp",
    "*.onnx.tmp",
)

#: Marker, in deren Tests **Netzzugang erlaubt** ist.  L0 (`unit`) ist per
#: §7.1 netzfrei; L1/L2/L5 sprechen mit localhost bzw. `.123`.  `wake`/`slow`
#: bleiben gesperrt (sie laufen lokal ohne Dienst, PLAN §7.1 L4).
NETWORK_ALLOWED_MARKS: Final[frozenset[str]] = frozenset(
    {"component", "integration", "live"}
)

#: Zielverzeichnis der JUnit-XML-Berichte (git-ignoriert, `.gitignore:34`).
JUNIT_DIRNAME: Final[str] = "reports/junit"

#: Reihenfolge, in der die Layer-Marker einem Test zugeordnet werden – der
#: spezifischere (integrativere) Marker gewinnt, falls ein Test mehrere trägt.
#: `slow` ist **kein** Layer, sondern nur eine Laufzeit-Angabe (`PLAN.md` §7.1).
LAYER_MARKER_ORDER: Final[tuple[str, ...]] = (
    "integration",
    "live",
    "wake",
    "component",
    "unit",
)

#: Testkonfiguration für den Manager-Subprozess – **nur Platzhalter/keine
#: Secrets**.  Wird als OS-Env an den Subprozess gereicht und übersteuert so
#: die echte `.env` (pydantic-settings: OS-Env > Dotenv).
TEST_ENV_FILE: Final[Path] = PROJECT_ROOT / ".env.test"
#: Log-Verzeichnis des Manager-Subprozesses (git-ignoriert, unter `reports/`).
MANAGER_LOG_DIRNAME: Final[str] = "reports/manager-proc"
#: Frist für den `/health`-Startnachweis (Sekunden).
MANAGER_HEALTH_TIMEOUT: Final[float] = 30.0
#: Frist für ein sauberes SIGTERM-Herunterfahren des Manager-Subprozesses.
MANAGER_STOP_TIMEOUT: Final[float] = 10.0


class NetworkAccessBlocked(RuntimeError):
    """Netzzugang in einem L0-Test — verstößt gegen `PLAN.md` §7.1 (L0)."""


# ── PCM-Bausteine ───────────────────────────────────────────────────────
def triangle_sample(index: int) -> int:
    """Ganzzahliges Dreieck 0…30000, Periode 800 Samples.

    Bewusst **ohne** `math.sin`: das wäre libm- und damit plattformabhängig,
    und ein Testfixture soll bytegleich rechnen.  30000 < 32767 ⇒ passt in
    `int16`.
    """
    phase = index % 800
    return 30000 - (phase * 75 if phase < 400 else (800 - phase) * 75)


def triangle_pcm(frames: int) -> bytes:
    """`frames` Samples S16_LE (Bytefolge **little endian**, wie im WAV/PCM)."""
    if array("h").itemsize != SAMPLE_WIDTH:  # pragma: no cover - exotische Plattform
        raise RuntimeError("Dieses Fixture setzt ein 16-Bit-`array('h')` voraus")
    samples = array("h", (triangle_sample(i) for i in range(frames)))
    if sys.byteorder != "little":  # pragma: no cover - x86/ARM sind little endian
        samples.byteswap()
    return samples.tobytes()


def write_wav(
    path: Path,
    *,
    rate: int = SPEAKER_RATE,
    frames: int = TMP_WAV_FRAMES,
) -> Path:
    """S16_LE-mono-WAV mit deterministischem Inhalt; legt sie unter `path` an.

    Standard ist die **Speaker**-Seite (48 kHz): der Mic-Stream ist auf der
    Wire rohes PCM **ohne** WAV-Header (`[0x01][seq][pcm]`), ein Container
    ergibt dort keinen Sinn.
    """
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(rate)
        handle.writeframes(triangle_pcm(frames))
    return path


# ── Aufräumen ───────────────────────────────────────────────────────────
def purge_test_artifacts(root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Entfernt temporäre Test-Artefakte; gibt die gelöschten Pfade zurück.

    Bewusst eng gefasst, damit niemandem echte Arbeit weggeräumt wird:

    * nur die vier `*.tmp`-Muster aus `.gitignore:38-42`,
    * in der Projektwurzel nur in der Wurzel selbst (`glob`, **nicht**
      rekursiv — `docs/reference/` und `.venv/` bleiben unangetastet), unter
      `reports/` dagegen rekursiv (`rglob`, denn das ist der erzeugte Baum),
    * danach leere Unterverzeichnisse von `reports/` (P9-Reportordner),
      **nicht** `reports/` selbst (das ist das Ausgabeziel der JUnit-XML).
    """
    removed: list[Path] = []
    for directory, recursive in ((root, False), (root / "reports", True)):
        if not directory.is_dir():
            continue
        for pattern in TEMP_ARTIFACT_PATTERNS:
            found = directory.rglob(pattern) if recursive else directory.glob(pattern)
            for path in sorted(found):
                if path.is_file():
                    path.unlink()
                    removed.append(path)
    reports_dir = root / "reports"
    if reports_dir.is_dir():
        for path in sorted(reports_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
                removed.append(path)
    return tuple(removed)


@pytest.fixture(autouse=True)
def _restore_manager_logging() -> Iterator[None]:
    """Globalen Zustand nach jedem Test zurücksetzen + Artefakte entfernen.

    Additiv zu `tests/test_logger.py::_restore_logging` und mit derselben
    Regel: **nur** Handler entfernen, die beim Setup nicht da waren.  pytest
    hängt an jeden nicht-propagierenden Logger (u. a. `manager`) seine eigenen
    `LogCaptureHandler`; wer die wegräumt, beschädigt die Aufzeichnung.
    (`STATE.md` §5/P1.T1 — genau dieser Fehler ist dort schon einmal passiert.)
    """
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    handlers_before = list(manager_logger.handlers)
    level_before = manager_logger.level
    propagate_before = manager_logger.propagate
    handler_installed_before = logger_mod._handler_installed
    warned_before = logger_mod._warned_invalid_level
    try:
        yield
    finally:
        for handler in list(manager_logger.handlers):
            if handler not in handlers_before:
                manager_logger.removeHandler(handler)
        manager_logger.setLevel(level_before)
        manager_logger.propagate = propagate_before
        logger_mod._handler_installed = handler_installed_before
        logger_mod._warned_invalid_level = warned_before
        purge_test_artifacts()


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Netzzugang in L0-Tests blockieren (`PLAN.md` §7.1: L0 = „rein, kein Netz").

    Umgesetzt wird **nur der ausgehende Teil** — `connect`, `connect_ex`,
    `create_connection`, `getaddrinfo`, `sendto`.  Das Anlegen von Sockets und
    `send`/`recv` auf verbundenen Sockets bleiben erlaubt, weil `asyncio` seine
    Self-Pipe über `socket.socketpair()` baut: ein pauschales Verbieten von
    `socket.socket` würde die **asynchronen** L0-Tests (Pipeline, P2.T0/P5)
    beim Erstellen des Event-Loops brechen.  DNS sowie TCP- und UDP-Versuche
    nach außen fallen dagegen auf.

    Für `component`/`integration`/`live` (L1/L2/L5 – localhost bzw. `.123`)
    ist die Sperre aus; sie ist damit **kein** Ersatz für `pytest-socket`,
    das bewusst **nicht** eingeführt wird (nicht installiert, PLAN §9/P9
    entscheidet das).
    """
    if any(request.node.get_closest_marker(name) for name in NETWORK_ALLOWED_MARKS):
        return

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessBlocked(
            "Netzzugang ist in diesem Test nicht erlaubt (L0, PLAN §7.1); "
            "Netz braucht den Marker component/integration/live."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "sendto", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


# ── L2-Harness: der Manager als echter uvicorn-Subprozess (P9.T0) ────────
#
# Der Layer L2 (`PLAN.md` §7.1) testet die Fakes gegen den **laufenden**
# Manager.  Statt den Manager in-process zu importieren (das wäre eine
# Attrappe), startet `manager_proc` `python -m app.main` als **echten
# Subprozess** auf `TEST_MANAGER_PORT`.  Der Test-Prozess spricht ihn über
# HTTP/WS an.
#
# **E36-Netzsperre (Lösung für L2):** Die autouse-Sperre `_block_network`
# blockiert `connect`/`connect_ex`/`sendto`/`create_connection`/`getaddrinfo`
# **nur für Tests ohne** `component`/`integration`/`live` (siehe
# `NETWORK_ALLOWED_MARKS`).  L2-Tests tragen den Marker `integration` und sind
# damit **gezielt ausgenommen** – die Sperre bleibt für L0 (und für `wake`/
# `slow`) vollständig aktiv.  Der Subprozess selbst läuft in einem **eigenen
# Prozess** und ist von der `monkeypatch`-Sperre des Test-Prozesses ohnehin
# nicht betroffen.  L2-Tests **müssen** den Marker `integration` (oder `live`)
# tragen; jeder andere Marker bekäme beim ersten `/health`-Aufruf
# `NetworkAccessBlocked` und damit einen klaren Fehler.


def load_env_file(path: Path) -> dict[str, str]:
    """Einfache `.env`-Datei lesen (KEY=VALUE, `#`-Kommentare, Inline-Werte).

    Bewusst minimal (kein Interpolations-/Quote-Parser): die Testkonfiguration
    ist ein kontrolliertes File.  Inline-Kommentare nach einem Leerzeichen
    werden abgeschnitten, damit `KEY=value  # erklärung` nicht am Wert klebt.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"} and len(value) >= 2:
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class ManagerProcess:
    """Handle auf den laufenden Manager-Subprozess (vom `manager_proc`-Fixture)."""

    process: subprocess.Popen[bytes]
    port: int
    base_url: str
    log_path: Path
    log_handle: Any = field(repr=False, default=None)
    health_payload: dict[str, Any] = field(default_factory=dict)

    def is_alive(self) -> bool:
        """True, solange der Subprozess noch nicht beendet wurde."""
        return self.process.poll() is None

    def log_text(self) -> str:
        """Bisher geschriebenes Log des Subprozesses (Ersatzzeichen-sicher)."""
        if not self.log_path.is_file():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def fetch_health(base_url: str, timeout: float = 2.0) -> dict[str, Any]:
    """`GET <base_url>/health` und JSON-Body lesen (L2, echtes HTTP)."""
    with urllib.request.urlopen(f"{base_url}/health", timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"/health lieferte HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(mp: ManagerProcess, timeout: float = MANAGER_HEALTH_TIMEOUT) -> dict[str, Any]:
    """Auf `/health` == HTTP 200 warten; bei Prozess-Tod sofort abbrechen.

    Wirft `RuntimeError`, wenn die Frist verstreicht oder der Prozess vorher
    endet – der Fixture fährt dann hart herunter (kein Zombie).
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if mp.process.poll() is not None:
            raise RuntimeError(
                f"Manager-Prozess endete vor dem Health-Check "
                f"(rc={mp.process.returncode}); Log: {mp.log_path}"
            )
        try:
            return fetch_health(mp.base_url)
        except Exception as exc:  # noqa: BLE001 – Startphase, jedes Netz-/HTTP-Ergebnis zählt.
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(
        f"/health nicht binnen {timeout:g}s erreichbar ({last_error}); Log: {mp.log_path}"
    )


def start_manager_process(
    *,
    port: int | None = None,
    env_overrides: dict[str, str] | None = None,
    log_dirname: str | None = None,
    health_timeout: float = MANAGER_HEALTH_TIMEOUT,
) -> ManagerProcess:
    """Manager als uvicorn-Subprozess starten und auf `/health` warten.

    Priorität der Env: OS-Env < `.env.test` < **erzwungene Testwerte** <
    `env_overrides`.  Erzwungen werden die sicherheitsrelevanten Werte
    (eigener Port, kein mDNS, Fake-Endpunkte, **Platzhalter-Secrets**,
    Test-Hooks an) – damit die echte `.env` des Projekts den Testlauf nicht
    kontaminieren kann.  Bei einem Startfehler wird der Prozess **immer**
    wieder heruntergefahren, bevor die Ausnahme fliegt.
    """
    if port is None:
        port = int(getattr(settings, "test_manager_port", 18767) or 18767)
    test_env = load_env_file(TEST_ENV_FILE)
    forced = {
        "MANAGER_HOST": "127.0.0.1",
        "MANAGER_PORT": str(port),
        "MANAGER_MDNS_ENABLED": "false",
        "ENABLE_TEST_HOOKS": "true",
        "RUN_LIVE": "0",
        "TEST_MANAGER_PORT": str(port),
        "HA_BASE_URL": test_env.get("HA_BASE_URL", "http://127.0.0.1:9"),
        "HA_TOKEN": test_env.get("HA_TOKEN", "test-ha-token-placeholder"),
        "LLM_BASE_URL": test_env.get("LLM_BASE_URL", "http://127.0.0.1:9"),
        "LLM_API_KEY": test_env.get("LLM_API_KEY", "test-llm-key-placeholder"),
        "PYTHONUNBUFFERED": "1",
    }
    env = os.environ.copy()
    env.update(test_env)
    env.update(forced)
    if env_overrides:
        env.update(env_overrides)

    log_dir = PROJECT_ROOT / (log_dirname or MANAGER_LOG_DIRNAME)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"manager-{stamp}-{os.getpid()}-{port}.log"
    handle = open(log_path, "wb")  # noqa: SIM115 – Handle lebt im ManagerProcess.
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        handle.close()
        raise

    mp = ManagerProcess(
        process=process,
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        log_path=log_path,
        log_handle=handle,
    )
    try:
        mp.health_payload = wait_for_health(mp, timeout=health_timeout)
    except Exception:
        stop_manager_process(mp)
        raise
    return mp


def stop_manager_process(mp: ManagerProcess, timeout: float = MANAGER_STOP_TIMEOUT) -> int:
    """Harten Teardown fahren: SIGTERM, dann ggf. SIGKILL – **kein Zombie**.

    Immer `wait()` nach dem Signal, damit der Kindprozess eingesammelt wird;
    ohne das bliebe ein Zombie zurück.  Gibt den Exit-Code zurück.
    """
    process = mp.process
    if process.poll() is None:
        process.terminate()  # SIGTERM: uvicorn fährt sauber herunter (P5.T4).
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()  # SIGKILL als letzte Instanz.
            process.wait(timeout=timeout)
    handle = mp.log_handle
    if handle is not None:
        try:
            handle.flush()
            handle.close()
        except Exception:  # noqa: BLE001 – Teardown ist best-effort.
            pass
        mp.log_handle = None
    return process.returncode


@pytest.fixture(scope="session")
def manager_proc() -> Iterator[ManagerProcess]:
    """Session-weiter Manager auf `TEST_MANAGER_PORT` (echter uvicorn-Subprozess).

    Startet `python -m app.main` mit `.env.test`-Werten (Platzhalter, Fake-
    Endpunkte), wartet auf `/health` 200 und fährt am Session-Ende hart
    herunter.  Log landet unter `reports/manager-proc/`.

    **Nur für L2 (`integration`) / L5 (`live`) gedacht** – siehe Kommentar
    oben zur E36-Netzsperre.
    """
    mp = start_manager_process()
    try:
        yield mp
    finally:
        stop_manager_process(mp)
        mp.process.wait()  # bereits geschehen; defensiv gegen Zombies.


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    """Kleine temporäre WAV-Datei auf der **Speaker**-Seite (48 kHz, S16_LE).

    200 ms ⇒ 9600 Frames, 19 200 Byte PCM + 44 Byte Header.  Inhalt ist das
    ganzzahlige Dreieck aus `triangle_pcm` — bitgleich auf jeder Plattform.
    """
    return write_wav(tmp_path / "speaker-200ms-48k-s16-mono.wav")


#: Signatur der Mic-Frame-Factory: `mic_frame(seq=…, pcm=…) -> bytes`.
MicFrameFactory = Callable[..., bytes]


@pytest.fixture
def mic_frame() -> MicFrameFactory:
    """Baut **exakt** einen Mic-Frame: `[0x01][seq_hi][seq_lo]` + 2560 B PCM.

    * `seq` ist **uint16 big-endian**; gültig 0…65535.  Das Gerät zählt mit
      `var seqNum uint16; seqNum++` und **wrappt** dabei (65535 → 0), es gibt
      also keinen Fehlerfall auf der Leitung — deshalb wirft die Factory hier
      `ValueError` statt still zu überlaufen, und der Test prüft die Kante.
    * `pcm` muss exakt `CHUNK_BYTES` (2560) Byte lang sein, sonst `ValueError`:
      ein Frame mit falscher Länge ist auf der Wire ein Protokollfehler.
    * `pcm=None` erzeugt das **Dreiecksmuster ab Sample 0** — die Phase
      beginnt pro Frame neu.  Wer einen lückenlosen Strom über mehrere
      Frames braucht (P2: MicChunker), übergibt selbst ein eigenes `pcm`.
    """

    def _build(seq: int = 0, pcm: bytes | None = None) -> bytes:
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise TypeError(f"seq muss ein int sein, ist {type(seq).__name__}")
        if not 0 <= seq <= MIC_SEQ_MAX:
            raise ValueError(
                f"seq={seq} liegt außerhalb uint16 (0…{MIC_SEQ_MAX}); das Gerät "
                "wrappt bei 65535 → 0, der Aufrufer muss den Wert selbst modulo "
                f"{MIC_SEQ_MAX + 1} bilden."
            )
        payload = triangle_pcm(CHUNK_BYTES // SAMPLE_WIDTH) if pcm is None else pcm
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"pcm muss bytes sein, ist {type(payload).__name__}")
        if len(payload) != CHUNK_BYTES:
            raise ValueError(
                f"pcm hat {len(payload)} Byte, erwartet {CHUNK_BYTES} "
                f"({MIC_CHUNK_MS:g} ms @ {MIC_RATE} Hz, S16_LE mono)"
            )
        header = bytes((MIC_FRAME_TYPE,)) + struct.pack(">H", seq)
        frame = header + bytes(payload)
        # Invariante aus K2/E28 – darf nicht von einer Fixture-Variante
        # stillschweigend gerissen werden.
        assert len(frame) == MIC_FRAME_BYTES, len(frame)
        return frame

    return _build


# ── JUnit-XML (siehe Kommentar in `pytest.ini`) ─────────────────────────
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Jedem Test seinen Layer als JUnit-`<property>` mitgeben (P9.T0).

    JUnit-XML kennt Marker nicht – ohne diese Eigenschaft könnte
    `tools/make_report.py` die Layer-Matrix nicht aus dem XML bauen.  Der
    spezifischste Marker aus `LAYER_MARKER_ORDER` gewinnt; Tests ohne
    Layer-Marker erhalten `unknown` (sichtbar, nicht stillschweigend).
    """
    for item in items:
        layer = next(
            (name for name in LAYER_MARKER_ORDER if item.get_closest_marker(name)),
            "unknown",
        )
        item.user_properties.append(("layer", layer))


def pytest_configure(config: pytest.Config) -> None:
    """Setzt den JUnit-Pfad mit Zeitstempel, falls keiner angegeben wurde.

    pytest kennt keinen ini-Schalter für den JUnit-Pfad und expandiert dort
    kein `strftime` — der Zeitstempel kann also nur hier entstehen.  Ein
    `--junit-xml=…` auf der Kommandozeile (P9, `run_tests.sh`) gewinnt.
    """
    if config.getoption("xmlpath", default=None):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    directory = config.rootpath / JUNIT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    config.option.xmlpath = str(directory / f"junit-{stamp}.xml")
