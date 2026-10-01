"""Connect-Sequenz, Button-Pfad und Sequenz-Re-Sync (P5.T5, `PLAN.md:476`).

Reine L0-Logik: **kein Netz, kein WebSocket**; nur stdlib + `app.config` +
`app.logger` + `app.protocol` + `app.device_config`.  Die Transporte
(`send_control`/`send_binary`) werden **injiziert** – `tests/test_connect_sequence.py`
(P5.T7) läuft damit ohne Netz/Gerät, und `app/ws_server.py` bindet später seine
`send_control`/`send_binary` als Callables ein.

Umgesetzte Festlegungen (jede Zahl/Reihenfolge belegt):

* **Connect-Sequenz (Spec §3.1, `em_controller.py:3280-3334`, `:2409`)** – nach
  dem `ack` (den `app/ws_server.py` im Handshake sendet, K1/E27) genau:
  1. `{"type":"config", …}` – **44 Top-Level-Felder** inkl. `type` (K4/E25),
     gebaut über :func:`app.device_config.build_config_push`;
  2. `{"type":"config","listeningAnim":{…}}` – **nur falls der Anim-Katalog
     bekannt ist** (E11/K7): `ledScene` gehört zu den verifizierten
     Solid-Szenen.  Unbekannte Szene ⇒ :class:`app.device_config.DeviceConfigError`
     ⇒ der schlanke Push **entfällt** (keine erfundene Farbe);
  3. `{"type":"mic_start"}` – **permanent**, ohne `lock_mic` (K3, E28).
* **Button-Pfad (K6, `em_controller.py:3082-3096`)** – **exakt** diese
  Reihenfolge: ``mic_stop`` → ``mic_start{lock_mic:true}`` → (Turn läuft) →
  ``mic_stop`` → ``mic_start{}``.  Das abschließende `mic_stop`
  ist **zwingend** (`em_controller.py:3085-3094`); deshalb läuft es in
  :meth:`ConnectSequence.run_button_turn` im **`finally`**, damit der
  `lock_mic`-Stream auch bei Fehler/Cancel zurück auf den Omni-Stream geht.
  `lock_mic:true` **nur** hier (und bei HA-`start_conversation`, nicht P5.T5).
* **Sequenz-Re-Sync (E28)** – :class:`SequenceTracker` erkennt eine **Lücke**
  in der eingehenden Mic-Sequenz (`seq != erwartet`), zählt sie und
  **synchronisiert** den Zähler auf `next_sequence(seq)` (uint16 big-endian,
  Wrap `65535 → 0`).  Übernommen aus `app.protocol.next_sequence`, **keine**
  eigene Modulo-Konstante.

Nicht Teil von P5.T5: die Keepalive-/`ack`-Erzeugung (P5.T1/T2) und die
Tests (P5.T6/T7).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Final, Mapping, Optional, Union

from app.config import settings
from app.device_config import (
    DeviceConfigError,
    build_config_push,
    build_listening_anim_push,
)
from app.logger import get_logger
from app.protocol import (
    SEQ_MODULUS,
    Ack,
    next_sequence,
    parse_ack,
    serialize_listening_anim,
    serialize_mic_start,
    serialize_mic_stop,
)

__all__ = [
    "DeviceSessionError",
    "ControlSender",
    "BinarySender",
    "TurnRunner",
    "SequenceTracker",
    "ConnectSequence",
]

_LOG: Final = get_logger("device_session")

#: `async (device_id, message) -> None` – JSON auf `/control` (C→D).
ControlSender = Callable[[str, Mapping[str, Any]], Awaitable[None]]
#: `async (device_id, data) -> None` – Binär auf `/data` (C→D).
BinarySender = Callable[[str, bytes], Awaitable[None]]
#: `async () -> Any` – der Turn-Rumpf zwischen den Button-Rahmen (K6).
TurnRunner = Callable[[], Awaitable[Any]]

#: Ein `ack` wird als Mapping/str/bytes **oder** als bereits geparster `Ack` genommen.
AckInput = Union[Ack, str, bytes, Mapping[str, Any]]


class DeviceSessionError(ValueError):
    """Fehler der Connect-Sequenz/des Button-Pfads (definiert statt still zu degradieren)."""


class SequenceTracker:
    """Erwartete Mic-Sequenznummer + **Re-Sync** nach Lücke (E28).

    Das Gerät nummeriert seine `0x01`-Mic-Frames selbst (uint16 big-endian,
    `app.protocol.MIC_FRAME_TYPE`).  Der Tracker merkt sich die **nächste**
    erwartete Nummer und erkennt jeden Sprung als Lücke; er korrigiert seinen
    Zähler sofort auf die beobachtete Nummer (`next_sequence(seq)`), damit der
    Manager nach einem Geräte-Neustart/Frame-Verlust weiter synchron bleibt.
    """

    def __init__(self, expected: Optional[int] = None) -> None:
        if expected is not None and not 0 <= int(expected) < SEQ_MODULUS:
            raise DeviceSessionError(
                f"Sequenz-Start {expected} liegt außerhalb 0..{SEQ_MODULUS - 1} (E28)"
            )
        self._expected: Optional[int] = int(expected) if expected is not None else None
        self._last: Optional[int] = None
        self._gaps = 0
        self._resyncs = 0

    @property
    def expected(self) -> Optional[int]:
        """Die als Nächstes erwartete Nummer (oder `None`, noch nicht synchron)."""
        return self._expected

    @property
    def last(self) -> Optional[int]:
        """Die zuletzt beobachtete Nummer (oder `None`)."""
        return self._last

    @property
    def gaps(self) -> int:
        """Wie oft eine Lücke erkannt wurde."""
        return self._gaps

    @property
    def resyncs(self) -> int:
        """Wie oft der Zähler nachgezogen wurde (= Zahl der Lücken)."""
        return self._resyncs

    @property
    def synced(self) -> bool:
        """True, sobald mindestens eine Nummer beobachtet wurde."""
        return self._expected is not None

    def observe(self, seq: int) -> bool:
        """Eine eingehende Mic-Sequenznummer verbuchen.

        Liefert **True**, wenn `seq` von der Erwartung abwich (Lücke) – in dem
        Fall wird der Zähler synchronisiert.  Der erste Aufruf ist **keine**
        Lücke (er synchronisiert nur).  `bool` wird abgelehnt, weil es in
        Python `int` ist und keine echte Sequenznummer darstellt.
        """
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise DeviceSessionError(
                f"Mic-Sequenz muss ein int sein, ist {type(seq).__name__} (E28)"
            )
        value = int(seq) % SEQ_MODULUS
        gap = self._expected is not None and value != self._expected
        if gap:
            self._gaps += 1
            self._resyncs += 1
            _LOG.warning(
                "Mic-Sequenz-Lücke: seq=%d erwartet=%s – Zähler re-synchronisiert (E28)",
                value,
                self._expected,
            )
        self._last = value
        self._expected = next_sequence(value)
        return gap

    def reset(self, expected: Optional[int] = None) -> None:
        """Tracker zurücksetzen (z. B. bei Re-Connect); optional mit Startwert."""
        if expected is not None and not 0 <= int(expected) < SEQ_MODULUS:
            raise DeviceSessionError(
                f"Sequenz-Start {expected} liegt außerhalb 0..{SEQ_MODULUS - 1} (E28)"
            )
        self._expected = int(expected) if expected is not None else None
        self._last = None
        self._gaps = 0
        self._resyncs = 0


class ConnectSequence:
    """Connect-Sequenz + Button-Pfad für **ein** Gerät (`PLAN.md:476`).

    Alle ausgehenden JSON-Nachrichten laufen über den **injizierten**
    ``send_control``; ``send_binary`` wird akzeptiert (P5.T6/T7 binden beide
    Transporte), aber die Connect-/Button-Sequenz ist reines `/control`-JSON.
    """

    def __init__(
        self,
        device_id: str,
        *,
        send_control: ControlSender,
        send_binary: Optional[BinarySender] = None,
        settings_obj: Any = None,
        reference: Optional[Mapping[str, Any]] = None,
        enable_listening_anim: bool = True,
        listening_anim: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise DeviceSessionError("device_id muss ein nicht-leerer str sein")
        if send_control is None:
            raise DeviceSessionError("send_control muss injiziert werden (P5.T5)")
        self.device_id = device_id
        self._send_control = send_control
        self._send_binary = send_binary
        self._settings = settings_obj if settings_obj is not None else settings
        self._reference = reference
        self._enable_listening_anim = bool(enable_listening_anim)
        self._listening_anim = listening_anim
        #: Re-Sync der eingehenden Mic-Sequenznummern (E28).
        self.sequence = SequenceTracker()
        self._sent: list[dict[str, Any]] = []

    # ── Diagnose/Test ─────────────────────────────────────────────────
    @property
    def frames(self) -> tuple[dict[str, Any], ...]:
        """Alle bisher ausgegebenen `/control`-Frames (Reihenfolge, Kopien)."""
        return tuple(dict(frame) for frame in self._sent)

    @property
    def mic_gaps(self) -> int:
        """Zahl der erkannten Mic-Sequenz-Lücken (E28)."""
        return self.sequence.gaps

    def observe_mic_sequence(self, seq: int) -> bool:
        """Eingehende Mic-Sequenznummer an den Tracker reichen (Re-Sync, E28)."""
        return self.sequence.observe(seq)

    # ── Connect-Sequenz ───────────────────────────────────────────────
    async def on_ack(self, ack: AckInput) -> tuple[dict[str, Any], ...]:
        """Nach dem `ack` die Connect-Sequenz senden (Spec §3.1).

        Reihenfolge: `config` (44 Felder, K4) → `listeningAnim` **falls der
        Katalog bekannt ist** (E11) → `mic_start` (**permanent**, K3).  Der
        `ack` selbst wird **nicht** gesendet (das macht `app/ws_server.py`) und
        `features` wird **nie** erwartet/gelesen (K1/E27/E44).
        """
        parsed = ack if isinstance(ack, Ack) else parse_ack(ack)
        if parsed.device_id != self.device_id:
            raise DeviceSessionError(
                f"ack.device_id={parsed.device_id!r} passt nicht zu {self.device_id!r}"
            )
        frames: list[dict[str, Any]] = []
        frames.append(
            await self._emit(
                build_config_push(
                    settings_obj=self._settings, reference=self._reference
                )
            )
        )
        anim = self._resolve_listening_anim()
        if anim is not None:
            frames.append(await self._emit(anim))
        frames.append(await self._emit(serialize_mic_start()))
        return tuple(frames)

    # ── Button-Pfad (K6) ──────────────────────────────────────────────
    async def button_start(self) -> tuple[dict[str, Any], ...]:
        """Button-Turn-Start (K6): `mic_stop` → `mic_start{lock_mic:true}`."""
        return (
            await self._emit(serialize_mic_stop()),
            await self._emit(
                serialize_mic_start(
                    lock_mic=bool(self._settings.mic_start_lock_mic_button)
                )
            ),
        )

    async def button_end(self) -> tuple[dict[str, Any], ...]:
        """Button-Turn-Ende (K6): `mic_stop` → `mic_start{}` (permanent)."""
        return (
            await self._emit(serialize_mic_stop()),
            await self._emit(serialize_mic_start()),
        )

    async def run_button_turn(self, turn: TurnRunner) -> tuple[dict[str, Any], ...]:
        """Den kompletten Button-Pfad um einen Turn-Rumpf legen (K6).

        Ergibt **exakt** `mic_stop` → `mic_start{lock_mic:true}` →
        ``await turn()`` → `mic_stop` → `mic_start{}`.  Das abschließende Paar
        läuft im **`finally`** (zwingend, `em_controller.py:3085-3094`).
        """
        frames = list(await self.button_start())
        try:
            await turn()
        finally:
            frames.extend(await self.button_end())
        return tuple(frames)

    # ── Intern ────────────────────────────────────────────────────────
    def _resolve_listening_anim(self) -> Optional[dict[str, Any]]:
        """`listeningAnim`-Push ermitteln oder `None`, wenn der Katalog unbekannt ist."""
        if not self._enable_listening_anim:
            return None
        if self._listening_anim is not None:
            return serialize_listening_anim(self._listening_anim)
        try:
            return build_listening_anim_push(reference=self._reference)
        except DeviceConfigError as exc:
            _LOG.info(
                "listeningAnim-Katalog unbekannt (%s) – schlanker Push entfällt (E11)",
                exc,
            )
            return None

    async def _emit(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Ein `/control`-Frame senden und protokollieren."""
        payload = dict(message)
        await self._send_control(self.device_id, payload)
        self._sent.append(payload)
        return payload
