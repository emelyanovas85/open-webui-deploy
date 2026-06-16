FROM ghcr.io/open-webui/open-webui:v0.9.6

# Патч: исправляет UnboundLocalError 'server_id' в tools.py
# Между `splits = tool_id.split(':')` и `if len(splits) == 2:` есть пустая строка,
# поэтому используем построчный подход вместо regex по всему блоку.
RUN python3 - <<'PYEOF'
path = "/app/backend/open_webui/utils/tools.py"

with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

if any("server_id = None  # patched" in l for l in lines):
    print("[PATCH] already patched, skipping")
    exit(0)

out = []
i = 0
patched1 = 0
patched2 = 0

while i < len(lines):
    line = lines[i]

    # Step 1: вставляем `server_id = None` перед `if len(splits) == 2:`
    # (после строки со splits = tool_id.split(':'), возможно через пустую строку)
    stripped = line.rstrip()
    if "if len(splits) == 2:" in stripped and patched1 == 0:
        indent = len(line) - len(line.lstrip())
        prefix = " " * indent
        out.append(prefix + "server_id = None  # patched\n")
        patched1 += 1

    # Step 2: вставляем `else: continue` после блока elif len(splits) == 3:
    # Ищем конец elif-блока: две строки с присваиваниями после elif
    if "elif len(splits) == 3:" in stripped and patched2 == 0:
        out.append(line)
        i += 1
        # следующие 2 строки — тело elif
        for _ in range(2):
            if i < len(lines):
                out.append(lines[i])
                i += 1
        indent = len(line) - len(line.lstrip())
        prefix = " " * indent
        out.append(prefix + "else:\n")
        out.append(prefix + "    continue  # unexpected tool_id format\n")
        patched2 += 1
        continue

    out.append(line)
    i += 1

if patched1 == 0:
    print("[WARN] Step 1 not applied — 'if len(splits) == 2:' not found")
else:
    print(f"[PATCH] Step 1 applied — server_id = None inserted")

if patched2 == 0:
    print("[WARN] Step 2 not applied — 'elif len(splits) == 3:' not found")
else:
    print(f"[PATCH] Step 2 applied — else: continue inserted")

if patched1 > 0 or patched2 > 0:
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    print("[PATCH] tools.py written successfully")
else:
    print("[ERROR] No patches applied")
    exit(1)
PYEOF
