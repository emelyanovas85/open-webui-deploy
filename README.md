# Open WebUI Deploy

Деплой [Open WebUI](https://github.com/open-webui/open-webui) на порту **8087** с Ollama и опциональным Webhook Handler ([MR_Checker](https://github.com/emelyanovas85/MR_Checker)).

## Стек

| Сервис | Порт | Описание |
|--------|------|----------|
| Open WebUI | **8087** | Web-интерфейс для LLM |
| Ollama | 11434 | Локальные LLM модели |
| Webhook Handler | 8080 | MR_Checker (опционально) |

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/emelyanovas85/open-webui-deploy.git
cd open-webui-deploy
```

### 2. Настроить переменные окружения

```bash
cp .env.example .env
# Отредактировать .env: прописать ключи API и секреты
```

### 3. Запустить

```bash
# Только Open WebUI + Ollama
docker compose up -d

# С Webhook Handler (MR_Checker)
docker compose --profile with-webhook up -d
```

### 4. Открыть в браузере

```
http://localhost:8087
```

## Настройка LLM

### Ollama (локальные модели)

Pull нужной модели после запуска:

```bash
docker exec -it ollama ollama pull llama3.2
docker exec -it ollama ollama pull qwen2.5
```

### OpenAI / совместимые API

Заполнить в `.env`:

```env
OPENAI_API_KEY=sk-...
OPENAI_API_BASE_URL=https://api.openai.com/v1
```

> **Совместимые провайдеры:** OpenRouter, Together AI, Groq, Mistral — просто поменяй `OPENAI_API_BASE_URL`.

### Подключение к внешнему Ollama

```env
OLLAMA_BASE_URL=http://192.168.1.100:11434
```

## Webhook Handler (MR_Checker)

Интеграция со Spring Boot приложением [MR_Checker](https://github.com/emelyanovas85/MR_Checker).

### Запуск с webhook handler

```bash
# Склонировать MR_Checker в папку mr-checker
git clone https://github.com/emelyanovas85/MR_Checker.git mr-checker

# Запустить с профилем
docker compose --profile with-webhook up -d
```

Эндпоинты Webhook Handler:

| Метод | URL |
|-------|-----|
| POST | `http://localhost:8080/api/webhook/gitlab` |
| POST | `http://localhost:8080/api/webhook/github` |
| GET | `http://localhost:8080/actuator/health` |

## Управление

```bash
# Статус сервисов
docker compose ps

# Логи Open WebUI
docker compose logs -f open-webui

# Логи Ollama
docker compose logs -f ollama

# Остановить всё
docker compose down

# Удалить данные (осторожно!)
docker compose down -v
```

## Структура проекта

```
open-webui-deploy/
├── docker-compose.yml     # Основная конфигурация
├── .env.example           # Пример переменных окружения
├── .env                   # Ваши настройки (не коммитить!)
├── .gitignore
└── README.md
```
