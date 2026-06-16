# Патч к ghcr.io/open-webui/open-webui:
# Исправляет UnboundLocalError: 'server_id' not associated with a value
# в backend/open_webui/utils/tools.py при подключении MCP инструментов.
#
# Причина бага: если tool_id разбивается на != 2 и != 3 части (например
# server:mcp:host:port), ни одна ветка if/elif не выполняется, и server_id
# остаётся необъявленной, что вызывает UnboundLocalError при следующем
# обращении к переменной.
#
# Исправление: добавляем инициализацию server_id = None перед блоком if/elif
# и guard-check после него, чтобы пропустить некорректный tool_id без краша.

FROM ghcr.io/open-webui/open-webui:v0.9.6

# Применяем патч через Python с regex — не зависит от количества пробелов
RUN python3 - <<'PYEOF'
import re

path = "/app/backend/open_webui/utils/tools.py"

with open(path, "r", encoding="utf-8") as f:
    src = f.read()

# Already patched?
if "server_id = None  # patched" in src:
    print("[PATCH] tools.py already patched, skipping")
    exit(0)

# Strategy: find the block
#     splits = tool_id.split(':')
#     if len(splits) == 2:
#         ...
#         server_id = splits[1]
#     elif len(splits) == 3:
#         ...
#         server_id = splits[2]
#
# and insert:
#   1. server_id = None  # patched   — before the if block
#   2. else: continue                — after the elif block, before the next statement

# Step 1: insert `server_id = None` before `if len(splits) == 2:`
pattern1 = r'([ \t]*)(splits\s*=\s*tool_id\.split\([\'\"]:[\'"]\)\n)([ \t]*if len\(splits\) == 2:)'
replacement1 = r'\1\2\1server_id = None  # patched\n\3'
src2, n1 = re.subn(pattern1, replacement1, src)
if n1 == 0:
    print("[WARN] Step 1 pattern not found in tools.py — code may have changed upstream")
    print("       Trying fallback…")
else:
    print(f"[PATCH] Step 1 applied ({n1} replacement)")

# Step 2: insert `else: continue` after the elif block, before `server_id_splits =`
# Match the elif block end and the next statement
pattern2 = r'([ \t]*elif len\(splits\) == 3:\n[ \t]+\S[^\n]*\n[ \t]+\S[^\n]*\n)(\n?)([ \t]*server_id_splits\s*=)'
def add_else(m):
    indent = re.match(r'([ \t]*)', m.group(3)).group(1)
    return (
        m.group(1)
        + f"{indent}else:\n"
        + f"{indent}    # tool_id has unexpected number of parts — skip to avoid UnboundLocalError\n"
        + f"{indent}    continue\n"
        + m.group(2)
        + m.group(3)
    )

src3, n2 = re.subn(pattern2, add_else, src2)
if n2 == 0:
    print("[WARN] Step 2 pattern not found in tools.py — code may have changed upstream")
else:
    print(f"[PATCH] Step 2 applied ({n2} replacement)")

if n1 > 0 or n2 > 0:
    with open(path, "w", encoding="utf-8") as f:
        f.write(src3)
    print("[PATCH] tools.py written successfully")
else:
    print("[ERROR] No patches applied — please check tools.py manually")
    exit(1)
PYEOF
