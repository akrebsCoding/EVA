"""Geräte-Konfiguration – K4-Minimaldict, effektiver Push und `listeningAnim` (P2.T6).

Reine L0-Logik: **kein Netz, keine I/O**, nur stdlib, `app.config` und
`app.protocol`.  Der `{"type":"config", …}`-Push wird über den Serializer aus
`app/protocol.py` gebaut (keine zweite JSON-Bauweise).

Grundlage und Belege:

* **`docs/device-config-reference.json`** (P0.T5) sind die **43 effektiven
  Felder wörtlich** – Original-Keys, Original-Werte, Original-Reihenfolge.
  Sie sind hier als `REFERENCE_CONFIG` hinterlegt, damit das Modul L0-pure
  bleibt (kein Datei-I/O zur Laufzeit); `tests/test_device_config.py` prüft
  **programmatisch** Key-für-Key gegen die JSON-Datei, damit die Kopie nicht
  still auseinanderläuft.
* **K4** (`PLAN.md` §2.5, `STATE.md` §3/P0.T5) nennt ~20 „relevante" Felder;
  real sind es **25** – `K4_FIELDS` (STATE.md:192: 2 Pflicht-Overrides + 13
  einzeln benannte + 3 `led*` + `duckDb` + 2 `eq*` + 3 `limiter*` + `agcEnabled`).
* **E24** – `owwOnDevice` ist in der DB der **String `"off"`**, K4 verlangt
  **Boolean `false`**.  Der Push erzwingt `False`; P7.T2 prüft die Geräteakzeptanz.
* **E25** – effektive Config = **43 Felder**; der Push hat mit `type` **44**
  Top-Level-Felder.
* **E11 / K7** – kein LED-Cue beim Wake; `listeningAnim` ist ein **separater,
  schlanker** `{"type":"config","listeningAnim":{…}}`-Push **nach** dem
  Config-Push (Spec §3, `em_controller.py:3331-3334`), **nicht** Teil der 43.
* **E26** – `owwThreshold` ist ein Gerätefeld; die **device-ignorierten**
  Felder (`DEVICE_IGNORED_FIELDS`, 15) und die **controller-only** Felder
  (`CONTROLLER_ONLY_FIELDS`, manager-seitig) werden hier getrennt geführt.

Wichtig zur Rollenverteilung:

* `build_k4_config()` liefert den **minimalen** K4-Dict (genau 25 Keys).
* `build_effective_config()` liefert die **vollständige** effektive Config
  (genau 43 Keys, identisch zum Referenzsatz) mit Manager-Autorität aus
  `app.config.settings` und den beiden erzwungenen Overrides.
* `build_config_push()` = `app.protocol.serialize_config(effective)` ⇒ **44**.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from app.config import Settings, settings as default_settings
from app.protocol import serialize_config, serialize_listening_anim

__all__ = [
    "DeviceConfigError",
    "REFERENCE_CONFIG",
    "REFERENCE_FIELDS",
    "K4_FIELDS",
    "CONTROLLER_ONLY_FIELDS",
    "DEVICE_IGNORED_FIELDS",
    "FORCED_OVERRIDES",
    "EFFECTIVE_FIELD_COUNT",
    "CONFIG_PUSH_FIELD_COUNT",
    "build_effective_config",
    "build_k4_config",
    "build_config_push",
    "build_listening_anim",
    "build_listening_anim_push",
]


class DeviceConfigError(ValueError):
    """Verletzung der Geräte-Config-Regeln (definiert statt still zu degradieren)."""


#: Die 43 effektiven Felder wörtlich aus `docs/device-config-reference.json`
#: (P0.T5, Reihenfolge und Werte unverändert).  `tests/test_device_config.py`
#: vergleicht diese Kopie programmatisch gegen die JSON-Datei.
REFERENCE_CONFIG: Final[dict[str, Any]] = {
    "owwOnDevice": "off",
    "adcDigitalGain": 88,
    "adcMicpga": 40,
    "micGainDb": 24,
    "aecEnabled": True,
    "aecDelayMs": 0,
    "aecTailMs": 300,
    "aecRefSource": "auto",
    "startupVolume": 111,
    "vadThreshold": 0.001,
    "vadSpeechMs": 32,
    "vadSilenceMs": 900,
    "buttonSingleTapEvent": False,
    "buttonMultiTapMs": 0,
    "owwThreshold": 0.9,
    "bargeInEnabled": True,
    "bargeInThreshold": 0.15,
    "duckDb": -18,
    "owwModel": "hey_jarvis_v0.1",
    "wakeArbitrationMs": 700,
    "owwSpeexNs": True,
    "nsAsr": False,
    "saveUtterances": False,
    "bleProxyEnabled": False,
    "beamformingEnabled": True,
    "beamAngle": -1,
    "eqBands": [0, 0, 0, 0, 0, 0, 0, 0],
    "eqLoudness": False,
    "bassGuardEnabled": True,
    "bassGuardDb": -30,
    "limiterEnabled": True,
    "limiterThreshold": -1,
    "limiterRelease": 150,
    "ledScene": "malevolent",
    "ledListenColor": "#00b400",
    "ledThinkColor": "#00c800",
    "meterAttack": 0.6,
    "meterDecay": 0.3,
    "meterFloor": 0.06,
    "meterGamma": 2.2,
    "meterRef": 0.22,
    "meterCurve": 0.7,
    "agcEnabled": True,
}

#: Feldnamen in Referenz-Reihenfolge (nur die Namen, 43).
REFERENCE_FIELDS: Final[tuple[str, ...]] = tuple(REFERENCE_CONFIG)

#: Die 25 K4-Felder (`STATE.md:192`, verifiziert in P0.T5) – exakt diese,
#: kein Fremdfeld.  Reihenfolge folgt der Referenz, nicht der K4-Aufzählung.
K4_FIELDS: Final[tuple[str, ...]] = (
    "owwOnDevice",
    "adcDigitalGain",
    "adcMicpga",
    "micGainDb",
    "aecEnabled",
    "aecDelayMs",
    "aecTailMs",
    "aecRefSource",
    "startupVolume",
    "vadThreshold",
    "vadSpeechMs",
    "vadSilenceMs",
    "duckDb",
    "bleProxyEnabled",
    "beamformingEnabled",
    "beamAngle",
    "eqBands",
    "eqLoudness",
    "limiterEnabled",
    "limiterThreshold",
    "limiterRelease",
    "ledScene",
    "ledListenColor",
    "ledThinkColor",
    "agcEnabled",
)

#: Felder, die **ausschließlich** der Manager auswertet (STATE.md:194/195) –
#: sie sind bewusst **nicht** Teil von `K4_FIELDS`.
CONTROLLER_ONLY_FIELDS: Final[tuple[str, ...]] = (
    "owwModel",
    "owwThreshold",
    "owwSpeexNs",
    "bargeInEnabled",
    "bargeInThreshold",
    "buttonSingleTapEvent",
)

#: Geräteseitig ignorierte Felder (E26/Spec, 15) – im Push vorhanden, aber
#: wirkungslos für das Gerät.  Bewusst **getrennt** von `CONTROLLER_ONLY_FIELDS`.
DEVICE_IGNORED_FIELDS: Final[tuple[str, ...]] = (
    "buttonMultiTapMs",
    "buttonSingleTapEvent",
    "ledListenColor",
    "ledScene",
    "ledThinkColor",
    "nsAsr",
    "owwSpeexNs",
    "saveUtterances",
    "wakeArbitrationMs",
    "meterAttack",
    "meterCurve",
    "meterDecay",
    "meterFloor",
    "meterGamma",
    "meterRef",
)

#: K4-konforme Pflicht-Overrides (E24/E5) – immer **Boolean `False`**, selbst
#: wenn die Referenz `"off"` bzw. `False`/`True` enthielte.
FORCED_OVERRIDES: Final[dict[str, Any]] = {
    "owwOnDevice": False,
    "bleProxyEnabled": False,
}

#: Effektive Config (43) bzw. `{"type":"config", …}`-Push (44, E25).
EFFECTIVE_FIELD_COUNT: Final[int] = len(REFERENCE_FIELDS)
CONFIG_PUSH_FIELD_COUNT: Final[int] = EFFECTIVE_FIELD_COUNT + 1

#: `listeningAnim` – schlanker Wake-Vorab-Push (E11/K7).  Pattern, Flag und
#: TTL aus STATE.md/E11, die Listening-Farbe je Szene aus der Referenz
#: `em_scenes._PRESETS` (malevolent = (110, 0, 45)); nur Szenen mit **einer**
#: Solid-Farbe werden unterstützt.
_LISTENING_ANIM_PATTERN: Final[str] = "solid"
_LISTENING_ANIM_TTL_SEC: Final[int] = 30
_SCENE_LISTENING: Final[dict[str, tuple[int, int, int]]] = {
    "standard": (0, 180, 0),
    "airy": (80, 150, 200),
    "malevolent": (110, 0, 45),
}


def _settings(settings_obj: Settings | None) -> Settings:
    return settings_obj if settings_obj is not None else default_settings


def _reference(reference: Mapping[str, Any] | None) -> dict[str, Any]:
    if reference is None:
        return dict(REFERENCE_CONFIG)
    if not isinstance(reference, Mapping):
        raise DeviceConfigError("reference muss ein Mapping sein")
    return dict(reference)


def _apply_manager_settings(
    effective: dict[str, Any], settings_obj: Settings | None
) -> None:
    """Manager-autoritative Felder aus `app.config.settings` überschreiben.

    Nur die Felder, die der **Manager** bindend auswertet (Barge-in, Wake,
    Speex-NS, Modell) – die Geräte-Audiofelder bleiben unverändert aus der
    Referenz (K4: „unverändert übernehmen").
    """
    cfg = _settings(settings_obj)
    effective["owwModel"] = cfg.oww_model
    effective["owwThreshold"] = cfg.oww_threshold
    effective["owwSpeexNs"] = cfg.oww_speex_ns
    effective["bargeInEnabled"] = cfg.oww_barge_in_enabled
    effective["bargeInThreshold"] = cfg.oww_barge_in_threshold


def build_effective_config(
    *,
    settings_obj: Settings | None = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Vollständige effektive Geräte-Config (genau 43 Felder, E25).

    Basis ist der Referenzsatz; darüber die Manager-Werte aus den Settings und
    zuletzt die erzwungenen K4-Overrides (`owwOnDevice=False`,
    `bleProxyEnabled=False`).  Es wird **kein** Feld erfunden oder entfernt:
    die Keys bleiben exakt die der Referenz.
    """
    effective = _reference(reference)
    _apply_manager_settings(effective, settings_obj)
    effective.update(FORCED_OVERRIDES)
    return effective


def build_k4_config(
    *,
    settings_obj: Settings | None = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimaler K4-Dict – **genau** die 25 `K4_FIELDS`, kein Fremdfeld.

    Werte stammen aus der effektiven Config (also Settings-geprägte
    Manager-Felder + Referenz), die Auswahl ist die verifizierte K4-Liste.
    """
    effective = build_effective_config(
        settings_obj=settings_obj, reference=reference
    )
    return {name: effective[name] for name in K4_FIELDS}


def build_config_push(
    *,
    settings_obj: Settings | None = None,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`{"type":"config", **effective}` – **44** Top-Level-Felder (E25/K4).

    Der Serializer kommt aus `app/protocol.py`, damit es nur **eine**
    JSON-Bauweise im Projekt gibt.
    """
    return serialize_config(
        build_effective_config(settings_obj=settings_obj, reference=reference)
    )


def build_listening_anim(
    *,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`listeningAnim`-Spec der aktiven `ledScene` (E11/K7, **nicht** in den 43).

    Shape aus der Referenz (`em_scenes.py:199-204`): `pattern`, `colors`,
    `listening: True`, `ttlSec`.  Nicht unterstützte Szenen (pride/custom/
    unbekannt) werfen `DeviceConfigError`, statt eine Farbe zu erfinden.
    """
    scene = _reference(reference).get("ledScene")
    if scene not in _SCENE_LISTENING:
        raise DeviceConfigError(
            f"ledScene {scene!r} hat keine verifizierte Solid-Listening-Farbe "
            "(unterstützt: standard, airy, malevolent)"
        )
    red, green, blue = _SCENE_LISTENING[scene]
    return {
        "pattern": _LISTENING_ANIM_PATTERN,
        "colors": [[red, green, blue]],
        "listening": True,
        "ttlSec": _LISTENING_ANIM_TTL_SEC,
    }


def build_listening_anim_push(
    *,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Schlanker `{"type":"config","listeningAnim":{…}}`-Push (E11/K7).

    Exakt 2 Top-Level-Keys; geht **separat** nach dem 44-Feld-Push heraus
    (Spec §3, `em_controller.py:3331-3334`) – der Serializer stammt aus
    `app/protocol.py`.
    """
    return serialize_listening_anim(build_listening_anim(reference=reference))
