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
#   --proxy         HTTP/HTTPS прокси для docker pull (напр.: http://proxy:3128)
#   --help          Показать справку
#
# Примеры:
#   ./deploy.sh
#   ./deploy.sh -h 192.168.1.100 -u deploy
#   ./deploy.sh -i ~/.ssh/id_rsa -b feature/new-config
#   ./deploy.sh --force-image
#   ./deploy.sh --proxy http://proxy.example.com:3128
#
# Примечание: удалённый хост не имеет доступа в интернет.
# Скрипт сравнивает Image ID локально и на сервере — если совпадают,
# передача пропускается. Иначе передаёт образ через docker save | ssh docker load.
# .env всегда берётся из git (редактируйте его локально и деплойте).
# Контейнер пересоздаётся автоматически если изменился .env или docker-compose.yml.
# Данные Open WebUI (чаты, пользователи, настройки) полностью сбрасываются
# при каждом деплое (удаление volume webui-data).
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

log()   { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()    { echo -e "${GREEN}[$(date '+%H:%M:%S')] \u2713${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date '+%H:%M:%S')] \u26a0${NC} $*"; }
error() { echo -e "${RED}[$(date '+%H:%M:%S')] \u2717${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REMOTE_HOST="10.1.5.97"
REMOTE_USER="svc-local-adm"
REMOTE_PORT="22"
SSH_KEY=""
GIT_BRANCH="main"
APP_DIR="~/open-webui-deploy"
APP_PORT="8087"
COMPOSE_FILE="docker-compose.yml"
OPEN_WEBUI_IMAGE="ghcr.io/open-webui/open-webui:v0.9.6"
PATCHED_IMAGE="open-webui-patched:v0.9.6"
INIT_IMAGE="open-webui-init:latest"
FORCE_IMAGE=false
HTTPS_PROXY_URL=""

usage() {
  grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,2\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--host)      REMOTE_HOST="$2";       shift 2 ;;
    -u|--user)      REMOTE_USER="$2";       shift 2 ;;
    -p|--port)      REMOTE_PORT="$2";       shift 2 ;;
    -i|--identity)  SSH_KEY="$2";           shift 2 ;;
    -b|--branch)    GIT_BRANCH="$2";        shift 2 ;;
    --force-image)  FORCE_IMAGE=true;       shift   ;;
    --proxy)        HTTPS_PROXY_URL="$2";   shift 2 ;;
    --help)         usage ;;
    *) error "Неизвестный аргумент: $1. Используйте --help для справки." ;;
  esac
done

[[ -z "${HTTPS_PROXY_URL}" && -n "${HTTPS_PROXY:-}" ]] && HTTPS_PROXY_URL="${HTTPS_PROXY}"
[[ -z "${HTTPS_PROXY_URL}" && -n "${https_proxy:-}" ]] && HTTPS_PROXY_URL="${https_proxy}"

docker_pull() {
  local image="$1"
  if [[ -n "${HTTPS_PROXY_URL}" ]]; then
    log "Используем прокси: ${HTTPS_PROXY_URL}"
    HTTPS_PROXY="${HTTPS_PROXY_URL}" HTTP_PROXY="${HTTPS_PROXY_URL}" docker pull "${image}"
  else
    docker pull "${image}"
  fi
}

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

# ── Вычисляем хэш конфигурации (.env + docker-compose.yml) локально ────────────────────
log "Вычисление хэша конфигурации (.env + docker-compose.yml)..."
LOCAL_CONFIG_HASH=$(
  git -C "${SCRIPT_DIR}" show "${GIT_BRANCH}:.env" 2>/dev/null
  git -C "${SCRIPT_DIR}" show "${GIT_BRANCH}:docker-compose.yml" 2>/dev/null
)
LOCAL_CONFIG_HASH=$(echo "${LOCAL_CONFIG_HASH}" | sha256sum | cut -d' ' -f1)
ok "Локальный хэш: ${LOCAL_CONFIG_HASH:0:12}..."

# Получаем хэш с сервера
REMOTE_CONFIG_HASH=$($SSH_CMD "cat ~/open-webui-deploy/.config-hash 2>/dev/null || echo 'MISSING'")

NEED_RECREATE=false
if [[ "${REMOTE_CONFIG_HASH}" == "${LOCAL_CONFIG_HASH}" ]]; then
  ok "Конфигурация не изменилась — пересоздание контейнера не нужно"
else
  if [[ "${REMOTE_CONFIG_HASH}" == "MISSING" ]]; then
    warn "Хэш конфигурации на сервере не найден — первый деплой или обновление"
  else
    warn "Конфигурация изменилась (.env или docker-compose.yml) — контейнер будет пересоздан"
  fi
  NEED_RECREATE=true
fi

# ── Проверка базового образа open-webui ─────────────────────────────────────────────
log "Проверка базового образа ${OPEN_WEBUI_IMAGE} локально..."
if ! docker image inspect "${OPEN_WEBUI_IMAGE}" >/dev/null 2>&1; then
  warn "Образ ${OPEN_WEBUI_IMAGE} не найден локально — запускаем docker pull..."
  docker_pull "${OPEN_WEBUI_IMAGE}" || error "Не удалось загрузить ${OPEN_WEBUI_IMAGE}."
fi
ok "Базовый образ найден локально"

# ── Сборка патченого образа open-webui-patched локально ──────────────────────────
log "Сборка патченого образа ${PATCHED_IMAGE} из Dockerfile..."
docker build -t "${PATCHED_IMAGE}" "${SCRIPT_DIR}" \
  || error "Не удалось собрать ${PATCHED_IMAGE}"
LOCAL_PATCHED_ID=$(docker image inspect "${PATCHED_IMAGE}" --format '{{.Id}}')
ok "Образ ${PATCHED_IMAGE} собран (ID: ${LOCAL_PATCHED_ID:7:12})"

NEED_PATCHED_TRANSFER=true
if [[ "${FORCE_IMAGE}" == "false" ]]; then
  log "Проверка ${PATCHED_IMAGE} на ${REMOTE_HOST}..."
  REMOTE_PATCHED_ID=$($SSH_CMD "docker image inspect ${PATCHED_IMAGE} --format '{{.Id}}' 2>/dev/null || echo 'MISSING'")
  if [[ "${REMOTE_PATCHED_ID}" == "${LOCAL_PATCHED_ID}" ]]; then
    ok "Образ ${PATCHED_IMAGE} на сервере актуален — передача пропущена"
    NEED_PATCHED_TRANSFER=false
  else
    log "Образ ${PATCHED_IMAGE} на сервере отсутствует или устарел — будет передан"
    NEED_RECREATE=true
  fi
fi

if [[ "${NEED_PATCHED_TRANSFER}" == "true" ]]; then
  log "Передача образа ${PATCHED_IMAGE} на ${REMOTE_HOST}..."
  docker save "${PATCHED_IMAGE}" | $SSH_CMD 'docker load'
  ok "Образ ${PATCHED_IMAGE} загружен на ${REMOTE_HOST}"
fi

# ── Сборка и передача open-webui-init образа ─────────────────────────────────────────────
log "Сборка образа ${INIT_IMAGE} локально из scripts/Dockerfile..."
docker build --no-cache -t "${INIT_IMAGE}" "${SCRIPT_DIR}/scripts" \
  || error "Не удалось собрать образ ${INIT_IMAGE}"
LOCAL_INIT_ID=$(docker image inspect "${INIT_IMAGE}" --format '{{.Id}}')
ok "Образ ${INIT_IMAGE} собран (ID: ${LOCAL_INIT_ID:7:12})"

NEED_INIT_TRANSFER=true
if [[ "${FORCE_IMAGE}" == "false" ]]; then
  log "Проверка ${INIT_IMAGE} на ${REMOTE_HOST}..."
  REMOTE_INIT_ID=$($SSH_CMD "docker image inspect ${INIT_IMAGE} --format '{{.Id}}' 2>/dev/null || echo 'MISSING'")
  if [[ "${REMOTE_INIT_ID}" == "${LOCAL_INIT_ID}" ]]; then
    ok "Образ ${INIT_IMAGE} на сервере актуален — передача пропущена"
    NEED_INIT_TRANSFER=false
  else
    log "Образ ${INIT_IMAGE} на сервере отсутствует или устарел — будет передан"
  fi
fi

if [[ "${NEED_INIT_TRANSFER}" == "true" ]]; then
  log "Передача образа ${INIT_IMAGE} на ${REMOTE_HOST}..."
  docker save "${INIT_IMAGE}" | $SSH_CMD 'docker load'
  ok "Образ ${INIT_IMAGE} загружен на ${REMOTE_HOST}"
fi

# ── Конфигурация ──────────────────────────────────────────────────────────────────────────────────────
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
NEED_RECREATE="${NEED_RECREATE}"
LOCAL_CONFIG_HASH="${LOCAL_CONFIG_HASH}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "\${BLUE}  \u2192\${NC} \$*"; }
ok()   { echo -e "\${GREEN}  \u2713\${NC} \$*"; }
warn() { echo -e "\${YELLOW}  \u26a0\${NC} \$*"; }
fail() { echo -e "\${RED}  \u2717\${NC} \$*" >&2; exit 1; }

[[ "\${APP_DIR}" == \~* ]] && APP_DIR="\${HOME}/\${APP_DIR#\~/}"
DC_CMD="\${DOCKER_COMPOSE} -f \${APP_DIR}/\${COMPOSE_FILE}"

log "Распаковка архива..."
tar -xzf "\${APP_DIR}/app.tar.gz" -C "\${APP_DIR}"
rm -f "\${APP_DIR}/app.tar.gz"
ok "Конфигурация распакована в \${APP_DIR} (.env взят из git)"

chmod +x "\${APP_DIR}/scripts/"*.py 2>/dev/null || true

# ── Установка no_proxy в /etc/profile.d/ (обход глобального системного прокси) ───────────
if sudo cp "\${APP_DIR}/scripts/no_proxy.sh" /etc/profile.d/no_proxy.sh 2>/dev/null; then
  sudo chmod 644 /etc/profile.d/no_proxy.sh
  source /etc/profile.d/no_proxy.sh
  ok "no_proxy установлен в /etc/profile.d/ и применён для текущей сессии"
else
  warn "sudo недоступен — устанавливаем no_proxy только для текущей сессии"
  source "\${APP_DIR}/scripts/no_proxy.sh" 2>/dev/null || true
fi

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
    if [[ "\${ANSWER}" =~ ^[Yy]\$ ]]; then
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

# ── Полная остановка и гарантированное удаление всех данных ────────────────────
log "Остановка стека и удаление данных (чистый деплой)..."

eval "\${DC_CMD} down --remove-orphans --volumes" 2>/dev/null || true
docker rm -f open-webui open-webui-init 2>/dev/null || true

FOUND_VOLUMES=\$(docker volume ls --format '{{.Name}}' | grep -E '(^|_)webui-data\$' || true)
if [[ -n "\${FOUND_VOLUMES}" ]]; then
  for VOL in \${FOUND_VOLUMES}; do
    docker volume rm "\${VOL}" 2>/dev/null && ok "Volume \${VOL} удалён" || warn "Не удалось удалить volume \${VOL} (занят?)"
  done
else
  ok "Вольюм webui-data не найден — первый деплой"
fi
ok "Старые данные очищены"

# ── Запуск open-webui ───────────────────────────────────────────────────────────────────────────
if [[ "\${NEED_RECREATE}" == "true" ]]; then
  log "Пересоздание контейнера (конфигурация изменилась)..."
  eval "\${DC_CMD} up -d --no-build --force-recreate open-webui"
else
  log "Запуск open-webui (конфигурация не изменилась)..."
  eval "\${DC_CMD} up -d --no-build open-webui"
fi
ok "open-webui запущен"

# ── Сохраняем хэш конфигурации ──────────────────────────────────────────────────────────────────
echo "\${LOCAL_CONFIG_HASH}" > "\${APP_DIR}/.config-hash"
ok "Хэш конфигурации сохранён: \${LOCAL_CONFIG_HASH:0:12}..."

# ── Ждём готовности Open WebUI ──────────────────────────────────────────────────────────────
log "Ожидание готовности Open WebUI (max 300 сек)..."
MAX_WAIT=300
HEALTHY=false
START_TS=\$(date +%s)

while true; do
  ELAPSED=\$(( \$(date +%s) - START_TS ))
  [[ \$ELAPSED -ge \$MAX_WAIT ]] && break

  CONTAINER_STATUS=\$(docker inspect open-webui --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  if [[ "\${CONTAINER_STATUS}" == "exited" || "\${CONTAINER_STATUS}" == "dead" || "\${CONTAINER_STATUS}" == "missing" ]]; then
    echo ""
    warn "Контейнер open-webui остановился (статус: \${CONTAINER_STATUS})"
    docker logs open-webui --tail=40 2>&1 || true
    fail "Деплой прерван — контейнер упал"
  fi

  HEALTH_RESPONSE=\$(wget -qO- --no-proxy "http://localhost:\${APP_PORT}/health" 2>/dev/null || echo "")
  if echo "\${HEALTH_RESPONSE}" | grep -q '"status":true'; then
    HEALTHY=true
    ELAPSED=\$(( \$(date +%s) - START_TS ))
    break
  fi

  if (( ELAPSED % 30 == 0 && ELAPSED > 0 )); then
    echo -e "\n  \u23f3 \${ELAPSED}\u0441 — /health: \${HEALTH_RESPONSE:-нет ответа}"
  else
    printf "."
  fi
  sleep 3
done
echo ""

if [[ "\$HEALTHY" != "true" ]]; then
  warn "Open WebUI не ответил за \${MAX_WAIT} сек."
  docker logs open-webui --tail=60 2>&1 || true
  exit 1
fi
ok "Open WebUI готов за \${ELAPSED} сек"

# ── Проверка доступности MCP-серверов (через curl --noproxy) ──────────────────────────────
MCP_SERVERS=("localhost 8086" "localhost 8083")
MCP_MAX_WAIT=60
MCP_INIT_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1.0"}}}'
log "Проверка доступности MCP-серверов (max \${MCP_MAX_WAIT} сек)..."

for MCP_ENTRY in "\${MCP_SERVERS[@]}"; do
  MCP_HOST=\$(echo "\${MCP_ENTRY}" | cut -d' ' -f1)
  MCP_PORT=\$(echo "\${MCP_ENTRY}" | cut -d' ' -f2)
  MCP_START=\$(date +%s)
  MCP_READY=false
  printf "  Проверка \${MCP_HOST}:\${MCP_PORT} "
  while true; do
    MCP_ELAPSED=\$(( \$(date +%s) - MCP_START ))
    [[ \$MCP_ELAPSED -ge \$MCP_MAX_WAIT ]] && break
    HTTP_CODE=\$(curl -s -o /dev/null -w '%{http_code}' --noproxy '\${MCP_HOST}' \
      -X POST "http://\${MCP_HOST}:\${MCP_PORT}/mcp" \
      -H 'Content-Type: application/json' \
      -H 'Accept: text/event-stream, application/json' \
      --max-time 3 \
      -d "\${MCP_INIT_PAYLOAD}" 2>/dev/null || echo '000')
    if [[ "\${HTTP_CODE}" =~ ^(200|202|307)\$ ]]; then
      MCP_READY=true
      break
    fi
    printf "."
    sleep 2
  done
  echo ""
  if [[ "\${MCP_READY}" == "true" ]]; then
    ok "MCP-сервер отвечает: \${MCP_HOST}:\${MCP_PORT}"
  else
    warn "MCP-сервер недоступен: \${MCP_HOST}:\${MCP_PORT} — продолжаем без него"
  fi
done

log "Пауза 5 сек для переподключения Open WebUI к MCP-серверам..."
sleep 5

# ── Запуск init-контейнера ──────────────────────────────────────────────────────────────────────────
log "Запуск init-контейнера (admin + pipe function + MCP)..."
docker rm -f open-webui-init 2>/dev/null || true
INIT_EXIT=0
eval "\${DC_CMD} run --name open-webui-init open-webui-init" || INIT_EXIT=\$?

echo ""
echo "═════ ЛОГИ init-контейнера ═════"
docker logs open-webui-init 2>&1 || true
echo "════════════════════════════════"

if [[ "\${INIT_EXIT}" == "0" ]]; then
  ok "Init-контейнер завершился успешно"
else
  warn "Init-контейнер завершился с ошибкой (exit code: \${INIT_EXIT})"
fi

echo ""
eval "\${DC_CMD} ps"
echo ""
ok "Деплой завершён успешно"
SERVER_IP=\$(hostname -I | awk '{print \$1}')
echo -e "\${GREEN}  Open WebUI: http://\${SERVER_IP}:\${APP_PORT}/\${NC}"
REMOTE_DEPLOY

ok "Деплой на ${REMOTE_HOST} завершён"
