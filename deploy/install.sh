#!/usr/bin/env bash
# EVA – Installer (P10.T1; PLAN §7 P10, Zielbild E102)
#
# „git clone + install.sh“ auf jedem Rechner. Stand T1: Installer-SKELETT
# mit --test-Flag (hartkodierte Testwerte, kein Onboarding-Wizard – der
# kommt in P10.T2).
#
# Ablauf:
#   (a) Docker + compose-plugin prüfen/installieren (Debian: offizielles
#       docker-ce-Repo; Arch: pacman — UNGETESTET, siehe INSTALL.md)
#   (b) Zielverzeichnis anlegen (Default /opt/eva, --dest überschreibt)
#   ohne --test (Produktions-Flow, P10.T2):
#   (c) Onboarding-Wizard (deploy/onboarding/wizard.py): fragt Pflichtfelder
#       + Topologie ab, schreibt .env (0600) + Compose, `up -d`, Health,
#       Summary — der Ein-Befehls-Flow für den Freund.
#   mit --test (P10.T1-Skelett, ohne Wizard):
#   (c) .env aus test.env.example (Fake-Werte, mDNS AUS)
#   (d) Compose aus compose/docker-compose.yml.tmpl rendern (single-host)
#   (e) docker compose up -d
#   (f) Health-Check + Smoke-Test (STT/TTS über die Produktions-Clients)
#   (g) Summary + Next-Steps
#
# mDNS-Isolation (wichtig in Netzen mit laufendem Produktions-Manager):
# `install.sh --test` setzt MANAGER_MDNS_ENABLED=false (Bestandteil
# test.env.example) — der Test-Manager announciert `_emcontroller._tcp`
# nicht und der Dot verbindet sich nicht dorthin. Produktions-Deploys
# (Wizard ab T2) lassen mDNS an.
#
# Idempotent: erneute Läufe aktualisieren Templates und fahren den Stack
# nicht unnötig herunter; eine bestehende .env wird NUR mit --test
# überschrieben (sonst Datenverlust-Schutz, Wizard übernimmt das Merge).

set -euo pipefail

# ── Konstanten ─────────────────────────────────────────────────────
SCRIPT_PATH="${BASH_SOURCE[0]}"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
TEMPLATE_DIR="${REPO_ROOT}/deploy"
TARGET_DIR="/opt/eva"
TEST_MODE=0
ANSWERS_FILE=""
NON_INTERACTIVE=0
NO_UP=0
HEALTH_TIMEOUT_S=900          # Modell-Download (~574 MB) beim ersten Start
MANAGER_PORT_DEFAULT=8767
TZ_DEFAULT="Europe/Berlin"
WHISPER_MODEL_DEFAULT="small"
WHISPER_COMPUTE_TYPE_DEFAULT="int8"
PIPER_VOICE_DEFAULT="de_DE-thorsten-high"

usage() {
  cat <<EOF
Usage: install.sh [--test] [--dest DIR] [--non-interactive --answers-file DATEI] [--no-up]

  --test                  Test-Deploy: hartkodierte Testwerte (Fake-HA-Token,
                          Fake-LLM-Key, HA_BASE_URL=http://127.0.0.1:8123) und
                          mDNS AUS (Isolation gegen den echten Dot). Kein Wizard.
  --dest DIR              Zielverzeichnis (Default: ${TARGET_DIR}).
  --non-interactive       Wizard ohne Fragen (Pflicht: --answers-file);
                          für automatisierte Tests.
  --answers-file DATEI    env-Format: TOPOLOGY, REMOTE_HOST, HA_BASE_URL,
                          HA_TOKEN, LLM_API_KEY (+ optional KEY=VALUE-Overrides).
  --no-up                 Wizard rendert nur (.env/Compose), startet nicht.
  -h, --help              Diese Hilfe.
EOF
}

log()  { printf '\033[1;36m[eva]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[eva]\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) TEST_MODE=1; shift ;;
    --dest) TARGET_DIR="${2:?--dest braucht ein Verzeichnis}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --answers-file) ANSWERS_FILE="${2:?--answers-file braucht eine Datei}"; shift 2 ;;
    --no-up) NO_UP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unbekanntes Argument: $1" ;;
  esac
done

# /opt braucht root; bequemer Re-Exec mit sudo statt späterem Steckenbleiben.
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null || die "Bitte als root ausführen (oder sudo installieren)."
  exec sudo -E "$SCRIPT_PATH" "$@"
fi

command -v git >/dev/null || die "git fehlt (Repository liegt vor: ${REPO_ROOT})."

# ── (a) Docker prüfen/installieren ─────────────────────────────────
install_docker_debian() {
  log "Installiere Docker CE (offizielles Repo, Debian)…"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null 2>&1 || true
}

install_docker_arch() {
  # ⚠ UNGETESTET (T1: Debian-12-LXC als Testumgebung). Der Arch-Pfad folgt
  # dem pacman-Standard; Rückmeldung willkommen (siehe deploy/docs/INSTALL.md).
  warn "Arch-Linux-Pfad ist UNGETESTET (P10.T1 nur auf Debian 12 geprüft)."
  pacman -Sy --noconfirm --needed docker docker-compose docker-buildx
  systemctl enable --now docker
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker vorhanden: $(docker --version) · compose $(docker compose version --short)"
    return 0
  fi
  case "$( . /etc/os-release && echo "${ID} ${ID_LIKE:-}")" in
    *arch*) install_docker_arch ;;
    *debian*|*ubuntu*) install_docker_debian ;;
    *) die "Nicht unterstützte Distribution (Debian/Ubuntu und Arch implementiert)." ;;
  esac
  command -v docker >/dev/null || die "Docker-Installation fehlgeschlagen."
  docker compose version >/dev/null 2>&1 || die "compose-plugin fehlt nach der Installation."
  log "Docker installiert: $(docker --version) · compose $(docker compose version --short)"
}

ensure_docker
docker info >/dev/null 2>&1 || die "Docker-Daemon läuft nicht (systemctl start docker)."

# ── (b) Zielverzeichnis ────────────────────────────────────────────
log "Zielverzeichnis: ${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/data/whisper" "${TARGET_DIR}/data/piper" "${TARGET_DIR}/share"

# ── (c) Ohne --test: Onboarding-Wizard (P10.T2, E103) ──────────────
if [[ ${TEST_MODE} -eq 0 ]]; then
  command -v python3 >/dev/null || die "python3 fehlt (Wizard braucht es; stdlib-only)."
  log "Starte Onboarding-Wizard …"
  WIZARD_ARGS=(--dest "${TARGET_DIR}")
  if [[ ${NON_INTERACTIVE} -eq 1 ]]; then
    [[ -n ${ANSWERS_FILE} ]] || die "--non-interactive braucht --answers-file DATEI."
    WIZARD_ARGS+=(--non-interactive --answers-file "${ANSWERS_FILE}")
  elif [[ -n ${ANSWERS_FILE} ]]; then
    die "--answers-file nur zusammen mit --non-interactive."
  fi
  [[ ${NO_UP} -eq 1 ]] && WIZARD_ARGS+=(--no-up)
  exec python3 "${TEMPLATE_DIR}/onboarding/wizard.py" "${WIZARD_ARGS[@]}"
fi

# ── (c, --test) .env mit Fake-Werten rendern ────────────────────────
ENV_FILE="${TARGET_DIR}/.env"
log "Test-Modus: .env mit FAKE-Werten (kein Secret!) rendern…"
cp "${TEMPLATE_DIR}/test.env.example" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# Single-Host-Topologie (E102): Hosts EXPLIZIT setzen (kein stiller
# Default!) — bei verteilter Wahl setzt der Wizard (T2) hier die zweite
# Host-IP.
env_set() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}
env_set WHISPER_HOST 127.0.0.1
env_set PIPER_HOST 127.0.0.1

# ── (d) Compose rendern ────────────────────────────────────────────
COMPOSE_FILE="${TARGET_DIR}/docker-compose.yml"
sed_escape() { printf '%s' "$1" | sed -e 's/[&/\]/\\&/g'; }

INITIAL_PROMPT="$(cat "${TEMPLATE_DIR}/initial-prompt.txt")"
[[ -n ${INITIAL_PROMPT} ]] || die "initial-prompt.txt ist leer."
# Shell-Quoting: der Prompt landet als Command-Argument im Container
# (enthält keine Quotes — doppelte Anführungszeichen genügen, aber wir
# quoten generisch über sed_escape).
PROMPT_QUOTED="\"$(sed_escape "${INITIAL_PROMPT}")\""

# single-host: host-Netz ⇒ kein Port-Publish (eine Kommentarzeile hält
# das YAML in beiden Fällen valide).
PUBLISH_COMMENT="    # (Port-Publish: nur bei verteilter Topologie — hier host-Netz.)"

log "Rendere Compose nach ${COMPOSE_FILE}…"
sed -e "s|@REPO_ROOT@|$(sed_escape "${REPO_ROOT}")|g" \
    -e "s|@DATA_DIR@|$(sed_escape "${TARGET_DIR}/data")|g" \
    -e "s|@TZ@|$(sed_escape "${TZ_DEFAULT}")|g" \
    -e "s|@MANAGER_MEM_LIMIT@|512m|g" \
    -e "s|@WHISPER_MODEL@|$(sed_escape "${WHISPER_MODEL_DEFAULT}")|g" \
    -e "s|@WHISPER_COMPUTE_TYPE@|$(sed_escape "${WHISPER_COMPUTE_TYPE_DEFAULT}")|g" \
    -e "s|@WHISPER_MEM_LIMIT@|3g|g" \
    -e "s|@PIPER_VOICE@|$(sed_escape "${PIPER_VOICE_DEFAULT}")|g" \
    -e "s|@PIPER_MEM_LIMIT@|1g|g" \
    -e "s|@INITIAL_PROMPT@|$(sed_escape "${PROMPT_QUOTED}")|g" \
    -e "s|^@WHISPER_PUBLISH@\$|${PUBLISH_COMMENT}|" \
    -e "s|^@PIPER_PUBLISH@\$|${PUBLISH_COMMENT}|" \
    "${TEMPLATE_DIR}/compose/docker-compose.yml.tmpl" > "${COMPOSE_FILE}"

grep -v '^\s*#' "${COMPOSE_FILE}" | grep -q '@' \
  && die "Render unvollständig (Platzhalter übrig) — Template/Installer prüfen."
chmod 600 "${ENV_FILE}"

docker compose --project-directory "${TARGET_DIR}" config -q \
  || die "Gerenderte Compose ist invalide."

# ── (e) Stack starten ──────────────────────────────────────────────
log "Baue Manager-Image und starte den Stack (erster Start: Modell-Download ~574 MB)…"
docker compose --project-directory "${TARGET_DIR}" up -d --build

# ── (f) Health-Check + Smoke-Test ──────────────────────────────────
log "Warte auf 3 healthy Container (Timeout ${HEALTH_TIMEOUT_S}s)…"
elapsed=0
while :; do
  states="$(docker compose --project-directory "${TARGET_DIR}" ps --format '{{.Name}} {{.Health}}' 2>/dev/null | sort || true)"
  healthy_count="$(printf '%s' "${states}" | grep -c ' healthy' || true)"
  if [[ ${healthy_count} -ge 3 ]]; then
    log "Alle 3 Container healthy (${elapsed}s):"
    printf '%s\n' "${states}"
    break
  fi
  [[ ${elapsed} -ge ${HEALTH_TIMEOUT_S} ]] && {
    printf '%s\n' "${states}" >&2
    die "Health-Timeout. Logs: docker compose --project-directory ${TARGET_DIR} logs"
  }
  sleep 5; elapsed=$((elapsed + 5))
done

HEALTH_URL="http://127.0.0.1:${MANAGER_PORT_DEFAULT}/health"
HEALTH_BODY="$(curl -fsS "${HEALTH_URL}")" || die "Manager /health nicht erreichbar."
log "Manager /health: ${HEALTH_BODY}"

# Smoke-Test über die Produktions-Clients (im Manager-Container, dort
# liegt die komplette App-Umgebung). Fixture: 16 kHz/S16_LE/mono WAV.
log "Smoke-Test: STT + TTS über Produktions-Clients…"
SMOKE_WAV="${REPO_ROOT}/tests/fixtures/sample_16k.wav"
[[ -f ${SMOKE_WAV} ]] || die "Smoke-Fixture fehlt: ${SMOKE_WAV}"
docker cp "${SMOKE_WAV}" eva-manager:/tmp/smoke.wav >/dev/null
if ! docker exec -i eva-manager python - <<'PY'
import asyncio, wave

from app.config import settings
from app.stt_client import SttClient
from app.tts_client import TtsClient


def pcm() -> bytes:
    with wave.open("/tmp/smoke.wav", "rb") as w:
        return w.readframes(w.getnframes())


async def main() -> None:
    stt = SttClient(host=settings.whisper_host, port=settings.whisper_port,
                    language=settings.whisper_language)
    text = await stt.transcribe(pcm())
    await stt.aclose()
    print(f"SMOKE STT: host={settings.whisper_host}:{settings.whisper_port} "
          f"transcript={text!r}")

    tts = TtsClient(host=settings.piper_host, port=settings.piper_port,
                    voice=settings.piper_voice)
    rate, total, chunks = None, 0, 0
    async for r, chunk in tts.synthesize("Schalte das Licht im Wohnzimmer ein."):
        rate, chunks = r, chunks + 1
        total += len(chunk)
    await tts.aclose()
    print(f"SMOKE TTS: host={settings.piper_host}:{settings.piper_port} "
          f"rate={rate} chunks={chunks} pcm_bytes={total}")


asyncio.run(main())
PY
then
  die "Smoke-Test fehlgeschlagen (STT/TTS)."
fi

# ── (g) Summary ────────────────────────────────────────────────────
echo
log "═══ EVA-Deploy fertig ═══"
echo "  Verzeichnis : ${TARGET_DIR}"
echo "  Manager     : ${HEALTH_URL}  (${HEALTH_BODY})"
echo "  Dashboard   : http://$(hostname -I | awk '{print $1}'):${MANAGER_PORT_DEFAULT}/dashboard"
echo "  Daten       : ${TARGET_DIR}/data/{whisper,piper} (HF-Cache, dauerhaft)"
echo "  .env        : ${ENV_FILE} (chmod 600)"
if [[ ${TEST_MODE} -eq 1 ]]; then
  echo "  Modus       : TEST (Fake-HA-Token, Fake-LLM-Key, mDNS AUS)"
fi
echo
echo "  Nächste Schritte:"
if [[ ${TEST_MODE} -eq 1 ]]; then
  echo "   · Test läuft ohne echtes HA/LLM — Wizard (P10.T2) für echte Werte."
  echo "   · Abbau:  docker compose --project-directory ${TARGET_DIR} down"
else
  echo "   · Secrets in ${ENV_FILE} eintragen (HA_TOKEN, LLM_API_KEY, HA_BASE_URL)"
  echo "     oder den Onboarding-Wizard nutzen (P10.T2)."
  echo "   · Danach: docker compose --project-directory ${TARGET_DIR} up -d"
fi
echo "   · Details/Rollback: deploy/docs/INSTALL.md"
