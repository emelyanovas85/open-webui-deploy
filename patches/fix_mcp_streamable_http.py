#!/usr/bin/env python3
"""
Patches open_webui/utils/tools.py to support MCP Streamable HTTP transport
(Spring AI / MCP SDK 0.10+).

Problem:
  get_tool_server_data() does GET {url} expecting OpenAPI spec.
  Spring AI Streamable HTTP requires:
    1. POST /mcp  initialize  -> response header Mcp-Session-Id (stateful)
                              OR no header at all            (stateless)
    2. POST /mcp  tools/list  with Mcp-Session-Id if present
  Without calling tools/list the tool list is always empty.

Fix (v3):
  Same as v2, but tools/list is now called UNCONDITIONALLY.
  Stateless servers (supergateway) don't return Mcp-Session-Id — the old
  `if session_id:` guard prevented tools/list from ever being called.
  Now: always call tools/list; add Mcp-Session-Id header only when present.
"""
import sys

path = "/app/backend/open_webui/utils/tools.py"

SENTINEL = "# patched: mcp_streamable_http_v3"

HELPER = '''
async def _mcp_streamable_initialize(url: str, headers: dict | None) -> dict:
    """MCP Streamable HTTP: initialize + tools/list -> OpenAPI-compatible dict."""
    # patched: mcp_streamable_http_v3
    import uuid as _uuid
    import json as _json

    _headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if headers:
        _headers.update(headers)

    def _parse_body(text, content_type):
        """Parse JSON or SSE response body."""
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        return _json.loads(line[5:].strip())
                    except Exception:
                        pass
            return {}
        try:
            return _json.loads(text)
        except Exception:
            return {}

    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        # Step 1: initialize
        init_payload = {
            "jsonrpc": "2.0",
            "id": str(_uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "open-webui", "version": "0.9.6"},
            },
        }
        async with session.post(
            url, json=init_payload, headers=_headers,
            ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
        ) as resp:
            if resp.status not in (200, 202):
                body = await resp.text()
                raise Exception(f"MCP initialize failed: {resp.status} {body[:200]}")
            session_id = resp.headers.get("Mcp-Session-Id", "")
            ct = resp.headers.get("Content-Type", "")
            text = await resp.text()
            init_result = _parse_body(text, ct)

        server_info = init_result.get("result", {}).get("serverInfo", {})
        srv_name = server_info.get("name", url)
        srv_version = server_info.get("version", "0.1.0")

        # Step 2: tools/list — always call, session_id optional
        # FIX v3: removed `if session_id:` guard — stateless MCP servers
        # (e.g. supergateway) don't return Mcp-Session-Id, but tools/list
        # must still be called to populate the tool list.
        tools = []
        list_headers = dict(_headers)
        if session_id:
            list_headers["Mcp-Session-Id"] = session_id
        list_payload = {
            "jsonrpc": "2.0",
            "id": str(_uuid.uuid4()),
            "method": "tools/list",
            "params": {},
        }
        async with session.post(
            url, json=list_payload, headers=list_headers,
            ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
        ) as resp2:
            if resp2.status in (200, 202):
                ct2 = resp2.headers.get("Content-Type", "")
                text2 = await resp2.text()
                list_result = _parse_body(text2, ct2)
                tools = list_result.get("result", {}).get("tools", [])

        # Step 3: build OpenAPI-compatible paths from tools
        paths = {}
        for tool in tools:
            t_name = tool.get("name", "unknown")
            t_desc = tool.get("description", "")
            input_schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            paths[f"/tools/{t_name}"] = {
                "post": {
                    "operationId": t_name,
                    "summary": t_desc,
                    "description": t_desc,
                    "x-mcp-tool": True,
                    "x-mcp-session-id": session_id,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": input_schema
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Tool result",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        }
                    },
                }
            }

        return {
            "openapi": "3.1.0",
            "info": {"title": srv_name, "version": srv_version},
            "paths": paths,
            "x-mcp-session-id": session_id,
            "x-mcp-base-url": url,
        }

'''

EARLY_RETURN = '    if "/mcp" in url:  # patched: mcp_streamable_http_v3\n        return await _mcp_streamable_initialize(url, headers)\n'

with open(path, encoding="utf-8") as f:
    lines = f.readlines()

# Already patched v3?
if any("patched: mcp_streamable_http_v3" in l for l in lines):
    print("[PATCH] fix_mcp_streamable_http v3: already patched, skipping")
    sys.exit(0)

# Remove previous v1/v2 patch artefacts
clean = []
skip_helper = False
for l in lines:
    if "async def _mcp_streamable_initialize(" in l:
        skip_helper = True
    if skip_helper:
        if l.startswith("async def ") and "_mcp_streamable_initialize" not in l:
            skip_helper = False
            clean.append(l)
        continue
    if "patched: mcp_streamable_http" in l:
        continue  # drop old sentinel comment lines
    clean.append(l)
lines = clean

out = []
i = 0
p_helper = p_return = 0

while i < len(lines):
    l = lines[i]

    if "async def get_tool_server_data(" in l and p_helper == 0:
        out.append(HELPER)
        p_helper += 1

    out.append(l)

    if "async def get_tool_server_data(" in l and p_return == 0:
        i += 1
        while i < len(lines) and lines[i].strip() == "":
            out.append(lines[i]); i += 1
        out.append(EARLY_RETURN)
        p_return += 1
        continue

    i += 1

print(f"[PATCH] helper inserted: {'OK' if p_helper else 'FAIL'}")
print(f"[PATCH] early-return inserted: {'OK' if p_return else 'FAIL'}")

if p_helper == 0 or p_return == 0:
    print("[ERROR] Patch failed — tools.py structure may have changed")
    sys.exit(1)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)

print("[PATCH] fix_mcp_streamable_http v3: tools.py written successfully")
