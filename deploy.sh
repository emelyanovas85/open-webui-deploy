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
#   --help          Показать справку
#
# Примеры:
#   ./deploy.sh
#   ./deploy.sh -h 192.168.1.100 -u deploy
#   ./deploy.sh -i ~/.ssh/id_rsa -b feature/new-config
#
# Примечание: удалённый хост не имеет доступа в интернет.
# Docker-образы (open-webui, ollama) должны быть предварительно
# загружены на сервер вручную или через docker save | ssh docker load.
# Скрипт копирует docker-compose.yml, .env и запускает стек.
# =============================================================================

set -euo pipefail

# ── Цвета ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

log()   { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
ok()    { echo -e "${GREEN}[$(date '+%H:%M:%S')] \u2713${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date '+%H:%M:%S')] \u26a0${NC} $*"; }
error() { echo -e "${RED}[$(date '+%H:%M:%S')] \u2717${NC} $*" >&2; exit 1; }

# ── Корень локального репозитория ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Значения по умолчанию ─────────────────────────────────────────────────────
REMOTE_HOST="10.1.5.97"
REMOTE_USER="svc-local-adm"
REMOTE_PORT="22"
SSH_KEY=""
GIT_BRANCH="main"
APP_DIR="~/open-webui-deploy"
APP_PORT="8087"

# ── Разбор аргументов ─────────────────────────────────────────────────────────
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
    --help)         usage ;;
    *) error "Неизвестный аргумент: $1. Используйте --help для справки." ;;
  esac
done

# ── SSH ControlMaster: одно подключение — один ввод пароля ────────────────────
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

# ── Первое подключение (ввод пароля) ──────────────────────────────────────────
log "Подключение к ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PORT} (единственный ввод пароля)..."
$SSH_CMD "echo ok" > /dev/null 2>&1 || error "Не удалось подключиться к ${REMOTE_HOST}"
ok "Соединение установлено (дальнейшие шаги — без пароля)"

# ── Проверка зависимостей ─────────────────────────────────────────────────────
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

# ── Передача файлов конфигурации на сервер ────────────────────────────────────
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

# ── Деплой на сервере ─────────────────────────────────────────────────────────
log "Начало деплоя ветки '${GIT_BRANCH}' на ${REMOTE_HOST}:${APP_DIR}"

$SSH_CMD bash -s <<REMOTE_DEPLOY
set -euo pipefail

APP_DIR="${APP_DIR}"
DOCKER_COMPOSE="${DOCKER_COMPOSE}"
APP_PORT="${APP_PORT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "\${BLUE}  \u2192\${NC} \$*"; }
ok()   { echo -e "\${GREEN}  \u2713\${NC} \$*"; }
warn() { echo -e "\${YELLOW}  \u26a0\${NC} \$*"; }
fail() { echo -e "\${RED}  \u2717\${NC} \$*" >&2; exit 1; }

[[ "\${APP_DIR}" == \~* ]] && APP_DIR="\${HOME}/\${APP_DIR#\~/}"

# 1. Распаковка архива (сохраняем .env если уже есть)
log "Распаковка архива..."
if [[ -f "\${APP_DIR}/.env" ]]; then
  cp "\${APP_DIR}/.env" "/tmp/.env.backup"
  warn "Найден существующий .env — сохранён в /tmp/.env.backup"
fi
tar -xzf "\${APP_DIR}/app.tar.gz" -C "\${APP_DIR}"
rm -f "\${APP_DIR}/app.tar.gz"
# Восстанавливаем .env если он был (не затираем секреты)
if [[ -f "/tmp/.env.backup" ]]; then
  mv "/tmp/.env.backup" "\${APP_DIR}/.env"
  ok ".env восстановлен из резервной копии"
elif [[ -f "\${APP_DIR}/.env.example" ]] && [[ ! -f "\${APP_DIR}/.env" ]]; then
  cp "\${APP_DIR}/.env.example" "\${APP_DIR}/.env"
  warn ".env создан из .env.example — заполните значения в \${APP_DIR}/.env"
fi
ok "Конфигурация распакована в \${APP_DIR}"

cd "\${APP_DIR}"

# 2. Проверка занятого порта не-docker процессом
PORT_IN_USE=false
if ss -tln "( sport = :\${APP_PORT} )" 2>/dev/null | grep -q LISTEN; then
  PORT_IN_USE=true
fi

if [[ "\${PORT_IN_USE}" == "true" ]]; then
  if docker ps --format '{{.Ports}}' | grep -q ":\${APP_PORT}->"; then
    warn "Порт \${APP_PORT} уже занят Docker-контейнером — будет освобождён через docker compose down"
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

# 3. Перезапуск стека
if docker ps --filter "name=open-webui" --format '{{.Names}}' 2>/dev/null | grep -q .; then
  log "Остановка предыдущего стека..."
  eval "\${DOCKER_COMPOSE} down --remove-orphans"
  ok "Стек остановлен"
else
  log "Запущенных контейнеров не найдено — первый запуск"
fi

log "Запуск стека (open-webui + ollama)..."
eval "\${DOCKER_COMPOSE} up -d"
ok "Стек запущен"

# 4. Ожидание готовности Open WebUI
log "Ожидание готовности Open WebUI (max 120 сек)..."
MAX_WAIT=120
ELAPSED=0
HEALTHY=false
while [[ \$ELAPSED -lt \$MAX_WAIT ]]; do
  if curl -sf "http://localhost:\${APP_PORT}/" > /dev/null 2>&1; then
    HEALTHY=true
    break
  fi
  printf "."
  sleep 3
  ELAPSED=\$((ELAPSED + 3))
done
echo ""
if [[ "\$HEALTHY" != "true" ]]; then
  warn "Open WebUI не ответил за \${MAX_WAIT} сек. Последние логи:"
  eval "\${DOCKER_COMPOSE} logs --tail=50"
  exit 1
fi
ok "Open WebUI готов (\${ELAPSED} сек)"

echo ""
eval "\${DOCKER_COMPOSE} ps"
echo ""
ok "Деплой завершён успешно"
SERVER_IP=\$(hostname -I | awk '{print \$1}')
echo -e "\${GREEN}  Open WebUI:   http://\${SERVER_IP}:\${APP_PORT}/\${NC}"
echo -e "\${GREEN}  Ollama API:   http://\${SERVER_IP}:11434/\${NC}"

REMOTE_DEPLOY

ok "Деплой на ${REMOTE_HOST} завершён"
