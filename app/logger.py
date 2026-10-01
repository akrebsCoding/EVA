"""Logging-Grundlage des wyoming-managers (P1.T1, `PLAN.md` §7 → P1.T1).

Schema
------
Jedes Modul holt seinen Logger über :func:`get_logger`; der Name wird auf den
Namensraum ``manager.<modul>`` normalisiert:

======================================  ==========================
Aufruf                                  Logger-Name
======================================  ==========================
``get_logger("pipeline")``              ``manager.pipeline``
``get_logger("manager.pipeline")``      ``manager.pipeline`` (kein Doppelpräfix)
``get_logger("app.ws_server")``         ``manager.ws_server``
``get_logger("manager")``               ``manager``
======================================  ==========================

Die-normalisierung ist absichtlich tolerant, weil spätere Module (P2–P5) den
bequemsten Aufruf wählen (``get_logger("pipeline")`` oder
``get_logger(__name__)``) – beide ergeben denselben Namen.

Level
-----
Der Level kommt aus ``settings.log_level`` (PLAN §4, Default ``INFO``), wird
**case-insensitiv** aufgelöst und ist über :func:`configure_logging` (Parameter
``level``) überschreibbar. Ein unbrauchbarer Level fällt auf ``INFO``
zurück und erzeugt **genau eine** Warnung pro Prozess – nie einen Abbruch:
Ein Tippfehler in ``LOG_LEVEL`` darf den Manager nicht startunfähig machen.

Ausgabe
-------
Ein **einziger** ``StreamHandler`` auf den ``manager``-Logger, Ausgabe nach
``stdout``, Format ``%(asctime)s %(levelname)s %(name)s: %(message)s``
(PLAN §7/Tabellen-Konvention, einzeilig, `journalctl`-tauglich).

Bewusste Entscheidungen:

* **Kein** ``dictConfig``, **keine** File-Handler, **keine** Rotation – der
  Container sammelt `stdout` (P6), der Manager rotiert nichts selbst.
* **Kein Handler auf dem Root-Logger.** uvicorn konfiguriert `root` selbst
  (`disable_existing_loggers`); ein zweiter Handler auf `root` ergäbe doppelte
  Zeilen. `manager` bekommt `propagate = False`, damit jede Zeile genau einmal
  und im eigenen Format erscheint – Loggers *fremder* Pakete (uvicorn,
  websockets) bleiben unberührt.
*   **Idempotent.** `configure_logging()` darf beliebig oft laufen: der Handler
  wird nur beim ersten Mal angehängt, der Level wird bei jedem Aufruf aktualisiert. Der
  Import dieses Moduls richtet **keinen** Handler ein (kein Side-Effect) – das
  passiert lazy beim ersten `get_logger()` bzw. beim Startpfad P5.T4
  (`app/main.py` → `configure_logging()`).
* **Ohne ``pydantic`` lauffähig.** Fehlt `app.config` (System-Python ohne
  `pydantic-settings`), wird `INFO` verwendet, statt den Import zu sprengen –
  der Logger ist die unterste Schicht und darf nicht vom Config-Paket abhängen.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

try:  # Package-Import (`from app.logger import get_logger`).
    from .config import LOG_LEVELS as _CONFIG_LOG_LEVELS
    from .config import settings as _settings
except ImportError:  # pragma: no cover - degradiert auf INFO statt Exception.
    _CONFIG_LOG_LEVELS = None
    _settings = None

#: Namensraum aller Manager-Logger (PLAN §7 P1.T1: Schema `manager.<modul>`).
LOGGER_NAMESPACE: Final[str] = "manager"
#: Einheitliches, einzeiliges Format für Container-Logs (`journalctl`).
LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: Level laut PLAN §4 (`LOG_LEVEL=INFO`).
DEFAULT_LOG_LEVEL: Final[str] = "INFO"
#: Präfixe, die `get_logger` als „Root des Managers" erkennt und **einmal**
#: entfernt – verhindert `manager.manager.x` bzw. `manager.app.x`.
_MANAGER_ROOTS: Final[frozenset[str]] = frozenset({LOGGER_NAMESPACE, "app"})
#: Gültige Levelnamen – aus `app.config.LOG_LEVELS` (P1.T0), mit gleichem
#: Fallback, falls `app.config` nicht importierbar ist.
LOG_LEVEL_NAMES: Final[tuple[str, ...]] = tuple(
    _CONFIG_LOG_LEVELS or ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")
)

#: Warnung „ungültiges Log-Level" genau einmal pro Prozess (nicht pro Modul-Import).
_warned_invalid_level: bool = False
#: Handler bereits angehängt? ⇒ `configure_logging()` ist idempotent.
_handler_installed: bool = False


def _fallback_to_info(raw: object) -> int:
    """`INFO` als Fallback + **einmalige** Warnung (kein Abbruch, kein Spam)."""
    global _warned_invalid_level
    if not _warned_invalid_level:
        _warned_invalid_level = True
        logging.getLogger(LOGGER_NAMESPACE).warning(
            "Ungültiges Log-Level %r → Fallback auf %s (gültig: %s).",
            raw,
            DEFAULT_LOG_LEVEL,
            ", ".join(LOG_LEVEL_NAMES),
        )
    return logging.INFO


def resolve_level(level: str | int | None) -> int:
    """Levelnamen/-nummern in ein numerisches `logging`-Level übersetzen.

    * `None` ⇒ `INFO` (kein Wert konfiguriert).
    * `str` ⇒ case-insensitiv (`"debug"` → `DEBUG`); unbekannt ⇒ `INFO` + Warnung.
    * `int` ⇒ unverändert durchgereicht (bereits numerisch).
    * Alles andere ⇒ `INFO` + Warnung.
    """
    if level is None:
        return logging.INFO
    if isinstance(level, str):
        candidate = level.strip().upper()
        if candidate in LOG_LEVEL_NAMES:
            return getattr(logging, candidate)
        return _fallback_to_info(level)
    if isinstance(level, int) and not isinstance(level, bool):
        return level
    return _fallback_to_info(level)


def qualify_name(name: str) -> str:
    """`name` auf `manager.<modul>` normalisieren.

    Entfernt ein führendes `manager.`- oder `app.`-Präfix **einmal** und
    stellt das `manager.`-Präfix voran; leerer Name ist ein Programmierfehler.
    """
    if not isinstance(name, str):
        raise TypeError(f"Logger-Name muss ein str sein, ist {type(name).__name__}")
    parts = [part for part in name.strip().strip(".").split(".") if part]
    if not parts:
        raise ValueError("Logger-Name darf nicht leer sein (erwartet 'pipeline')")
    if parts[0] in _MANAGER_ROOTS:
        parts = parts[1:]
    if not parts:
        return LOGGER_NAMESPACE
    return f"{LOGGER_NAMESPACE}.{'.'.join(parts)}"


def _settings_log_level() -> str | None:
    """`LOG_LEVEL` aus den Settings, oder `None` ohne `app.config`."""
    return getattr(_settings, "log_level", None)


def configure_logging(level: str | int | None = None) -> logging.Logger:
    """Logging-Grundkonfiguration setzen – **idempotent** aufrufbar.

    Legt beim ersten Aufruf den einen `stdout`-Handler an und setzt danach nur
    noch den Level. `level=None` ⇒ Wert aus `settings.log_level`
    (PLAN §4, Default `INFO`). Rückgabe: der `manager`-Logger, damit Aufrufer
    den effektiven Level prüfen können. Startpfad: P5.T4 (`app/main.py`).
    """
    global _handler_installed
    manager_logger = logging.getLogger(LOGGER_NAMESPACE)
    if not _handler_installed:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.set_name("manager-stdout")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        manager_logger.addHandler(handler)
        # Doppelte Zeilen vermeiden, falls `root` (z. B. durch uvicorn) Handler
        # hat – und fremde Logger nicht mit unserem Format überziehen.
        manager_logger.propagate = False
        _handler_installed = True
    resolved = resolve_level(_settings_log_level() if level is None else level)
    manager_logger.setLevel(resolved)
    return manager_logger


def get_logger(name: str) -> logging.Logger:
    """Logger im Schema `manager.<modul>` – Einstieg für alle Manager-Module.

    Konfiguriert beim ersten Aufruf das Logging (lazy, kein Import-Side-Effect)
    und liefert danach den fertigen Logger, z. B. `manager.pipeline`.
    """
    if not _handler_installed:
        configure_logging()
    return logging.getLogger(qualify_name(name))
