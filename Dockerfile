# wyoming-manager – Container-Image (PLAN §7 P1.T2, Basis-Konfiguration §4)
#
# Zwei-Stufen-Build:
#   1) "builder"  – kompiliert/installiert die Python-Deps in ein venv unter /opt/venv
#   2) "runtime"  – enthält NUR das venv + app/ + Laufzeit-apt-Pakete, non-root
#   Build-Tools (gcc, Cython, libspeex-dev) landen dadurch nicht im Endbild.
#
# Installationsreihenfolge (PLAN §4 / requirements.txt-Kopf, in P1.T2 verifiziert):
#   requirements.txt wird an EINER Stelle aufgeteilt –
#   Zeilen MIT "openwakeword"  -> pip --no-deps   (sonst zieht openwakeword tflite-runtime)
#   Rest der Zeilen            -> normal, mit onnxruntime statt tflite (PLAN §2.4)
#   So bleibt requirements.txt die einzige Quelle für Versionen/Pins.

# ─────────────────────────────── Stufe 1: Builder ───────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# speexdsp-ns==0.1.2 ist ein sdist und braucht einen C/Cython-Build gegen die
# Speex-Header (libspeexdsp-dev laut eigener METADATA). openwakeword selbst ist
# ein reines Python-Wheel und braucht keinen Compiler.
# build-essential/libspeex-dev/cython3 sind AUSSCHLIESSLICH in dieser Stufe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libspeex-dev \
        cython3 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

# requirements.txt aufsplitten statt Abschreibungen im Dockerfile.
RUN grep -E '^[[:space:]]*openwakeword' requirements.txt > /tmp/req-openwakeword.txt \
 && grep -v -E '^[[:space:]]*openwakeword' requirements.txt > /tmp/req-rest.txt \
 && test -s /tmp/req-openwakeword.txt \
 && test -s /tmp/req-rest.txt

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r /tmp/req-rest.txt \
 && /opt/venv/bin/pip install --no-deps -r /tmp/req-openwakeword.txt

# --no-deps nimmt openwakeword seine Laufzeit-Deps mit. Nachinstalliert werden
# deshalb alle Kern-Deps AUSSER tflite-runtime – direkt aus der Paket-METADATA
# abgeleitet (keine Abschrift von Pins im Dockerfile):
#   onnxruntime / scipy sind bereits installiert und werden von pip übersprungen,
#   tqdm / scikit-learn / requests kommen dazu, tflite-runtime bleibt raus
#   (PLAN §2.4/§4: kein tflite, onnxruntime ist der Ersatz).
RUN META=$(ls /opt/venv/lib/python3.11/site-packages/openwakeword-*.dist-info/METADATA) \
 && sed -n 's/^Requires-Dist: //p' "$META" | grep -v 'extra ==' | grep -v '^tflite-runtime' \
      > /tmp/req-openwakeword-runtime.txt \
 && cat /tmp/req-openwakeword-runtime.txt \
 && /opt/venv/bin/pip install -r /tmp/req-openwakeword-runtime.txt

# Wake-Word-Modelle werden zur BUILD-Zeit geladen, damit auf .123 zur Laufzeit
# kein Download nötig ist. Korrektur zu PLAN §2.4: das PyPI-Paket enthält KEINE
# Modelle – weder Wheel (60 kB) noch sdist (71 kB) haben ein resources/-Verzeichnis.
# openWakeWord lädt sie sonst beim ersten Model()-Aufruf aus den GitHub-Release-
# Assets nach (openwakeword.utils.download_models). Der Referenz-Container hat sie
# ebenfalls nur, weil sie beim Image-Bau gezogen wurden.
# Geholt werden die Feature-Modelle (melspectrogram/embedding, tflite+onnx), die
# VAD-Modelle und hey_jarvis (tflite+onnx) – mehr nicht, keine ~30 Fremdmodelle.
RUN /opt/venv/bin/python -c "from openwakeword.utils import download_models; download_models(model_names=['hey_jarvis_v0.1'])" \
 && ls -l /opt/venv/lib/python3.11/site-packages/openwakeword/resources/models

# ─────────────────────────────── Stufe 2: Runtime ───────────────────────────
FROM python:3.11-slim AS runtime

# apt-Pakete laut PLAN §4: libsoxr0 (soxr), libspeex1 (speexdsp-ns), netcat-openbsd (Smoke-Tests).
# Ein libgomp1 ist NICHT nötig: onnxruntime 1.30 linkt keine externe OpenMP-Lib
# (ldd auf allen capi-*.so: keine libgomp-Abhängigkeit, keine fehlenden Libs),
# scikit-learn vendort seine eigene.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libsoxr0 \
        libspeex1 \
        netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    OMP_NUM_THREADS=1 \
    OMP_WAIT_POLICY=PASSIVE

# OMP_NUM_THREADS=1: Zielhost .123 hat 3.2 GiB RAM bei ~344 MiB verfügbar (E21/P0.T4);
# die ORW-Threadpools und -Arenas skalieren mit den Kernen des Hosts und konkurrieren
# mit Whisper. Der OWW-Inferenz-Pfad ist ein 1.2-MiB-Modell – ein Thread reicht,
# OMP_WAIT_POLICY=PASSIVE verhindert Leerlauf-Spinning. Beides ist per Env
# überschreibbar; P1.T5 misst die Chunk-Inferenzzeit, P6.T4 die CPU-Last.

COPY --from=builder /opt/venv /opt/venv

RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid 1001 --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --chown=appuser:app app/ /app/app/

# Container-lokale .env für die Config-GUI (write_env_value, E96/E111):
# leer im Image (Secrets laufen über env_file, nie ins Image). Der Save
# schreibt atomar (tmp-Datei + os.replace) und braucht dafür ein
# **beschreibbares Verzeichnis** — /app gehört deshalb appuser (konsistent
# mit /app/app aus COPY --chown; Live-Befund P11.T4 auf .106: ohne chown
# scheitert jeder GUI-Save mit PermissionError).
RUN chown appuser:app /app \
 && touch /app/.env \
 && chown appuser:app /app/.env \
 && chmod 600 /app/.env

# Das Wake-Word-Modell (hey_jarvis_v0.1.onnx) liegt unter
# <venv>/lib/python3.11/site-packages/openwakeword/resources/models/ – dort, wo
# openWakeWord es zur Laufzeit erwartet – und ist für non-root lesbar
# (Dateien 0644, Verzeichnisse 0755; /opt/venv bleibt root-beschrieben).
USER appuser

EXPOSE 8767/tcp
STOPSIGNAL SIGTERM

# Endgültiger Entrypoint ist app/main.py (P5.T4, mit `if __name__ == "__main__"`).
# Die Datei existiert noch nicht – der Build darf daran nicht scheitern. Host/Port
# kommen zur Laufzeit aus der Konfiguration (MANAGER_HOST/MANAGER_PORT, PLAN §4),
# deshalb keine festen uvicorn-Argumente im CMD.
CMD ["python", "-m", "app.main"]
