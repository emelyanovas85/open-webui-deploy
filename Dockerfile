# Патч к ghcr.io/open-webui/open-webui:
# Исправляет UnboundLocalError: 'server_id' not associated with a value
# в backend/open_webui/utils/tools.py при подключении MCP инструментов.
#
# Причина бага: если tool_id разбивается на != 2 и != 3 части (например
# server:mcp:host:port), ни одна ветка if/elif не выполняется, и server_id
# остаётся необъявленной, что вызывает UnboundLocalError при следующем
# обращении к переменной.
#
# Исправление: добавляем 'else: continue' после блока if/elif, чтобы
# пропустить некорректный tool_id без краша.

FROM ghcr.io/open-webui/open-webui:v0.9.6

# Применяем патч через Python — sed ненадёжен для многострочных замен
RUN python3 - <<'PYEOF'
import re

path = "/app/backend/open_webui/utils/tools.py"

with open(path, "r", encoding="utf-8") as f:
    src = f.read()

# Ищем блок:
#     elif len(splits) == 3:
#         type = splits[1]
#         server_id = splits[2]
#
#     server_id_splits = server_id.split('|')
#
# и вставляем 'else: continue' между ними

old = (
    "elif len(splits) == 3:\n"
    "                    type = splits[1]\n"
    "                    server_id = splits[2]\n"
    "\n"
    "                server_id_splits = server_id.split('|')"
)
new = (
    "elif len(splits) == 3:\n"
    "                    type = splits[1]\n"
    "                    server_id = splits[2]\n"
    "                else:\n"
    "                    # tool_id has unexpected number of parts — skip to avoid UnboundLocalError\n"
    "                    continue\n"
    "\n"
    "                server_id_splits = server_id.split('|')"
)

if old in src:
    src = src.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("[PATCH] tools.py patched successfully: added else: continue for server_id")
elif "else:\n                    # tool_id has unexpected number of parts" in src:
    print("[PATCH] tools.py already patched, skipping")
else:
    print("[WARN] tools.py patch target not found — code may have changed upstream")
    print("       Please verify tools.py manually")
PYEOF
