"""mDNS-Announcer des wyoming-managers (P5.T3, `PLAN.md` §7 → P5.T3).

Zweck
-----
Der Echo Dot findet den Manager per mDNS über den Service
``_emcontroller._tcp.local`` und wählt danach ``ws://<ip>:<port>``.  Dieses
Modul kündigt genau diesen Service an.

Verifizierte Fakten (P0.T0/P0.T6, `docs/reference/em_controller.py:4230-4243`)
-----------------------------------------------------------------------------
Die Referenz baut den Record so::

    props = {"version": "1", "server": MDNS_NAME}
    if tls_active:
        props["tls_port"] = str(SERVER_TLS_PORT)
    ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=SERVER_PORT,
        properties=props,
        server=f"{MDNS_NAME}.local.",
    )

Daraus folgt **verbindlich**:

* Service-Typ ``_emcontroller._tcp.local.``
* Instance-Name ``<MDNS_NAME>._emcontroller._tcp.local.``
* TXT ``version=1`` und ``server=<MDNS_NAME>`` — **immer**
* TXT ``tls_port=<port>`` — **nur** bei aktivem TLS
* IPv4 wird als **4-Byte**-Adresse übergeben (``socket.inet_aton``), nicht als
  String und nicht über ``inet_ntoa``.

**Es gibt KEIN TXT-Feld `tls=0/1` und KEIN `path`** (P0.T0/P0.T6).  Die
Formulierung in ``PLAN.md:474`` (``name``/``tls=0``/``path=/control``) ist
überholt; der Referenz-Quelltext hat Vorrang (dokumentiert als **E62** in
``STATE.md`` §4).

Fehlertoleranz
--------------
Kein Zeroconf, kein Netz, kein detektierbarer LAN-Address: :meth:`start`
loggt **WARN** und liefert ``False`` — es wirft **nicht**.  Der Manager
startet in jedem Fall.  ``stop()`` ist idempotent.

Schichten
---------
Reine Logik: nur stdlib (``socket``, ``threading``) + ``app.config`` +
``app.logger`` + ``zeroconf``.  **Kein** Import von ``app.pipeline`` oder
``app.ws_server``.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Final

from zeroconf import ServiceInfo, Zeroconf

from .config import settings
from .logger import get_logger

log = get_logger("mdns")

#: mDNS-Service-Typ des Controllers (P0.T0/P0.T6, `em_controller.py:4237`).
SERVICE_TYPE: Final[str] = "_emcontroller._tcp.local."

#: TXT-Schema-Version der Referenz (P0.T0: `version=1`).
TXT_VERSION: Final[str] = "1"

#: RFC 5737 TEST-NET-1; nie geroutet, es wird kein Paket gesendet. Nur der
#: UDP-`connect()` setzt die Route und wählt die Quelladresse (wie
#: `docs/reference/em_hostip.py:39`).
_ROUTE_PROBE: Final[tuple[str, int]] = ("192.0.2.1", 9)

#: Referenz aktualisiert den Record alle 120 s (`em_controller.py:273`).
_REFRESH_INTERVAL: Final[float] = 120.0


def detect_address() -> str | None:
    """LAN-IPv4 dieses Hosts ermitteln, oder ``None`` ohne nutzbare Route.

    Reine Routine-Table-Abfrage über einen UDP-``connect()`` — es wird kein
    Paket versendet (RFC 5737 TEST-NET-1).  Wirft nie: fehlende Route ist ein
    gewöhnlicher Zustand, den :meth:`MdnsAnnouncer.start` in eine WARN-Zeile
    verwandelt.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(_ROUTE_PROBE)
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


class MdnsAnnouncer:
    """Announcer für ``_emcontroller._tcp.local`` — idempotenter Lifecycle.

    Konstruktorargumente überschreiben Defaults aus :mod:`app.config`; sie
    sind für Tests und den Startpfad (P5.T4) gedacht.  ``tls_port=None``
    bedeutet „kein TLS aktiv" — dann fehlt das ``tls_port``-TXT-Feld, exakt
    wie in der Referenz (``SERVER_TLS_PORT`` von EVA ist nicht gesetzt).
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        port: int | None = None,
        address: str | None = None,
        tls_port: int | None = None,
        enabled: bool | None = None,
        refresh_interval: float = _REFRESH_INTERVAL,
    ) -> None:
        self.name: str = settings.manager_mdns_name if name is None else name
        self.port: int = settings.manager_port if port is None else port
        self.address: str | None = address
        self.tls_port: int | None = tls_port
        self.enabled: bool = settings.manager_mdns_enabled if enabled is None else enabled
        self.refresh_interval: float = refresh_interval

        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None
        self._stop_event: threading.Event | None = None
        self._refresh_thread: threading.Thread | None = None

        #: T3: der **tatsächlich** registrierte Instance-Name (nach evtl.
        #: Umbenennung durch Zeroconf) oder ``None`` vor dem Start.  Bei einer
        #: Namenskollision (zweiter Announcer mit gleichem Namen im Netz) macht
        #: ``allow_name_change=True`` aus ``echomuse`` z. B. ``echomuse-2`` —
        #: genau dieser Fall wird hier sichtbar (Diagnose, kein Verhaltens-
        #: Umbau: die Umbenennung selbst bleibt Zeroconf-Verhalten, E62).
        self.announced_name: str | None = None

    # ── öffentliche API ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True, solange ein Service registriert ist."""
        return self._info is not None

    @property
    def name_conflict(self) -> bool:
        """True, wenn Zeroconf den Instance-Namen ändern musste (Doppel-Announce).

        Nur nach einem erfolgreichen :meth:`start` aussagekräftig (davor
        ``False`` — „nicht bekannt" wird als ``False`` ausgedrückt, weil das
        Feld ausschließlich Diagnose ist).
        """
        return self.announced_name is not None and self.announced_name != self.name

    @staticmethod
    def registered_instance_name(info_name: str) -> str:
        """Instance-Name aus einem registrierten ``ServiceInfo.name`` lösen.

        ``"echomuse._emcontroller._tcp.local."`` → ``"echomuse"``; bei einer
        Zeroconf-Umbenennung entsprechend ``"echomuse-2"``.  Bewusst als
        reine Zeichenketten-Operation (einfach testbar ohne Netz).
        """
        suffix = f".{SERVICE_TYPE}"
        if info_name.endswith(suffix):
            return info_name[: -len(suffix)]
        return info_name

    def make_service_info(self) -> ServiceInfo:
        """ServiceInfo exakt nach Referenz bauen (IPv4 via ``inet_aton``).

        Wirft ``ValueError``, wenn weder ``address`` noch eine detektierbare
        LAN-Adresse vorliegt — :meth:`start` fängt das in eine WARN-Zeile.
        """
        ip = self.address or detect_address()
        if not ip:
            raise ValueError("keine IPv4-Adresse ermittelbar (kein LAN-Route)")

        properties: dict[str, str] = {
            "version": TXT_VERSION,
            "server": self.name,
        }
        if self.tls_port:
            # Nur bei aktivem TLS; absent = pre-TLS-Controller ⇒ plain ws.
            properties["tls_port"] = str(self.tls_port)

        return ServiceInfo(
            SERVICE_TYPE,
            f"{self.name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            properties=properties,
            server=f"{self.name}.local.",
        )

    def start(self) -> bool:
        """Service announcen.  Idempotent; wirft **nie** (Fehler ⇒ WARN).

        Rückgabe: ``True`` bei aktiver Registrierung, ``False`` wenn
        deaktiviert oder das Announcen fehlschlug.  Der Manager läuft in
        beiden Fällen weiter.
        """
        if self._info is not None:
            return True
        if not self.enabled:
            log.info("mDNS deaktiviert (MANAGER_MDNS_ENABLED=false) — kein Announcement")
            return False

        try:
            info = self.make_service_info()
            zeroconf = Zeroconf()
            try:
                zeroconf.register_service(info, allow_name_change=True)
            except Exception:
                zeroconf.close()
                raise
        except Exception as exc:  # noqa: BLE001 - bewusst: jedes Scheitern degradiert.
            log.warning("mDNS-Announcement fehlgeschlagen — Manager läuft ohne mDNS weiter: %s", exc)
            return False

        self._zeroconf = zeroconf
        self._info = info
        self._adopt(info)
        log.info(
            "mDNS announciert %s.%s → %s:%d%s",
            self.name,
            SERVICE_TYPE,
            info.parsed_addresses()[0] if info.parsed_addresses() else "?",
            self.port,
            f" (tls_port={self.tls_port})" if self.tls_port else "",
        )
        self._start_refresh()
        return True

    def stop(self) -> None:
        """Service abmelden und Zeroconf schließen.  Idempotent, wirft nie."""
        self._stop_refresh()

        info, zeroconf = self._info, self._zeroconf
        self._info = None
        self._zeroconf = None
        self.announced_name = None

        if info is None and zeroconf is None:
            return
        if zeroconf is not None and info is not None:
            try:
                zeroconf.unregister_service(info)
            except Exception as exc:  # noqa: BLE001
                log.warning("mDNS-Abmeldung fehlgeschlagen: %s", exc)
        if zeroconf is not None:
            try:
                zeroconf.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("Zeroconf-Schließen fehlgeschlagen: %s", exc)

    # ── interner Refresh (Referenz: alle 120 s) ──────────────────────

    def _adopt(self, info: ServiceInfo) -> None:
        """Registrierte ServiceInfo übernehmen + Doppel-Announce sichtbar machen.

        T3/Doppel-Pairing: hält Zeroconf eine Namenskollision fest (ein
        ZWEITER Announcer hält den Instanznamen schon), hat es uns wegen
        ``allow_name_change=True`` umgenannt (z. B. ``echomuse-2``).  Der Dot
        filtert auf den Instanznamen (`echomuse`) und findet
        diesen Announcer dann möglicherweise NICHT ⇒ Warnung, aber kein
        Abbruch (Betriebsverhalten unverändert).
        """
        self.announced_name = self.registered_instance_name(info.name)
        if self.name_conflict:
            log.warning(
                "mDNS-Instanzname geändert: '%s' belegt, announciere als '%s' "
                "(Doppel-Announce im Netz? Der Dot sucht nach '%s').",
                self.name,
                self.announced_name,
                self.name,
            )

    def _start_refresh(self) -> None:
        if self.refresh_interval <= 0:
            return
        self._stop_event = threading.Event()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            args=(self._stop_event,),
            name="mdns-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _stop_refresh(self) -> None:
        event, thread = self._stop_event, self._refresh_thread
        self._stop_event = None
        self._refresh_thread = None
        if event is not None:
            event.set()
        if thread is not None:
            thread.join(timeout=2.0)

    def _refresh_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.refresh_interval):
            info, zeroconf = self._info, self._zeroconf
            if info is None or zeroconf is None:
                return
            try:
                zeroconf.update_service(info)
            except Exception as exc:  # noqa: BLE001 - Refresh darf nie töten.
                log.warning("mDNS-Refresh fehlgeschlagen: %s", exc)


__all__ = [
    "MdnsAnnouncer",
    "SERVICE_TYPE",
    "TXT_VERSION",
    "detect_address",
]
