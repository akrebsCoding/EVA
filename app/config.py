"""Settings des wyoming-managers – verbindliche Variablenliste aus `PLAN.md` §4.

Feldnamen, Blockreihenfolge und Defaults folgen 1:1 der `.env`-Vorlage
(`.env.example`, angelegt in P0.T8).  Abweichungen von `PLAN.md` §4 sind
ausschließlich die bereits entschiedenen Korrekturen:

* **E17** – zusätzlich `HA_ALLOWED_ENTITIES` (statische Entity-Allowlist,
  kommagetrennt, Default leer = **kein** Vorfilter) neben `HA_ENTITY_DOMAINS`.
* **E23** – `OWW_BARGE_IN_THRESHOLD=0.15` (verifizierter Ist-Wert des
  Alt-Setups), **nicht** 0.6 wie in §2.4/§4 behauptet.
* **E27** – `WS_PING_INTERVAL=20` / `WS_PING_TIMEOUT=30` bleiben wie in §4
  (die Referenz nutzt auf WS-Ebene 20/10 plus App-JSON-Ping 5 s; die endgültige
  Entscheidung fällt in P5.T1) – hier wird daran nichts geändert.
* **E96 (P9.T1)** – `OWW_THRESHOLD` **0,9 → 0,80** (datenbasiert, siehe das
  Feld) sowie **zwei neue** Variablen: `WAKE_ATTEMPT_KEEP_FLOOR=0.2` (was im
  Wake-Messring steht) und `DASHBOARD_WRITE_ENABLED=false` (Schreibmodus des
  Dashboards, **Default aus**).  An bestehenden Feldern wurde **nur** der eine
  Schwellwert-Default geändert.
* **E113 (P12.T2)** – `HA_ENTITY_DOMAINS` von **7 auf 16** Domains erweitert
  (14 sichere + `update` + `automation`, s. das Feld).  Die
  Sicherheits-Domains (`lock`/`alarm_control_panel`/`vacuum`) bleiben
  **aus** und stehen hart in `HA_BLOCKED_DOMAINS` (`app/ha_client.py`) – sie
  sind **kein** Env-Key, damit die `.env` sie nicht freischalten kann.

Design-Entscheidungen (bewusst und dokumentiert):

* **Kein Env-Prefix.**  Die `.env` benutzt die Klartext-Keys aus §4, deshalb
  heißen die Felder `manager_port` und nicht `wyoming_manager_port`.
* **`env_file` absolut** (Projektwurzel, nicht CWD-relativ), damit die Werte
  unabhängig vom Arbeitsverzeichnis gefunden werden – uvicorn wird nicht
  zwingend aus der Projektwurzel gestartet.
* **`extra="ignore"`** – die echte `.env` darf später weitere Keys enthalten
  (P9: `.env.test`, Test-Hooks), ohne dass der Import bricht.
* **`env_ignore_empty=True`** – ein leerer Platzhalter (`HA_TOKEN=`) im `.env`
  ist „nicht gesetzt" und verdrängt den Default nicht mit einem Leerstring.
* **Pflicht-Secrets ohne harte Pflicht.**  `HA_TOKEN` und `LLM_API_KEY` haben
  Default `""`, damit `from app.config import settings` **grün bleibt**, solange
  die Secrets fehlen (E18/E20: der User legt sie später in `.env` ab).  Statt
  eines Import-Crash gibt es eine Warnung; `require_secrets()` erzwingt die
  Prüfung explizit zum Startzeitpunkt (P5.T4).
* **CSV-Variablen als `str`.**  `HA_ENTITY_DOMAINS` / `HA_ALLOWED_ENTITIES`
  kommen kommagetrennt; ein `list`-Feld würde von pydantic-settings als JSON
  geparst und bei `light,switch` einen `SettingsError` werfen.  Die Aufteilung
  passiert deshalb in den Properties `entity_domains` / `allowed_entities`.

Dieses Modul enthält bewusst **keine** Logik aus P2–P5 (kein Pipeline-Zustand,
kein Protokoll, keine Client-Logik) – nur Typisierung, Defaults und Plausibilitäts-
prüfungen der Konfiguration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Projektwurzel – eine Ebene über `app/`.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
#: Secret-Datei (git-ignoriert, `chmod 600`, P6.T3).
ENV_FILE: Final[Path] = PROJECT_ROOT / ".env"

#: Pflicht-Secrets als `(ENV-KEY, Feldname)` – vom User zu liefern (E18/E20).
REQUIRED_SECRETS: Final[tuple[tuple[str, str], ...]] = (
    ("HA_TOKEN", "ha_token"),
    ("LLM_API_KEY", "llm_api_key"),
)

#: Erlaubte Werte für `JEV_MODE` (E1: A/B/C).
JEV_MODES: Final[tuple[str, ...]] = ("intent", "gate", "off")
#: Erlaubte Werte für `ROUTER_VARIANT` (E90): ``class`` = Variante A (Default,
#: keine Verhaltensänderung) | ``entity`` = Variante D (2-stufiges Jev).
ROUTER_VARIANTS: Final[tuple[str, ...]] = ("class", "entity")
#: Erlaubte Werte für `LOG_LEVEL`.
LOG_LEVELS: Final[tuple[str, ...]] = (
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
    "NOTSET",
)

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


def _split_csv(raw: str) -> tuple[str, ...]:
    """Kommagetrennte Env-Variable in ein bereinigtes Tuple zerlegen."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class Settings(BaseSettings):
    """Alle Variablen aus `PLAN.md` §4, typsicher und mit Defaults."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # ── Manager / WebSocket ───────────────────────────────────────────
    manager_host: str = "0.0.0.0"
    manager_port: int = Field(default=8767, ge=1, le=65535)
    manager_mdns_name: str = "echomuse"
    manager_mdns_enabled: bool = True
    #: E27: §4 = 20; Referenz nutzt 20 s Intervall / 10 s Timeout (P5.T1 klärt).
    ws_ping_interval: int = Field(default=20, ge=1)
    ws_ping_timeout: int = Field(default=30, ge=1)

    # ── Wake-Word (openWakeWord, manager-seitig; E5) ──────────────────
    oww_enabled: bool = True
    oww_model: str = "hey_jarvis_v0.1"
    #: **E96 (P9.T1) – datenbasiert 0,9 → 0,80.**  Auswertung der P9.T0-Messung
    #: (4 angenommene Versuche 0,922/0,955/0,988/0,977, **9** `below_threshold`
    #: mit 0,526–0,887, der höchste also **0,013** unter der alten Schwelle):
    #: das „manchmal muss ich es zweimal sagen" ist ein **Grenzfall an der
    #: Schwelle**, kein Rauschproblem.  Von den neun fängt 0,80 **acht** ab
    #: (0,800–0,887) – die **0,526 bleibt korrekt abgelehnt** (Abstand 0,27 zur
    #: Schwelle, das ist kein Grenzfall mehr).  ⚠️ `.env` auf `.123`
    #: setzt `OWW_THRESHOLD` **explizit** ⇒ ein env-Wert **gewinnt** gegen
    #: diesen Default; beide Wege wurden gemeinsam auf 0,80 gesetzt.
    oww_threshold: float = Field(default=0.80, gt=0.0, le=1.0)
    oww_barge_in_enabled: bool = True
    #: E23: Ist-Wert des Alt-Setups = 0.15 (PLAN §2.4/§4 nennen 0.6).
    oww_barge_in_threshold: float = Field(default=0.15, gt=0.0, le=1.0)
    #: → `OWWModel(enable_speex_noise_suppression=…)` (Konstruktor-Bool).
    oww_speex_ns: bool = True
    #: 2560 B = 80 ms @ 16 kHz S16_LE mono (K2/E28).
    oww_chunk_bytes: int = Field(default=2560, ge=2)
    #: Treffer nach `model.reset()` verwerfen (openWakeWord seedet das Fenster).
    oww_warmup_chunks: int = Field(default=16, ge=0)
    #: 3 Chunks = 240 ms Audio-Anfang verwerfen (E7, nur Wake-Pfad).
    oww_preroll_discard_chunks: int = Field(default=3, ge=0)
    oww_cooldown_ms: int = Field(default=1000, ge=0)
    #: CPU-Regler (PLAN §9: OWW-CPU-Last).
    oww_score_every_n_chunks: int = Field(default=1, ge=1)
    #: **E96 (P9.T1)** – Beobachtbarkeits-Boden für den Wake-Messring: Versuche
    #: mit `score <= wake_attempt_keep_floor` landen **nicht** im Ring, werden
    #: aber **weiterhin gezählt** (`WakeWordDetector.attempt_summary`).  0,2 ist
    #: bewusst tief unter den interessanten Scores (0,5–0,9) und **über** dem
    #: Rauschteppich des P9.T0-Laufs (Stille = 0,0): der Ring war live mit 50×
    #: `below_threshold` score 0,0 überschrieben und damit nutzlos, während
    #: genau die interessanten Versuche (0,5–0,9) verrutscht wurden.  Der
    #: Log-Boden (`ATTEMPT_LOG_FLOOR_RATIO = 0.5 · Schwelle = 0,40`) ist davon
    #: **unabhängig** und bleibt unberührt.
    wake_attempt_keep_floor: float = Field(default=0.2, ge=0.0, lt=1.0)

    # ── Turn-Steuerung ───────────────────────────────────────────────
    #: Sicherheitsnetz (K5) – das Gerät meldet das Turn-Ende (0x04/0x05).
    turn_hard_cap_seconds: float = Field(default=15.0, gt=0.0)
    turn_no_speech_seconds: float = Field(default=8.0, gt=0.0)
    vad_device_sided: bool = True
    #: Button-Pfad: Beamformer aufs Sprechermikro (K6).
    mic_start_lock_mic_button: bool = True

    # ── STT – Whisper (Wyoming, im selben Compose) ───────────────────
    whisper_host: str = "127.0.0.1"
    whisper_port: int = Field(default=10300, ge=1, le=65535)
    whisper_language: str = "de"
    #: E2: `base` int8 | `small` | `tiny`.
    whisper_model: str = "base"

    # ── TTS – Piper (Wyoming, im selben Compose) ─────────────────────
    piper_host: str = "127.0.0.1"
    piper_port: int = Field(default=10200, ge=1, le=65535)
    piper_voice: str = "de_DE-thorsten-high"

    # ── Audio ─────────────────────────────────────────────────────────
    audio_mic_rate: int = Field(default=16000, gt=0)
    audio_speaker_rate: int = Field(default=48000, gt=0)
    #: Bytes pro Sample (2 = S16_LE).
    audio_width: int = Field(default=2, ge=1, le=4)
    audio_channels: int = Field(default=1, ge=1, le=2)
    speaker_chunk_bytes: int = Field(default=4096, ge=2)

    # ── Home Assistant ───────────────────────────────────────────────
    ha_base_url: str = "http://10.0.0.10:8123"
    #: **Pflicht-Secret** (E18), Default leer ⇒ Import bleibt grün.
    ha_token: str = ""
    #: **E113 (P12.T2)** – Domain-Scope des Entity-Caches **maximal innerhalb
    #: der Sicherheitsgrenzen**: die **14 sicheren** Domains aus
    #: `deploy/docs/P12_DESIGN.md` A-1 (`light,switch,cover,climate,
    #: media_player,scene,script,button,number,select,input_boolean,
    #: input_select,input_number,fan`) **plus `update` und `automation`**
    #: (User-Entscheidung 2026-09-30) = **16** Domains.
    #:
    #: Live-Beleg (2026-09-30, `GET /api/states` ∩ `GET /api/services` gegen
    #: `.123:8123`, HA 2026.8.2): **209 → 375** gecachte Entities
    #: (neu: `button` 45, `number` 23, `select` 13, `input_boolean` 4,
    #: `input_select` 3, `input_number` 1, `fan` 3, `update` 27,
    #: `automation` 47 = **+166**).
    #:
    #: **Zwei Dinge sind hier bewusst *nicht* konfigurierbar:**
    #:
    #: * **Sicherheits-/Hochrisiko-Domains** (`lock`, `alarm_control_panel`,
    #:   `vacuum`) – die stehen **hart** in `HA_BLOCKED_DOMAINS`
    #:   (`app/ha_client.py`) und sind **kein** Env-Key: ein Config-Schlüssel,
    #:   der die Sicherheitsgrenze aushebeln kann, ist schlechter als ein
    #:   Hardcode.  Ein Eintrag in dieser Liste kann sie **nicht** freischalten –
    #:   **Block schlägt immer Erlaubnis**.
    #: * **Read-only-Domains** (`sensor`, `binary_sensor`, `device_tracker`,
    #:   `event`, `sun`, `person`, `zone`, `weather`, `image`, `conversation`) –
    #:   nur die steuerfähigen `update`/`automation`-Entities sind hier gemeint;
    #:   die **Sensor**-Entities bleiben draußen (sie sind kein Schaltziel).
    #:   Der Lese-Pfad (`sensor`-Fragen) ist ein eigener Schritt (P12.T3).
    ha_entity_domains: str = (
        "light,switch,cover,climate,media_player,scene,script,"
        "button,number,select,input_boolean,input_select,input_number,fan,"
        "update,automation"
    )
    ha_cache_interval_seconds: int = Field(default=600, ge=0)
    ha_request_timeout: float = Field(default=5.0, gt=0.0)
    #: E17: statische Entity-Allowlist, kommagetrennt, **leer = kein Vorfilter**
    #: (Area-/Label-Filter ist per HA-REST nicht umsetzbar, P0.T1).
    ha_allowed_entities: str = ""

    # ── LLM-Gateway: opencode.ai (ersetzt JEV_*/DEEPSEEK_* aus v4 §5.1) ─
    llm_base_url: str = "https://opencode.ai/zen/v1"
    #: **Pflicht-Secret** (E20), Default leer ⇒ Import bleibt grün.
    llm_api_key: str = ""
    #: E1: A=`intent` | B=`gate` | C=`off`.
    jev_mode: str = "intent"
    jev_model: str = "jev-1.13"
    jev_timeout: float = Field(default=8.0, gt=0.0)
    deepseek_model: str = "deepseek-v4.1-flash"
    deepseek_base_url: str = "https://opencode.ai/zen/v1/chat/completions"
    deepseek_timeout: float = Field(default=12.0, gt=0.0)
    deepseek_system_prompt: str = (
        "Du bist ein hilfreicher Assistent. Antworte kurz und präzise, auf Deutsch."
    )
    #: E3: unterhalb ⇒ COMMAND wird abgelehnt (kein Fehlalarm).
    router_confidence_gate: float = Field(default=0.75, ge=0.0, le=1.0)
    #: E90: Router-Variante. ``class`` = A (Default, Jev `intent`+`target_class`
    #: + DeepSeek), ``entity`` = D (2-stufiges Jev: #1 COMMAND/TEXT, #2
    #: Entity+Service `choice`).  **Default `class`** ⇒ keine Verhaltensänderung
    #: ohne explizites Umstellen (Rollback = eine Zeile zurück auf `class`).
    router_variant: str = "class"
    #: E92: Auslöse-Schwelle für ``needs_param`` in Jev #2 (Variante D).  Dieselbe
    #: Logik wie beim Intent-Gate (``NoulResult.value`` = ``score >= 0.5``, E58),
    #: aber **konfigurierbar**: „Muss ein Zahlenwert angegeben werden?" ist eine
    #: ``noul``-Frage, 0,5 ist ihre dokumentierte Ja/Nein-Grenze.  Höher ⇒ Zahlen
    #: werden still verworfen (die v1-Krankheit), niedriger ⇒ unnötige
    #: DeepSeek-Aufrufe auf parameterlosen Befehlen.
    router_needs_param_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # ── Logging ───────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Dashboard (P8/P9) ──────────────────────────────────────────────
    #: **E96 (P9.T1) – Schreibmodus des Dashboards, Default AUS.**  Solange
    #: `false`, ist `POST /api/config/oww-threshold` **nicht** wirksam (403) und
    #: das Frontend blendet die Schreib-UI aus.  Auch `true` erlaubt **genau
    #: eine** schreibbare Einstellung (`oww_threshold`), **keinen** HA-Call und
    #: **kein** Geräteschalten – das Dashboard schaltet nie etwas.
    #: ⚠️ Das Projekt hat **keine** Auth/CSRF-Schicht; der Schreibmodus gehört
    #: deshalb **nur** ins vertrauenswürdige LAN und wird nach dem Tuning wieder
    #: abgeschaltet (E96-Restrisiko, STATE.md §3/§4).
    dashboard_write_enabled: bool = False
    #: **E110 (P11.T1) – optionale Auth-Schicht für `POST /api/config/*`.**
    #: Default **leer = offen** (backward-kompatibel zu E96/P9.T1, ohne den Wert
    #: ändert sich nichts).  Gesetzt ⇒ die Schreibroute verlangt
    #: `Authorization: Bearer <token>` (exakt, konstantenzeitig verglichen);
    #: fehlend/falsch ⇒ **401**.  Die Schicht liegt **vor** `dashboard_write_enabled`
    #: (401 vor 403) und gilt **nur** für `POST /api/config/*` – `GET`-Routen und
    #: `/health` bleiben ungeschützt.  Der Token ist ein Secret: er gehört in die
    #: `.env`, nie ins Git, und wird nie im Log/der Antwort ausgegeben.
    dashboard_api_token: str = ""

    # ── Audio-Diagnose-Dump (P9.T5, Default AUS) ──────────────────────
    #: **E100 (P9.T5) – Diagnostik, kein Verhaltens-Tuning.**  Wenn `true`,
    #: legt der Manager pro Turn das komplette STT-Audio (16 kHz S16_LE mono)
    #: als `.wav` plus eine kleine `.json`-Datei (Endpoint-Stats + Transcript)
    #: in `audio_dump_dir` ab – für die Offline-Analyse der VAD-Distanz-
    #: Hypothese („3 m sehr schlecht, langsam reden hilft").  **Default AUS** =
    #: null I/O, null Overhead (gleiche Linie wie `DASHBOARD_WRITE_ENABLED`,
    #: P9.T1/E96).  ⚠️ Die Dumps enthalten **rohe Sprache** des Nutzers: sie
    #: bleiben auf dem Zielhost, werden nicht exportiert, und das Verzeichnis
    #: kann jederzeit geleert werden.
    audio_dump_enabled: bool = False
    #: Zielverzeichnis der Dumps.  Leer ⇒ kein Dump, selbst wenn `enabled`.
    #: Der Manager-Container hat **keinen** Bind-Mount (P9.T5 verifiziert),
    #: daher ist ein Container-lokales Verzeichnis (`/tmp/audio-dumps`)
    #: bewusst akzeptiert – es geht beim Recreate verloren und ist für die
    #: Diagnostik ausreichend (STATE.md §3).
    audio_dump_dir: str = ""
    #: Rotierendes Limit: älteste Dumps werden zuerst gelöscht.
    audio_dump_max_files: int = Field(default=50, ge=1)
    #: Rotierendes Limit in Bytes (Default 100 MB), älteste zuerst.
    audio_dump_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)

    # ── Test-Hooks (nur Testbetrieb; auf .123 IMMER false) ────────────
    #: `true` nur für L5a; `/internal/*` existiert sonst nicht (P5.T4/P9.T7).
    enable_test_hooks: bool = False
    #: 1 = L5a/L5b auf .123 freischalten.
    run_live: int = Field(default=0, ge=0, le=1)
    #: Manager-Port für lokale Testläufe (L0–L4).
    test_manager_port: int = Field(default=18767, ge=1, le=65535)

    # ── Validatoren (nur Plausibilität, keine Fachlogik) ──────────────
    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized and normalized not in LOG_LEVELS:
                raise ValueError(
                    f"LOG_LEVEL muss eines von {LOG_LEVELS} sein, ist {value!r}"
                )
            return normalized or value
        return value

    @field_validator("jev_mode", mode="before")
    @classmethod
    def _normalize_jev_mode(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized and normalized not in JEV_MODES:
                raise ValueError(
                    f"JEV_MODE muss eines von {JEV_MODES} sein (E1), ist {value!r}"
                )
            return normalized or value
        return value

    @field_validator("router_variant", mode="before")
    @classmethod
    def _normalize_router_variant(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized and normalized not in ROUTER_VARIANTS:
                raise ValueError(
                    f"ROUTER_VARIANT muss eines von {ROUTER_VARIANTS} sein (E90), "
                    f"ist {value!r}"
                )
            return normalized or value
        return value

    @field_validator("whisper_language", "whisper_model", "piper_voice", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("oww_chunk_bytes", "speaker_chunk_bytes")
    @classmethod
    def _must_be_even(cls, value: int, info: Any) -> int:
        # S16_LE ⇒ ganzzahlige Sample-Anzahl pro Chunk.
        if value % 2 != 0:
            raise ValueError(
                f"{info.field_name}={value} muss gerade sein (S16_LE, 2 Byte/Frame)"
            )
        return value

    @field_validator("ha_base_url", "llm_base_url", "deepseek_base_url", mode="before")
    @classmethod
    def _strip_url(cls, value: object) -> object:
        return value.strip().rstrip("/") if isinstance(value, str) else value

    @model_validator(mode="after")
    def _warn_about_missing_secrets(self) -> Settings:
        """Pflicht-Secrets melden, aber **nicht** beim Import sterben (E18/E20)."""
        missing = self.missing_secrets
        if missing:
            _LOG.warning(
                "Pflicht-Variable(n) %s fehlen (leer). Der Manager startet, die "
                "betroffenen Clients scheitern zur Laufzeit. Werte gehören in %s.",
                ", ".join(missing),
                ENV_FILE,
            )
        return self

    # ── Abgeleitete Helfer (Properties, keine Felder) ────────────────
    @property
    def entity_domains(self) -> tuple[str, ...]:
        """`HA_ENTITY_DOMAINS` als Tuple (P4.T0: Domain-Filter im Entity-Cache)."""
        return _split_csv(self.ha_entity_domains)

    @property
    def allowed_entities(self) -> tuple[str, ...]:
        """`HA_ALLOWED_ENTITIES` als Tuple – **leer = kein Vorfilter** (E17)."""
        return _split_csv(self.ha_allowed_entities)

    @property
    def missing_secrets(self) -> tuple[str, ...]:
        """ENV-Keys der Pflicht-Secrets, die leer sind."""
        return tuple(
            key for key, attr in REQUIRED_SECRETS if not getattr(self, attr).strip()
        )

    @property
    def has_secrets(self) -> bool:
        """True, wenn beide Pflicht-Secrets gesetzt sind."""
        return not self.missing_secrets

    def require_secrets(self) -> None:
        """Pflicht-Secrets erzwingen – für den Startpfad (P5.T4), nicht den Import."""
        if self.missing_secrets:
            raise RuntimeError(
                "Fehlende Pflicht-Variable(n) in "
                f"{ENV_FILE}: {', '.join(self.missing_secrets)} (E18/E20)"
            )

    @property
    def debug(self) -> bool:
        """True bei `LOG_LEVEL=DEBUG`."""
        return self.log_level == "DEBUG"

    @property
    def log_level_number(self) -> int:
        """Numerisches Log-Level für `logging` (P1.T1 nutzt das)."""
        return getattr(logging, self.log_level, logging.INFO)

    @property
    def test_mode(self) -> bool:
        """True, wenn Test-Hooks aktiv sind oder Live-Läufe freigeschaltet sind."""
        return self.enable_test_hooks or self.run_live == 1


#: Modulweiter Singleton – `from app.config import settings`.
settings: Final[Settings] = Settings()
