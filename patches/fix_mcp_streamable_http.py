#!/usr/bin/env python3
"""
Patches open_webui/utils/tools.py to support MCP Streamable HTTP transport.

Problem:
  get_tool_server_data() always does GET {url} with Accept: application/json
  expecting an OpenAPI spec. Spring AI Streamable HTTP servers return 400
  on that request — they expect a POST with MCP initialize JSON-RPC payload.

Fix:
  Wrap get_tool_server_data() so that when the server URL contains '/mcp'
  (Streamable HTTP indicator), it performs an MCP initialize handshake via
  POST instead of a GET, then synthesises a minimal OpenAPI-like info dict
  so the rest of the Open WebUI code stays unchanged.

  The patch inserts an early-return branch at the top of get_tool_server_data:
    if '/mcp' in url:
        return await _mcp_streamable_initialize(url, headers)
  and injects the helper function just above get_tool_server_data.
"""
import sys

path = "/app/backend/open_webui/utils/tools.py"

SENTINEL = "# patched: mcp_streamable_http"

HELPER = '''
async def _mcp_streamable_initialize(url: str, headers: dict | None) -> dict:
    """
    Performs MCP initialize handshake over Streamable HTTP (POST to /mcp).
    Returns a minimal info dict compatible with what Open WebUI expects.
    """  # patched: mcp_streamable_http
    import uuid as _uuid
    _headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if headers:
        _headers.update(headers)
    payload = {
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "open-webui", "version": "0.9.6"},
        },
    }
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.post(
            url, json=payload, headers=_headers,
            ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
        ) as response:
            if response.status not in (200, 202):
                body = await response.text()
                raise Exception(f"MCP initialize failed: {response.status} {body[:200]}")
            content_type = response.headers.get("Content-Type", "")
            text = await response.text()
            result = {}
            if "text/event-stream" in content_type:
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        import json as _json
                        try:
                            result = _json.loads(line[5:].strip())
                        except Exception:
                            pass
                        break
            else:
                import json as _json
                try:
                    result = _json.loads(text)
                except Exception:
                    pass
            server_info = result.get("result", {}).get("serverInfo", {})
            name = server_info.get("name", url)
            version = server_info.get("version", "0.1.0")
            return {"info": {"id": url, "name": name, "version": version}}

'''

EARLY_RETURN = '    if "/mcp" in url:  # patched: mcp_streamable_http\n        return await _mcp_streamable_initialize(url, headers)\n'

with open(path, encoding="utf-8") as f:
    lines = f.readlines()

if any(SENTINEL in l for l in lines):
    print("[PATCH] fix_mcp_streamable_http: already patched, skipping")
    sys.exit(0)

out = []
i = 0
p_helper = p_return = 0

while i < len(lines):
    l = lines[i]

    # Insert helper function just before get_tool_server_data definition
    if "async def get_tool_server_data(" in l and p_helper == 0:
        out.append(HELPER)
        p_helper += 1

    out.append(l)

    # Insert early-return after the opening of get_tool_server_data
    # i.e. after the line that starts building _headers dict
    if "async def get_tool_server_data(" in l and p_return == 0:
        # skip to the first line of the function body (the _headers = { line)
        i += 1
        while i < len(lines) and lines[i].strip() == "":
            out.append(lines[i]); i += 1
        # now insert early-return before _headers
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

print("[PATCH] fix_mcp_streamable_http: tools.py written successfully")
