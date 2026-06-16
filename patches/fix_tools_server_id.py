#!/usr/bin/env python3
"""
Patches open_webui/utils/tools.py to fix UnboundLocalError:
  cannot access local variable 'server_id' where it is not associated with a value

The bug: if tool_id.split(':') returns != 2 and != 3 parts,
neither if/elif branch runs, server_id stays unbound,
and the next line `server_id.split('|')` crashes.

Fix:
  1. Insert `server_id = None` before `if len(splits) == 2:`
  2. Insert `else: continue` after `elif len(splits) == 3:` block
"""
import sys

path = "/app/backend/open_webui/utils/tools.py"

with open(path, encoding="utf-8") as f:
    lines = f.readlines()

if any("server_id = None  # patched" in l for l in lines):
    print("[PATCH] already patched, skipping")
    sys.exit(0)

out = []
i = 0
p1 = p2 = 0

while i < len(lines):
    l = lines[i]

    # Step 1: insert `server_id = None  # patched` before `if len(splits) == 2:`
    if "if len(splits) == 2:" in l and p1 == 0:
        indent = " " * (len(l) - len(l.lstrip()))
        out.append(indent + "server_id = None  # patched\n")
        p1 += 1

    # Step 2: insert `else: continue` after elif len(splits) == 3: block (2 body lines)
    if "elif len(splits) == 3:" in l and p2 == 0:
        out.append(l); i += 1           # elif line
        out.append(lines[i]); i += 1   # type = splits[1]
        out.append(lines[i]); i += 1   # server_id = splits[2]
        indent = " " * (len(l) - len(l.lstrip()))
        out.append(indent + "else:\n")
        out.append(indent + "    continue  # unexpected tool_id format\n")
        p2 += 1
        continue

    out.append(l)
    i += 1

print(f"[PATCH] Step 1 (server_id=None): {'OK' if p1 else 'FAIL'}")
print(f"[PATCH] Step 2 (else: continue): {'OK' if p2 else 'FAIL'}")

if p1 == 0 and p2 == 0:
    print("[ERROR] No patches applied — tools.py structure may have changed")
    sys.exit(1)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)

print("[PATCH] tools.py written successfully")
