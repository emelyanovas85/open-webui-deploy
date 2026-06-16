#!/usr/bin/env python3
"""
Patches open_webui/utils/tools.py to allow type='mcp' in get_tool_servers_data.

open-webui v0.9.6 filters tool servers with:
  server.get('type', 'openapi') == 'openapi'

MCP servers are registered with type='mcp', so they are silently skipped
and tools list is always empty when only MCP servers are configured.

Fix: change the condition to:
  server.get('type', 'openapi') in ('openapi', 'mcp')
"""
import sys

path = "/app/backend/open_webui/utils/tools.py"

try:
    with open(path, encoding="utf-8") as f:
        src = f.read()
except FileNotFoundError:
    print(f"[SKIP] {path} not found")
    sys.exit(0)

old = "server.get('type', 'openapi') == 'openapi'"
new = "server.get('type', 'openapi') in ('openapi', 'mcp')"

if new in src:
    print("[PATCH] already applied — type in ('openapi', 'mcp') found")
    sys.exit(0)

if old not in src:
    print("[WARN] pattern not found — tools.py structure may have changed")
    for i, line in enumerate(src.splitlines()):
        if "type" in line and "openapi" in line and "server.get" in line:
            print(f"  found candidate line {i}: {line.strip()}")
    sys.exit(1)

patched = src.replace(old, new)
with open(path, "w", encoding="utf-8") as f:
    f.write(patched)

print("[PATCH] get_tool_servers_data now accepts type='mcp'")
