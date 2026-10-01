"""wyoming-manager – FastAPI/uvicorn-WebSocket-Server für die EchoMuse-Firmware.

Paketfundament aus P1.T0. Das Paket selbst ist absichtlich **abhängigkeitsfrei**
(kein Import von `app.config` hier), damit `import app` auch ohne installierte
Runtime-Dependencies gelingt.  Für die Settings gilt:

    from app.config import settings

Bindende Grundlagen: `PLAN.md` §4 (Konfiguration), §2.5 (K1–K7), §7 (P1);
Entscheidungen E5, E17, E23, E27 in `STATE.md` §4.
"""

__version__ = "0.1.0"
