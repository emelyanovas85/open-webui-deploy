# Open WebUI Deploy

Деплой [Open WebUI](https://github.com/open-webui/open-webui) на порту **8087** с автоматической настройкой LLM, Pipe Function, MCP Tool Server и учётной записи администратора.

## Стек

| Сервис | Порт | Описание |
|--------|------|----------|
| Open WebUI | **8087** | Web-интерфейс для LLM |
| open-webui-init | — | Init-контейнер: создаёт admin, pipe function, MCP |
| Ollama | 11434 | Локальные LLM (профиль `with-ollama`) |
| Webhook Handler | 8080 | MR_Checker (профиль `with-webhook`) |

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/emelyanovas85/open-webui-deploy.git
cd open-webui-deploy
```

### 2. Запустить деплой

Используйте `deploy.sh` — он собирает образы локально, передаёт на сервер и запускает стек.

```bash
# Базовый запуск (токен будет запрошен интерактивно)
./deploy.sh

# С явной передачей токена GitLab MCP
./deploy.sh --gitlab-token glpat-xxxxxxxxxxxxxxxxxxxx

# Указать конкретную ветку
./deploy.sh --gitlab-token glpat-xxx -b feature/my-branch

# Принудительно переслать образ (если Image ID совпадает, но хочется обновить)
./deploy.sh --gitlab-token glpat-xxx --force-image
```

> **Токен GitLab** нужен для `REMOTE_AUTHORIZATION=true` на GitLab MCP сервере.  
> Получить: GitLab → User Settings → Access Tokens → Create (scope: `api`, `read_api`)  
> Токен **не хранится в git** — подставляется в `.env` только в момент деплоя.

### 3. Открыть в браузере

```
http://10.1.5.97:8087
```

Войти: `bugbusters@cbr.ru` / `bugbusters`

## Конфигурация (.env)

Файл `.env` в git содержит все настройки **кроме токенов**. Ключевые переменные:

```env
# LLM
OPENAI_API_KEY=sk-...
OPENAI_API_BASE_URL=https://chat.ehd-zr.cbr.ru/openai
DEFAULT_MODELS=Qwen/Qwen3.5-397B-A17B-GPTQ-Int4

# Администратор (создаётся автоматически init-контейнером)
WEBUI_ADMIN_EMAIL=bugbusters@cbr.ru
WEBUI_ADMIN_PASSWORD=bugbusters

# MCP Tool Servers (формат: url::name::desc,url2::name2::desc2)
MCP_SERVER_URLS=http://10.1.5.97:8086/mcp::Java MCP::Java Class Context MCP,http://10.1.5.97:8083/mcp::GitLab MCP::GitLab MCP

# Bearer-токены для MCP серверов с REMOTE_AUTHORIZATION=true
# Формат: url::token,url2::token2
# GITLAB_MCP_TOKEN подставляется deploy.sh через --gitlab-token (не хранить в git!)
GITLAB_MCP_TOKEN=
MCP_BEARER_TOKENS=http://10.1.5.97:8083/mcp::${GITLAB_MCP_TOKEN}

# Прокси-исключения (обязательно для корпоративной сети ЦБ)
NO_PROXY=localhost,127.0.0.1,10.1.5.97
```

## MCP Tool Servers

### Java MCP (порт 8086)

Сервер структурного анализа Java-классов по GitLab MR.

| Параметр | Значение |
|----------|----------|
| Транспорт | Streamable HTTP |
| URL | `http://10.1.5.97:8086/mcp` |
| Авторизация | не требуется |

### GitLab MCP (порт 8083)

Сервер [zereight/gitlab-mcp](https://github.com/zereight/gitlab-mcp) — полный доступ к GitLab API через MCP.

| Параметр | Значение |
|----------|----------|
| Транспорт | Streamable HTTP (supergateway `--stateful`) |
| URL | `http://10.1.5.97:8083/mcp` |
| Авторизация | `REMOTE_AUTHORIZATION=true` — Bearer токен через `MCP_BEARER_TOKENS` |

Чтобы развернуть / обновить GitLab MCP сервер из исходников:

```bash
# Из репозитория MR_Checker, ветка feature/zereight-gitlab-mcp
./deploy-gitlab-mcp.sh --build-from-source --token glpat-xxxxxxxxxxxxxxxxxxxx

# С указанием конкретного коммита или тега
./deploy-gitlab-mcp.sh --build-from-source --mcp-ref v2.1.28 --token glpat-xxx
```

Проверить доступность MCP-серверов:

```bash
# Java MCP
curl --noproxy '*' -X POST http://localhost:8086/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# GitLab MCP (с токеном)
curl --noproxy '*' -X POST http://localhost:8083/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer glpat-xxxxxxxxxxxxxxxxxxxx' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

## Подключение к LLM (CBR Models)

Модели доступны через **Pipe Function** внутри Open WebUI. Особенности:

- Обходит корпоративный прокси Squid через `NO_PROXY`
- Решает SSL handshake failure (`SECLEVEL=0`, TLS 1.2)
- Поддерживает **streaming** (ответ печатается по словам)
- Автоматически вызывает MCP инструменты при наличии tool_calls в ответе LLM

Pipe Function хранится в `scripts/init-openwebui.py` и автоматически создаётся/обновляется при каждом деплое.

## Что сохраняется после рестарта

| Данные | Механизм сохранения |
|--------|--------------------|
| Чаты, настройки UI | Docker volume `open-webui-data` |
| Учётная запись admin | `WEBUI_ADMIN_*` в `.env` + init-скрипт |
| Pipe Function CBR Models | init-скрипт (upsert при каждом деплое) |
| MCP Tool Servers | init-скрипт (idempotent, не дублирует) |
| NO_PROXY | `environment` в `docker-compose.yml` |

> ⚠️ Данные Open WebUI **полностью сбрасываются** при каждом деплое через `deploy.sh` (удаление volume `webui-data`). Это намеренно для чистого деплоя.

## Управление

```bash
# Статус
docker compose ps

# Логи Open WebUI
docker compose logs -f open-webui

# Логи init-скрипта
docker compose logs open-webui-init

# Перезапустить только init (например после изменений в .env)
docker compose run --rm open-webui-init

# Остановить
docker compose down

# Удалить данные (осторожно!)
docker compose down -v
```

## Структура проекта

```
open-webui-deploy/
├── deploy.sh                # Полный деплой: сборка образов + передача + запуск
├── docker-compose.yml       # Основная конфигурация
├── Dockerfile               # Патч open-webui (обход SSL, proxy)
├── .env                     # Переменные окружения (без секретных токенов!)
├── .env.example             # Пример для новых установок
├── scripts/
│   ├── Dockerfile           # Образ для init-контейнера
│   └── init-openwebui.py    # Init-скрипт: admin + pipe function + MCP Tool Servers
├── .gitignore
└── README.md
```

## Профили

```bash
# С Ollama (локальные модели)
docker compose --profile with-ollama up -d

# С Webhook Handler (MR_Checker)
docker compose --profile with-webhook up -d

# Всё вместе
docker compose --profile with-ollama --profile with-webhook up -d
```
