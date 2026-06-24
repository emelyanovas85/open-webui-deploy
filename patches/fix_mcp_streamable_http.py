#!/usr/bin/env python3
"""
Patches open_webui/utils/tools.py to support MCP Streamable HTTP transport
(Spring AI / MCP SDK 0.10+).

Fix (v5):
  - Accept header order fixed: "text/event-stream, application/json"
    Spring AI WebMvcStreamableServerTransportProvider requires text/event-stream
    to appear first in the Accept header, otherwise returns -32601 error.
  - All other logic identical to v4.
"""
import sys

path = "/app/backend/open_webui/utils/tools.py"

SENTINEL = "# patched: mcp_streamable_http_v5"

HELPER = '''
async def _is_mcp_server(url: str, headers: dict | None) -> bool:
    """Probe URL to check if it speaks MCP Streamable HTTP."""
    # patched: mcp_streamable_http_v5
    import uuid as _uuid
    import json as _json

    _headers = {
        "Accept": "text/event-stream, application/json",
        "Content-Type": "application/json",
    }
    if headers:
        _headers.update(headers)

    probe_payload = {
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "open-webui-probe", "version": "0.1"},
        },
    }
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url, json=probe_payload, headers=_headers,
                ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
            ) as resp:
                if resp.status not in (200, 202):
                    return False
                text = await resp.text()
                ct = resp.headers.get("Content-Type", "")
                if "text/event-stream" in ct:
                    for line in text.splitlines():
                        if line.strip().startswith("data:"):
                            try:
                                data = _json.loads(line.strip()[5:].strip())
                                result = data.get("result", {})
                                if "serverInfo" in result or "protocolVersion" in result:
                                    return True
                            except Exception:
                                pass
                    return False
                try:
                    data = _json.loads(text)
                    result = data.get("result", {})
                    return "serverInfo" in result or "protocolVersion" in result
                except Exception:
                    return False
    except Exception:
        return False


async def _mcp_streamable_initialize(url: str, headers: dict | None) -> dict:
    """MCP Streamable HTTP: initialize (with version negotiation) + tools/list -> OpenAPI-compatible dict."""
    # patched: mcp_streamable_http_v5
    import uuid as _uuid
    import json as _json

    _headers = {
        "Accept": "text/event-stream, application/json",
        "Content-Type": "application/json",
    }
    if headers:
        _headers.update(headers)

    PROTOCOL_VERSIONS = ["2025-03-26", "2024-11-05"]

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

        # Step 1: initialize with protocol version negotiation
        session_id = ""
        init_result = {}
        last_error = None

        for proto_version in PROTOCOL_VERSIONS:
            init_payload = {
                "jsonrpc": "2.0",
                "id": str(_uuid.uuid4()),
                "method": "initialize",
                "params": {
                    "protocolVersion": proto_version,
                    "capabilities": {},
                    "clientInfo": {"name": "open-webui", "version": "0.9.6"},
                },
            }
            try:
                async with session.post(
                    url, json=init_payload, headers=_headers,
                    ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
                ) as resp:
                    if resp.status not in (200, 202):
                        last_error = f"HTTP {resp.status}"
                        continue
                    session_id = resp.headers.get("Mcp-Session-Id", "")
                    ct = resp.headers.get("Content-Type", "")
                    text = await resp.text()
                    init_result = _parse_body(text, ct)
                    # Check for protocol version mismatch error
                    err = init_result.get("error", {})
                    if err and err.get("code") in (-32602, -32600):
                        last_error = f"protocol mismatch for {proto_version}: {err}"
                        continue
                    # Success
                    break
            except Exception as e:
                last_error = str(e)
                continue
        else:
            raise Exception(f"MCP initialize failed after all protocol versions: {last_error}")

        server_info = init_result.get("result", {}).get("serverInfo", {})
        srv_name = server_info.get("name", url)
        srv_version = server_info.get("version", "0.1.0")

        # Step 2: tools/list — always call, session_id optional
        # Stateless servers (supergateway) don\'t return Mcp-Session-Id,
        # but tools/list must still be called.
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
                    "x-mcp-base-url": url,
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

# Early-return block: probe first, then call MCP init or fall through
EARLY_RETURN = '''    # patched: mcp_streamable_http_v5
    # Smart MCP detection: probe URL instead of checking URL path
    try:
        if await _is_mcp_server(url, headers):
            return await _mcp_streamable_initialize(url, headers)
    except Exception as _mcp_probe_err:
        log.warning(f"MCP probe for {url} failed: {_mcp_probe_err}, falling through to OpenAPI")

'''

with open(path, encoding="utf-8") as f:
    lines = f.readlines()

# Already patched v5?
if any("patched: mcp_streamable_http_v5" in l for l in lines):
    print("[PATCH] fix_mcp_streamable_http v5: already patched, skipping")
    sys.exit(0)

# Remove previous v1/v2/v3/v4 patch artefacts
clean = []
skip_helper = False
for l in lines:
    if "async def _is_mcp_server(" in l or "async def _mcp_streamable_initialize(" in l:
        skip_helper = True
    if skip_helper:
        # Stop skipping when we hit a new top-level async def that is NOT our helpers
        if (l.startswith("async def ") and
                "_is_mcp_server" not in l and
                "_mcp_streamable_initialize" not in l):
            skip_helper = False
            clean.append(l)
        continue
    if "patched: mcp_streamable_http" in l:
        continue  # drop old sentinel/early-return lines
    if '"\'/mcp\' in url"' in l or "'/mcp' in url" in l or '"/mcp" in url' in l:
        continue  # drop old early-return condition line
    if "return await _mcp_streamable_initialize" in l:
        continue  # drop old early-return body
    if "MCP probe for" in l:
        continue
    clean.append(l)
lines = clean

out = []
i = 0
p_helper = p_return = 0

while i < len(lines):
    l = lines[i]

    # Insert helpers just before get_tool_server_data definition
    if "async def get_tool_server_data(" in l and p_helper == 0:
        out.append(HELPER)
        p_helper += 1

    out.append(l)

    # Insert early-return as first statements inside get_tool_server_data
    if "async def get_tool_server_data(" in l and p_return == 0:
        i += 1
        # consume blank lines after def line
        while i < len(lines) and lines[i].strip() == "":
            out.append(lines[i])
            i += 1
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

print("[PATCH] fix_mcp_streamable_http v5: tools.py written successfully")
