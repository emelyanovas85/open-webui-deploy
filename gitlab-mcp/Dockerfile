# =============================================================================
# GitLab MCP Server — Dockerfile
#
# Базовый образ: supercorp/supergateway:3.2.0 (Docker Hub, без ghcr.io)
# GitLab MCP файлы копируются из mcp/gitlab:latest (локальный образ).
#
# ВАЖНО: zod зафиксирован на v3.x
# @modelcontextprotocol/sdk v1.x несовместим с zod v4:
#   "Tool get_projects expected a Zod schema or ToolAnnotations,
#    but received an unrecognized object"
# zod v4 изменил внутренние идентификаторы схем (_def.typeName),
# которые SDK использует для проверки — поэтому нужен строго v3.
#
# ВАЖНО: --stateful ОБЯЗАТЕЛЕН
# Без него supergateway запускает новый процесс на каждый POST.
# MCP требует 3 запроса в одной сессии:
#   1. POST initialize        → Mcp-Session-Id
#   2. POST notifications/initialized
#   3. POST tools/list / tools/call
# В stateless-режиме шаги 2-3 попадают в новый процесс без инициализации
# и возвращают пустой список или ошибку "No valid session ID provided".
# =============================================================================

FROM supercorp/supergateway:3.2.0

# Копируем файлы GitLab MCP Server из локального образа
COPY --from=mcp/gitlab:latest /app /app

WORKDIR /app

# Фиксируем zod на v3 — несовместимость с @modelcontextprotocol/sdk v1.x
# zod v4 (^4.x) вызывает краш при старте: "received an unrecognized object"
RUN npm install zod@">=3.0.0 <4.0.0" --save --no-fund --no-audit 2>/dev/null || \
    npm install zod@3 --save --no-fund --no-audit

# supergateway запускается как ENTRYPOINT базового образа.
# --stateful: сохраняет дочерний процесс между запросами одной MCP-сессии.
# --outputTransport streamableHttp: требуется для Open WebUI v0.9.6+.
CMD ["--stdio", "node index.js", \
     "--port", "8083", \
     "--outputTransport", "streamableHttp", \
     "--streamableHttpPath", "/mcp", \
     "--healthEndpoint", "/health", \
     "--stateful"]
