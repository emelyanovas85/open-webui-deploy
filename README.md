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

### 2. Проверить `.env`

Файл `.env` уже заполнен. При необходимости отредактировать:

```bash
nano .env
```

Ключевые переменные:

```env
# LLM
OPENAI_API_KEY=sk-...
OPENAI_API_BASE_URL=https://chat.ehd-zr.cbr.ru/openai
DEFAULT_MODELS=Qwen/Qwen3.5-397B-A17B-GPTQ-Int4

# Администратор (создаётся автоматически)
WEBUI_ADMIN_EMAIL=bugbusters@cbr.ru
WEBUI_ADMIN_PASSWORD=bugbusters

# MCP Tool Server
MCP_SERVER_URL=http://10.1.5.97:8086/sse

# Прокси-исключения (обязательно для корпоративной сети ЦБ)
NO_PROXY=localhost,127.0.0.1,10.1.5.97
```

### 3. Запустить

```bash
docker compose up -d
```

При первом старте автоматически выполняется `open-webui-init`, который:
- создаёт учётную запись администратора (`bugbusters@cbr.ru`)
- регистрирует Pipe Function **Qwen3.5 CBR** (с SSL-обходом и streaming)
- подключает MCP Tool Server `java-class-context-mcp` по SSE

### 4. Открыть в браузере

```
http://10.1.5.97:8087
```

Войти: `bugbusters@cbr.ru` / `bugbusters`

## Подключение к LLM (Qwen3.5 CBR)

Модель доступна через **Pipe Function** внутри Open WebUI. Особенности:

- Обходит корпоративный прокси Squid через `NO_PROXY`
- Решает SSL handshake failure (`SECLEVEL=0`, TLS 1.2)
- Поддерживает **streaming** (ответ печатается по словам)

Pipe Function хранится в `scripts/init-openwebui.py` и автоматически создаётся при каждом `docker compose up`.

## MCP Tool Server (java-class-context-mcp)

Подключение к [java-class-context-mcp](https://github.com/emelyanovas85/java-class-context-deploy/tree/main/mcp-wrapper) — MCP-сервер для структурного анализа Java-классов по GitLab MR.

| Параметр | Значение |
|----------|----------|
| Транспорт | SSE |
| URL | `http://10.1.5.97:8086/sse` |
| Endpoint сообщений | `/mcp/message` |

Сервер должен быть запущен до старта Open WebUI. Init-скрипт автоматически регистрирует его как Tool Server.

Проверить доступность:

```bash
curl --noproxy '*' http://localhost:8086/sse
```

## Что сохраняется после рестарта

| Данные | Механизм сохранения |
|--------|--------------------|
| Чаты, настройки UI | Docker volume `open-webui-data` |
| Учётная запись admin | `WEBUI_ADMIN_*` в `.env` + init-скрипт |
| Pipe Function Qwen3.5 | init-скрипт (upsert при каждом старте) |
| MCP Tool Server | init-скрипт (idempotent, не дублирует) |
| NO_PROXY | `environment` в `docker-compose.yml` |

## Управление

```bash
# Запустить
docker compose up -d

# Статус
docker compose ps

# Логи Open WebUI
docker compose logs -f open-webui

# Логи init-скрипта
docker compose logs open-webui-init

# Перезапустить только init (например после изменений в .env)
docker compose run --rm open-webui-init bash -c \
  "pip install requests -q && sleep 5 && python /scripts/init-openwebui.py"

# Остановить
docker compose down

# Удалить данные (осторожно!)
docker compose down -v
```

## Структура проекта

```
open-webui-deploy/
├── docker-compose.yml       # Основная конфигурация
├── .env                     # Переменные окружения (не коммитить API-ключи!)
├── .env.example             # Пример для новых установок
├── scripts/
│   └── init-openwebui.py    # Init-скрипт: admin + pipe function + MCP
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
