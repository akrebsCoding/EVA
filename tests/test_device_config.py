"""Geräte-Config-Tests (P2.T6, `PLAN.md` §7 → P2.T6, Layer **L0**).

Prüfling ist `app/device_config.py` (P2.T6) gegen den verifizierten Vertrag
(`docs/device-config-reference.json`, P0.T5; `PLAN.md` K4/§2.4; `STATE.md`
§3/P0.T5, E24/E25/E26).  Getestet wird **programmatisch** – Anzahl, Namen,
Werte und die Push-Shape werden gezählt bzw. verglichen, nicht augenscheinlich.

Abdeckung der Pflichtpunkte:

* **K4-Feldliste vollständig (25/25) und keine Fremdfelder** → `test_k4_*`
* **`owwOnDevice is False` (Boolean, nicht der DB-String `"off"`), E24** → `test_oww_*`
* **`bleProxyEnabled is False`** → `test_ble_proxy_*`
* **Push-Shape: 44 Felder inkl. `type == "config"` (E25/K4)** → `test_config_push_*`
* **`bargeInThreshold == 0.15` (E23)** → `test_barge_in_*`
* **`listeningAnim`-Push getrennt und korrekt (E11/K7)** → `test_listening_anim_*`
* **keine controller-only Felder in der K4-Liste** → `test_k4_config_contains_no_controller_only_fields`

**Kein Netz, kein Gerät**: nur stdlib (JSON-Datei-Read ist lokal) und
`app.config`/`app.device_config`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.device_config import (
    CONFIG_PUSH_FIELD_COUNT,
    CONTROLLER_ONLY_FIELDS,
    DEVICE_IGNORED_FIELDS,
    EFFECTIVE_FIELD_COUNT,
    K4_FIELDS,
    REFERENCE_CONFIG,
    REFERENCE_FIELDS,
    DeviceConfigError,
    build_config_push,
    build_effective_config,
    build_k4_config,
    build_listening_anim,
    build_listening_anim_push,
)

pytestmark = pytest.mark.unit

#: Die referenzierte JSON-Datei – Quelle der 43 Feldnamen/Ist-Werte (P0.T5).
REFERENCE_JSON: Path = (
    Path(__file__).resolve().parent.parent / "docs" / "device-config-reference.json"
)

#: Die 25 K4-Felder, wie in `STATE.md:192` aus K4 hergeleitet.
EXPECTED_K4: frozenset[str] = frozenset(
    {
        # 2 Pflicht-Overrides (K4/E24)
        "owwOnDevice",
        "bleProxyEnabled",
        # 13 einzeln benannte Felder
        "startupVolume",
        "micGainDb",
        "adcDigitalGain",
        "adcMicpga",
        "aecEnabled",
        "aecDelayMs",
        "aecTailMs",
        "aecRefSource",
        "beamformingEnabled",
        "beamAngle",
        "vadThreshold",
        "vadSpeechMs",
        "vadSilenceMs",
        # 3 led* / 1 duckDb / 2 eq* / 3 limiter* / 1 agcEnabled
        "ledScene",
        "ledListenColor",
        "ledThinkColor",
        "duckDb",
        "eqBands",
        "eqLoudness",
        "limiterEnabled",
        "limiterThreshold",
        "limiterRelease",
        "agcEnabled",
    }
)


# ── Referenz-Konstante ↔ JSON-Datei (programmatischer Abgleich) ───────────
def test_reference_config_matches_json_file_key_for_key() -> None:
    data = json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))
    # Reihenfolge und Namen identisch (nicht nur die Anzahl).
    assert list(data) == list(REFERENCE_FIELDS)
    assert list(REFERENCE_CONFIG) == list(REFERENCE_FIELDS)
    # Anzahl exakt 43.
    assert len(data) == len(REFERENCE_FIELDS) == EFFECTIVE_FIELD_COUNT == 43
    # Werte wörtlich (inkl. Listen wie `eqBands`).
    assert data == REFERENCE_CONFIG
    assert data["eqBands"] == [0, 0, 0, 0, 0, 0, 0, 0]


# ── K4: 25/25, keine Fremdfelder ─────────────────────────────────────────
def test_k4_fields_are_exactly_the_25_documented_fields() -> None:
    assert len(K4_FIELDS) == 25
    assert len(set(K4_FIELDS)) == 25  # keine Dubletten
    assert set(K4_FIELDS) == EXPECTED_K4
    assert set(K4_FIELDS) <= set(REFERENCE_FIELDS)


def test_build_k4_config_is_minimal_and_invents_no_field() -> None:
    cfg = build_k4_config()
    assert len(cfg) == 25
    assert set(cfg) == set(K4_FIELDS)
    assert set(cfg).isdisjoint(set(REFERENCE_FIELDS) - set(K4_FIELDS))
    assert set(cfg) - set(REFERENCE_FIELDS) == set()


def test_k4_config_contains_no_controller_only_fields() -> None:
    assert set(K4_FIELDS).isdisjoint(CONTROLLER_ONLY_FIELDS)
    cfg = build_k4_config()
    assert set(cfg).isdisjoint(CONTROLLER_ONLY_FIELDS)
    for name in CONTROLLER_ONLY_FIELDS:
        assert name not in cfg
    # Die controller-only UND die device-ignorierten Felder sind dokumentiert
    # (E26: echte Ignorier-Liste = 15 Felder), aber nicht Teil der K4-Auswahl.
    assert len(DEVICE_IGNORED_FIELDS) == 15
    assert set(CONTROLLER_ONLY_FIELDS) <= set(REFERENCE_FIELDS)
    assert set(DEVICE_IGNORED_FIELDS) <= set(REFERENCE_FIELDS)


# ── E24: Boolean False, nicht der String "off" ───────────────────────────
def test_oww_on_device_is_boolean_false_not_the_db_string_off() -> None:
    # Beleg der Diskrepanz: die Quelle (DB/Referenz) führt den String.
    assert REFERENCE_CONFIG["owwOnDevice"] == "off"
    # Der Push erzwingt den Boolean (K4/E24), in allen drei Ausgängen.
    assert build_effective_config()["owwOnDevice"] is False
    assert build_k4_config()["owwOnDevice"] is False
    assert build_config_push()["owwOnDevice"] is False
    assert not isinstance(build_config_push()["owwOnDevice"], str)


def test_ble_proxy_enabled_is_boolean_false() -> None:
    assert build_effective_config()["bleProxyEnabled"] is False
    assert build_k4_config()["bleProxyEnabled"] is False
    assert build_config_push()["bleProxyEnabled"] is False


def test_forced_overrides_win_over_a_deviating_reference() -> None:
    reference = dict(REFERENCE_CONFIG, owwOnDevice="on", bleProxyEnabled=True)
    cfg = build_effective_config(reference=reference)
    assert cfg["owwOnDevice"] is False
    assert cfg["bleProxyEnabled"] is False


# ── Push-Shape: 43 effektiv + type = 44 (E25/K4) ─────────────────────────
def test_effective_config_has_43_fields_and_invents_none() -> None:
    cfg = build_effective_config()
    assert len(cfg) == EFFECTIVE_FIELD_COUNT == 43
    assert set(cfg) == set(REFERENCE_FIELDS)


def test_config_push_carries_all_43_effective_fields_plus_type() -> None:
    msg = build_config_push()
    assert len(msg) == CONFIG_PUSH_FIELD_COUNT == 44
    assert msg["type"] == "config"
    assert set(msg) == set(REFERENCE_FIELDS) | {"type"}
    effective = build_effective_config()
    for key in REFERENCE_FIELDS:
        assert msg[key] == effective[key]
    # JSON-tauglich (alles primitive/list-Formen, keine Sets/Tupel).
    json.dumps(msg)


def test_settings_drive_only_the_manager_authoritative_fields() -> None:
    custom = Settings(
        oww_model="custom_model_v9",
        oww_threshold=0.77,
        oww_barge_in_enabled=False,
        oww_barge_in_threshold=0.42,
        oww_speex_ns=False,
    )
    cfg = build_effective_config(settings_obj=custom)
    assert cfg["owwModel"] == "custom_model_v9"
    assert cfg["owwThreshold"] == 0.77
    assert cfg["bargeInEnabled"] is False
    assert cfg["bargeInThreshold"] == 0.42
    assert cfg["owwSpeexNs"] is False
    # Geräte-Audiofelder bleiben unverändert aus der Referenz (K4 „unverändert").
    assert cfg["micGainDb"] == 24
    assert cfg["vadThreshold"] == 0.001
    assert len(cfg) == 43


# ── E23: bargeInThreshold = 0.15 ─────────────────────────────────────────
def test_barge_in_threshold_is_015_from_the_verified_ist_value() -> None:
    assert REFERENCE_CONFIG["bargeInThreshold"] == 0.15
    assert build_effective_config()["bargeInThreshold"] == 0.15
    assert build_config_push()["bargeInThreshold"] == 0.15


# ── E11/K7: listeningAnim separat ────────────────────────────────────────
def test_listening_anim_push_is_separate_and_correct() -> None:
    anim = build_listening_anim()
    assert anim == {
        "pattern": "solid",
        "colors": [[110, 0, 45]],
        "listening": True,
        "ttlSec": 30,
    }
    msg = build_listening_anim_push()
    assert msg == {"type": "config", "listeningAnim": anim}
    assert len(msg) == 2
    assert msg["type"] == "config"
    # Der schlanke Push ist KEIN Teil der 43/44-Feld-Nachricht.
    assert set(msg).isdisjoint(REFERENCE_FIELDS)
    assert "listeningAnim" not in REFERENCE_FIELDS


def test_listening_anim_rejects_a_scene_without_verified_solid_colour() -> None:
    with pytest.raises(DeviceConfigError):
        build_listening_anim(reference=dict(REFERENCE_CONFIG, ledScene="pride"))
    with pytest.raises(DeviceConfigError):
        build_listening_anim(reference=dict(REFERENCE_CONFIG, ledScene="unbekannt"))
