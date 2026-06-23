#!/usr/bin/env python3
"""
PATCH COMPATIBILITY CHECK

Verifies that the structure of open_webui/utils/tools.py is compatible
with our patches BEFORE applying them.

Checks:
  1. Function 'get_tool_server_data' exists (async def)
  2. Function 'get_tool_servers_data' exists (async def)  [type filter patch target]
  3. 'type' filtering block is present                    [fix_tools_type_filter target]
  4. 'server_id' variable is assigned inside tools loop   [fix_tools_server_id target]
  5. 'aiohttp' is imported                               [MCP patch dependency]
  6. 'AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA' constant exists
  7. 'AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL' constant exists

Exits with code 0 if all checks pass.
Exits with code 1 if any check fails — Docker build will abort with a clear message.
"""
import sys
import re

path = "/app/backend/open_webui/utils/tools.py"

try:
    with open(path, encoding="utf-8") as f:
        content = f.read()
except FileNotFoundError:
    print(f"[COMPAT-CHECK] ERROR: {path} not found")
    sys.exit(1)

checks = [
    (
        "async def get_tool_server_data(",
        "Function get_tool_server_data not found — MCP patch (fix_mcp_streamable_http) will fail",
    ),
    (
        "async def get_tool_servers_data(",
        "Function get_tool_servers_data not found — type-filter patch (fix_tools_type_filter) will fail",
    ),
    (
        "aiohttp",
        "aiohttp not imported — MCP patch helper depends on it",
    ),
    (
        "AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA",
        "Constant AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA not found — MCP patch will raise NameError",
    ),
    (
        "AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL",
        "Constant AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL not found — MCP patch will raise NameError",
    ),
]

# Regex-based check: server_id assignment inside a loop (fix_tools_server_id target)
server_id_checks = [
    (
        r"server_id\s*=",
        "Variable 'server_id' assignment not found — fix_tools_server_id patch may not apply correctly",
    ),
]

failed = []

for needle, msg in checks:
    if needle not in content:
        failed.append(msg)
        print(f"[COMPAT-CHECK] FAIL: {msg}")
    else:
        print(f"[COMPAT-CHECK] OK:   {needle!r} found")

for pattern, msg in server_id_checks:
    if not re.search(pattern, content):
        failed.append(msg)
        print(f"[COMPAT-CHECK] FAIL: {msg}")
    else:
        print(f"[COMPAT-CHECK] OK:   pattern {pattern!r} found")

if failed:
    print()
    print("[COMPAT-CHECK] ============================================")
    print("[COMPAT-CHECK] COMPATIBILITY CHECK FAILED")
    print("[COMPAT-CHECK] tools.py structure has changed in this version of open-webui.")
    print("[COMPAT-CHECK] Review and update patches before building.")
    print("[COMPAT-CHECK] ============================================")
    sys.exit(1)

print()
print("[COMPAT-CHECK] All checks passed — patches are compatible with this tools.py")
sys.exit(0)
