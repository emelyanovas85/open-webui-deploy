#!/usr/bin/env bash
# =============================================================================
# deploy.sh — разворачивание open-webui-deploy на удалённой машине
#
# Использование:
#   ./deploy.sh [ОПЦИИ]
#
# Опции:
#   -h, --host      SSH-хост удалённой машины (по умолчанию: 10.1.5.97)
#   -u, --user      SSH-пользователь (по умолчанию: svc-local-adm)
#   -p, --port      SSH-порт (по умолчанию: 22)
#   -i, --identity  Путь к приватному SSH-ключу (необязательно)
#   -b, --branch    Ветка Git для деплоя (по умолчанию: main)
#   --force-image   Принудительно передать образ, даже если он уже актуален
#   --help          Показать справку
#
# Примеры:
#   ./deploy.sh
#   ./deploy.sh -h 192.168.1.100 -u deploy
#   ./deploy.sh -i ~/.ssh/id_rsa -b feature/new-config
#   ./deploy.sh --force-image
#
# Примечание: удалённый хост не имеет доступа в интернет.
# Скрипт сравнивает Image ID локально и на сервере — если совпадают,
# передача пропускается. Иначе передаёт образ через docker save | ssh docker load.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

log()   { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()    { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*"; }
error() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REMOTE_HOST="10.1.5.97"
REMOTE_USER="svc-local-adm"
REMOTE_PORT="22"
SSH_KEY=""
GIT_BRANCH="main"
APP_DIR="~/open-webui-deploy"
APP_PORT="8087"
COMPOSE_FILE="docker-compose.yml"
OPEN_WEBUI_IMAGE="ghcr.io/open-webui/open-webui:v0.8.10"
PYTHON_INIT_IMAGE="python:3.11-slim"
FORCE_IMAGE=false

usage() {
  grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,2\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--host)      REMOTE_HOST="$2";     shift 2 ;;
    -u|--user)      REMOTE_USER="$2";     shift 2 ;;
    -p|--port)      REMOTE_PORT="$2";     shift 2 ;;
    -i|--identity)  SSH_KEY="$2";         shift 2 ;;
    -b|--branch)    GIT_BRANCH="$2";      shift 2 ;;
    --force-image)  FORCE_IMAGE=true;     shift   ;;
    --help)         usage ;;
    *) error "Неизвестный аргумент: $1. Используйте --help для справки." ;;
  esac
done

SSH_CTRL_DIR="$(mktemp -d /tmp/ssh-ctrl-XXXXXX)"
SSH_CTRL_SOCK="${SSH_CTRL_DIR}/master"

cleanup() {
  ssh -o ControlPath="${SSH_CTRL_SOCK}" -O exit "${REMOTE_HOST}" 2>/dev/null || true
  rm -rf "${SSH_CTRL_DIR}"
}
trap cleanup EXIT

SSH_BASE_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ControlMaster=auto -o ControlPath=${SSH_CTRL_SOCK} -o ControlPersist=300"
[[ -n "$SSH_KEY" ]] && SSH_BASE_OPTS="${SSH_BASE_OPTS} -i ${SSH_KEY}"

SSH_CMD="ssh ${SSH_BASE_OPTS} -p ${REMOTE_PORT} ${REMOTE_USER}@${REMOTE_HOST}"
SCP_CMD="scp -r ${SSH_BASE_OPTS} -P ${REMOTE_PORT}"

log "Подключение к ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PORT} (единственный ввод пароля)..."
$SSH_CMD "echo ok" > /dev/null 2>&1 || error "Не удалось подключиться к ${REMOTE_HOST}"
ok "Соединение установлено (дальнейшие шаги — без пароля)"

log "Проверка зависимостей на удалённой машине..."
$SSH_CMD bash << 'REMOTE_CHECK'
set -e
docker info > /dev/null 2>&1 || { echo "ERROR: docker daemon не запущен"; exit 1; }
if docker compose version &>/dev/null 2>&1; then
  DC_VERSION=$(docker compose version --short 2>/dev/null || docker compose version | grep -oE '[0-9]+\.[0-9]+'| head -1)
  echo "INFO: docker compose v2 (${DC_VERSION})"
elif command -v docker-compose &>/dev/null; then
  DC_VERSION=$(docker-compose version --short 2>/dev/null || docker-compose version | grep -oE '[0-9]+\.[0-9]+' | head -1)
  echo "INFO: docker-compose v1 (${DC_VERSION})"
  echo "WARN: docker-compose v1 достиг EOL. Рекомендуем обновиться до docker compose v2"
else
  echo "ERROR: не найден ни 'docker compose', ни 'docker-compose'"
  exit 1
fi
echo "ALL_OK"
REMOTE_CHECK
ok "Зависимости в порядке"

DOCKER_COMPOSE=$($SSH_CMD 'if docker compose version >/dev/null 2>&1; then echo "docker compose"; else echo "docker-compose"; fi')
log "Используем: ${DOCKER_COMPOSE}"

# ── Проверка и передача open-webui образа ─────────────────────────────────────
log "Проверка Docker-образа ${OPEN_WEBUI_IMAGE} локально..."
if ! docker image inspect "${OPEN_WEBUI_IMAGE}" >/dev/null 2>&1; then
  error "Локальный образ ${OPEN_WEBUI_IMAGE} не найден. Сначала загрузите его: docker pull ${OPEN_WEBUI_IMAGE}"
fi
LOCAL_IMAGE_ID=$(docker image inspect "${OPEN_WEBUI_IMAGE}" --format '{{.Id}}')
ok "Локальный образ найден (ID: ${LOCAL_IMAGE_ID:7:12})"

NEED_TRANSFER=true
if [[ "${FORCE_IMAGE}" == "false" ]]; then
  log "Проверка образа на ${REMOTE_HOST}..."
  REMOTE_IMAGE_ID=$($SSH_CMD "docker image inspect ${OPEN_WEBUI_IMAGE} --format '{{.Id}}' 2>/dev/null || echo 'MISSING'")
  if [[ "${REMOTE_IMAGE_ID}" == "${LOCAL_IMAGE_ID}" ]]; then
    ok "Образ на сервере актуален (ID совпадает) — передача пропущена"
    NEED_TRANSFER=false
  elif [[ "${REMOTE_IMAGE_ID}" == "MISSING" ]]; then
    log "Образ на сервере отсутствует — будет передан"
  else
    warn "Образ на сервере устарел (remote: ${REMOTE_IMAGE_ID:7:12}) — будет обновлён"
  fi
fi

if [[ "${NEED_TRANSFER}" == "true" ]]; then
  log "Передача образа ${OPEN_WEBUI_IMAGE} на ${REMOTE_HOST} (docker save | ssh docker load)..."
  docker save "${OPEN_WEBUI_IMAGE}" | $SSH_CMD 'docker load'
  ok "Образ open-webui загружен на ${REMOTE_HOST}"
fi

# ── Проверка и передача python:3.11-slim для init-контейнера ──────────────────
log "Проверка образа init-контейнера ${PYTHON_INIT_IMAGE} локально..."
if ! docker image inspect "${PYTHON_INIT_IMAGE}" >/dev/null 2>&1; then
  warn "Локальный образ ${PYTHON_INIT_IMAGE} не найден — пробуем docker pull..."
  docker pull "${PYTHON_INIT_IMAGE}" || error "Не удалось получить ${PYTHON_INIT_IMAGE}"
fi
LOCAL_PYTHON_ID=$(docker image inspect "${PYTHON_INIT_IMAGE}" --format '{{.Id}}')
ok "Образ init-контейнера найден (ID: ${LOCAL_PYTHON_ID:7:12})"

NEED_PYTHON_TRANSFER=true
if [[ "${FORCE_IMAGE}" == "false" ]]; then
  log "Проверка ${PYTHON_INIT_IMAGE} на ${REMOTE_HOST}..."
  REMOTE_PYTHON_ID=$($SSH_CMD "docker image inspect ${PYTHON_INIT_IMAGE} --format '{{.Id}}' 2>/dev/null || echo 'MISSING'")
  if [[ "${REMOTE_PYTHON_ID}" == "${LOCAL_PYTHON_ID}" ]]; then
    ok "Образ python:3.11-slim на сервере актуален — передача пропущена"
    NEED_PYTHON_TRANSFER=false
  else
    log "Образ python:3.11-slim на сервере отсутствует или устарел — будет передан"
  fi
fi

if [[ "${NEED_PYTHON_TRANSFER}" == "true" ]]; then
  log "Передача образа ${PYTHON_INIT_IMAGE} на ${REMOTE_HOST}..."
  docker save "${PYTHON_INIT_IMAGE}" | $SSH_CMD 'docker load'
  ok "Образ python:3.11-slim загружен на ${REMOTE_HOST}"
fi

# ── Конфигурация ──────────────────────────────────────────────────────────────
log "Подготовка конфигурации (ветка: ${GIT_BRANCH})..."
LOCAL_ARCHIVE="$(mktemp /tmp/open-webui-deploy-XXXXXX.tar.gz)"
git -C "${SCRIPT_DIR}" archive --format=tar.gz "${GIT_BRANCH}" -o "${LOCAL_ARCHIVE}" \
  || error "Не удалось создать архив. Убедитесь, что ветка '${GIT_BRANCH}' существует локально."
ok "Архив создан ($(du -sh "${LOCAL_ARCHIVE}" | cut -f1))"

log "Передача архива на ${REMOTE_HOST}..."
$SSH_CMD "mkdir -p ${APP_DIR}"
$SCP_CMD "${LOCAL_ARCHIVE}" "${REMOTE_USER}@${REMOTE_HOST}:${APP_DIR}/app.tar.gz"
rm -f "${LOCAL_ARCHIVE}"
ok "Архив передан"

log "Начало деплоя ветки '${GIT_BRANCH}' на ${REMOTE_HOST}:${APP_DIR}"

$SSH_CMD bash -s <<REMOTE_DEPLOY
set -euo pipefail

APP_DIR="${APP_DIR}"
DOCKER_COMPOSE="${DOCKER_COMPOSE}"
APP_PORT="${APP_PORT}"
COMPOSE_FILE="${COMPOSE_FILE}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "\${BLUE}  →\${NC} \$*"; }
ok()   { echo -e "\${GREEN}  ✓\${NC} \$*"; }
warn() { echo -e "\${YELLOW}  ⚠\${NC} \$*"; }
fail() { echo -e "\${RED}  ✗\${NC} \$*" >&2; exit 1; }

[[ "\${APP_DIR}" == \~* ]] && APP_DIR="\${HOME}/\${APP_DIR#\~/}"
DC_CMD="\${DOCKER_COMPOSE} -f \${APP_DIR}/\${COMPOSE_FILE}"

log "Распаковка архива..."
if [[ -f "\${APP_DIR}/.env" ]]; then
  cp "\${APP_DIR}/.env" "/tmp/.env.backup"
  warn "Найден существующий .env — сохранён в /tmp/.env.backup"
fi
tar -xzf "\${APP_DIR}/app.tar.gz" -C "\${APP_DIR}"
rm -f "\${APP_DIR}/app.tar.gz"
if [[ -f "/tmp/.env.backup" ]]; then
  mv "/tmp/.env.backup" "\${APP_DIR}/.env"
  ok ".env восстановлен из резервной копии"
elif [[ -f "\${APP_DIR}/.env.example" ]] && [[ ! -f "\${APP_DIR}/.env" ]]; then
  cp "\${APP_DIR}/.env.example" "\${APP_DIR}/.env"
  warn ".env создан из .env.example — заполните значения в \${APP_DIR}/.env"
fi
ok "Конфигурация распакована в \${APP_DIR}"

# Убедимся что scripts/ исполняемые
chmod +x "\${APP_DIR}/scripts/"*.py 2>/dev/null || true

PORT_IN_USE=false
if ss -tln "( sport = :\${APP_PORT} )" 2>/dev/null | grep -q LISTEN; then
  PORT_IN_USE=true
fi

if [[ "\${PORT_IN_USE}" == "true" ]]; then
  if docker ps --format '{{.Ports}}' | grep -q ":\${APP_PORT}->"; then
    warn "Порт \${APP_PORT} уже занят Docker-контейнером — будет освобождён через down"
  else
    warn "Порт \${APP_PORT} уже занят процессом вне Docker"
    PIDS=\$(lsof -i :\${APP_PORT} -sTCP:LISTEN -t 2>/dev/null || true)
    if [[ -n "\${PIDS}" ]]; then
      warn "PID: \${PIDS}"
    fi
    ANSWER=""
    exec 3</dev/tty
    echo -en "\${YELLOW}Завершить процесс и продолжить деплой? [y/N]: \${NC}"
    read -r ANSWER <&3
    exec 3<&-
    if [[ "\${ANSWER}" =~ ^[Yy]$ ]]; then
      if [[ -n "\${PIDS}" ]]; then
        kill \${PIDS} 2>/dev/null && ok "Процесс \${PIDS} завершён"
      else
        fuser -k \${APP_PORT}/tcp 2>/dev/null && ok "Порт \${APP_PORT} освобождён"
      fi
      sleep 1
    else
      fail "Отмена деплоя. Освободите порт \${APP_PORT} вручную и запустите deploy.sh снова"
    fi
  fi
fi

if docker ps -a --filter "name=open-webui" --format '{{.Names}}' 2>/dev/null | grep -q .; then
  log "Остановка предыдущего стека..."
  eval "\${DC_CMD} down --remove-orphans" || true
  ok "Стек остановлен"
else
  log "Запущенных контейнеров не найдено — первый запуск"
fi

log "Запуск стека через \${COMPOSE_FILE}..."
eval "\${DC_CMD} up -d --no-build"
ok "Стек запущен"

# ── Healthcheck: ждём готовности Open WebUI по /health ────────────────────────
log "Ожидание готовности Open WebUI (max 300 сек)..."
MAX_WAIT=300
ELAPSED=0
HEALTHY=false
START_TS=\$(date +%s)

while [[ \$ELAPSED -lt \$MAX_WAIT ]]; do
  CONTAINER_STATUS=\$(docker inspect open-webui --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  if [[ "\${CONTAINER_STATUS}" == "exited" || "\${CONTAINER_STATUS}" == "dead" || "\${CONTAINER_STATUS}" == "missing" ]]; then
    echo ""
    warn "Контейнер open-webui остановился со статусом '\${CONTAINER_STATUS}'"
    warn "Последние логи:"
    docker logs open-webui --tail=40 2>&1 || true
    fail "Деплой прерван — контейнер упал"
  fi

  HEALTH_RESPONSE=\$(curl -sf --max-time 3 "http://localhost:\${APP_PORT}/health" 2>/dev/null || echo "")
  if echo "\${HEALTH_RESPONSE}" | grep -q '"status":.*true'; then
    HEALTHY=true
    ELAPSED=\$(( \$(date +%s) - START_TS ))
    break
  fi

  if (( ELAPSED % 30 == 0 && ELAPSED > 0 )); then
    CONTAINER_DETAIL=\$(docker inspect open-webui --format '{{.State.Status}} ({{.State.Health.Status}})' 2>/dev/null || echo "?")
    echo -e "\n  ⏳ \${ELAPSED}с — контейнер: \${CONTAINER_DETAIL}, /health: \${HEALTH_RESPONSE:-нет ответа}"
  else
    printf "."
  fi
  sleep 3
  ELAPSED=\$(( \$(date +%s) - START_TS ))
done
echo ""

if [[ "\$HEALTHY" != "true" ]]; then
  warn "Open WebUI /health не вернул {status: true} за \${MAX_WAIT} сек."
  echo ""
  warn "Статус контейнера:"
  docker inspect open-webui --format 'Status: {{.State.Status}}  ExitCode: {{.State.ExitCode}}' 2>/dev/null || true
  echo ""
  warn "Последние логи контейнера:"
  docker logs open-webui --tail=60 2>&1 || true
  exit 1
fi

ok "Open WebUI готов за \${ELAPSED} сек"

# ── Ждём завершения init-контейнера ───────────────────────────────────────────
log "Ожидание завершения init-контейнера (admin + pipe function + MCP)..."
INIT_MAX_WAIT=300
INIT_ELAPSED=0
INIT_START=\$(date +%s)

while [[ \$INIT_ELAPSED -lt \$INIT_MAX_WAIT ]]; do
  INIT_STATUS=\$(docker inspect open-webui-init --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  if [[ "\${INIT_STATUS}" == "exited" ]]; then
    INIT_EXIT_CODE=\$(docker inspect open-webui-init --format '{{.State.ExitCode}}' 2>/dev/null || echo "1")
    if [[ "\${INIT_EXIT_CODE}" == "0" ]]; then
      ok "Init-контейнер завершился успешно"
    else
      warn "Init-контейнер завершился с кодом \${INIT_EXIT_CODE}"
    fi
    break
  fi
  if [[ "\${INIT_STATUS}" == "missing" ]]; then
    warn "Init-контейнер не найден — пропускаем ожидание"
    break
  fi
  printf "."
  sleep 3
  INIT_ELAPSED=\$(( \$(date +%s) - INIT_START ))
done
echo ""

log "Логи init-контейнера:"
docker logs open-webui-init 2>&1 || true

echo ""
eval "\${DC_CMD} ps"
echo ""
ok "Деплой завершён успешно"
SERVER_IP=\$(hostname -I | awk '{print \$1}')
echo -e "\${GREEN}  Open WebUI: http://\${SERVER_IP}:\${APP_PORT}/\${NC}"
echo -e "\${GREEN}  Логин: bugbusters@cbr.ru\${NC}"
REMOTE_DEPLOY

ok "Деплой на ${REMOTE_HOST} завершён"
